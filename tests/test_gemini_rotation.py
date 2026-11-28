import json
from types import SimpleNamespace
from pathlib import Path
import pytest
import gemini_failover as g


class Manager:
    probe=False
    def __init__(self):
        self.config={'live_input': True}
        self.active_sessions={}
        self.routes=[{'base_url':'https://'+h,'api_key':h} for h in ('a','b','c')]
        self.states={'w':{'token':'saved','worker_control':{'provider_route':2}}}
    def worker(self,name):return {'kind':'gemini','model':'carbon'}
    def provider_pool(self,name):return self.routes
    def persist(self,name):pass


def test_same_session_all_routes_cooldown_and_recovery(tmp_path,monkeypatch):
    monkeypatch.setattr(g,'ROOT',tmp_path)
    m=Manager();s=m.states['w'];ids=[g.key_id(r) for r in m.routes]
    p=tmp_path/'log'
    p.write_text(json.dumps({'event':'result','result':{'status':'ERROR','error':'Error 503, temporarily unavailable'}}))
    session=SimpleNamespace(transcript_path=p)
    sequence=[]
    for _ in range(4):
        idx=g.choose(s,m.routes);sequence.append(idx);g.begin(s,m.routes,idx,'carbon')
        assert g.schedule(m,'w',session,SimpleNamespace(category='infra_transient'))
        assert s['token']=='saved'
        m.states['w']=s=json.loads(json.dumps(s))
    assert sequence==[0,0,1,2]
    assert s['gemini_rotation']['exhausted'] and s['next']>g.time.time()+290
    assert set(s['gemini_rotation']['observations'])==set(ids)
    g.begin(s,m.routes,0,'carbon')
    g.event_evidence(s, {'event':'result','result':{'conversation_id':'saved','status':'SUCCESS','response':'Done'}}, g.time.time())
    assert s['gemini_rotation']['exhausted']
    g.event_evidence(s, {'step_update':{'conversation_id':'saved','step_type':'tool','tool_name':'view_file','state':'DONE','tool_info':{'error':'no file'}}}, g.time.time())
    assert s['gemini_rotation']['exhausted']
    g.event_evidence(s, {'step_update':{'conversation_id':'saved','step_type':'tool','tool_name':'view_file','state':'DONE','tool_info':{'output':'data'}}}, g.time.time())
    assert not s['gemini_rotation']['exhausted']


def test_policy_and_refusal(tmp_path,monkeypatch):
    monkeypatch.setattr(g,'ROOT',tmp_path)
    m=Manager()
    (tmp_path/'gemini-routing.json').write_text(json.dumps({'enabled':[g.key_id(m.routes[1])]}))
    assert g.eligible(m,'w')==m.routes[1:2]
    p=tmp_path/'log';p.write_text(json.dumps({'event':'result','result':{'error':'safety refusal'}}))
    g.schedule(m,'w',SimpleNamespace(transcript_path=p),SimpleNamespace(category='infra_transient'))
    assert m.states['w']['blocked_reason']=='model_refusal'


def test_migrate_legacy_watchdog_only_when_identity_matches(tmp_path):
    m=Manager(); m.run=tmp_path
    state=m.states['w']
    state.update(launches=3078, blocked_reason='worker_control_failed',
                 worker_control={'session_id':'saved','provider_route':2},
                 worker_control_hold={'session_id':'saved','failure_category':'infra_transient'})
    transcript=tmp_path/'w/.runtime/launch_003078/transcript.jsonl'
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({'event':'result','result':{'status':'SUCCESS'}})+'\n'+
                          json.dumps({'type':'watchdog','action':'resume_native'}))
    assert g.guard(m,'w')
    assert state.get('worker_control_hold') is None and 'provider_route' not in state['worker_control']
    state.update(blocked_reason='worker_control_failed',worker_control_hold={'session_id':'other','failure_category':'infra_transient'})
    assert not g.guard(m,'w')
    assert state['blocked_reason']=='worker_control_conflict'


@pytest.mark.parametrize(('diagnostic','reason'),[
    ('Error 400: malformed field','gemini_unclassified_failure'),
    ('prohibited by safety policy','model_refusal'),
    ('native compaction failed','gemini_compaction_failed')])
def test_gemini_blocks_without_worker_control(tmp_path,diagnostic,reason):
    m=Manager();m.run=tmp_path
    s=m.states['w'];s.pop('worker_control')
    g.begin(s,m.routes,0,'carbon')
    path=tmp_path/'transcript'
    path.write_text(json.dumps({'event':'result','result':{'status':'ERROR','error':diagnostic}}))
    assert g.schedule(m,'w',SimpleNamespace(transcript_path=path), SimpleNamespace(category='infra_transient'))
    assert s['blocked_reason']==reason and not g.guard(m,'w')


def test_backup_native_file_read_after_primary_503_and_restart(tmp_path, monkeypatch):
    """Use the real controller lifecycle and a disposable CLI with local HTTP endpoints."""
    import http.server
    import threading
    import sys
    from test_antigravity import config, environment, drain
    from common import read, save
    from main import prepare_workspaces
    from recovery import ResearchManager
    monkeypatch.setattr(g,'ROOT',tmp_path)
    accounts=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            accounts.append(self.server.status)
            self.send_response(self.server.status)
            self.end_headers()
            self.wfile.write(b'view_file')
        def log_message(self,*args): pass
    servers=[]
    for status in (503,200,503):
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.status=status
        threading.Thread(target=server.serve_forever,daemon=True).start()
        servers.append(server)
    try:
        c=config(tmp_path);env=environment(tmp_path); worker='gemini_1'
        c['gemini_context_tokens']=300000
        urls=[f'http://127.0.0.1:{server.server_port}' for server in servers]
        env.update(RSTATION_WORKER_GEMINI_BASE_URL=urls[0],
                   RSTATION_WORKER_GEMINI_BACKUP_API_KEY='b;c',
                   RSTATION_WORKER_GEMINI_BACKUP_BASE_URL=';'.join(urls[1:]))
        Path(env['ANTIGRAVITY_BIN_PATH']).write_text('#!'+sys.executable+'\n'+"""
import json, os, pathlib, sys, urllib.request, urllib.error
def emit(event): print(json.dumps(event), flush=True)
sid='test-saved'
if '--conversation' in sys.argv: assert sys.argv[sys.argv.index('--conversation')+1]==sid
emit({'event':'init','conversation_id':sid})
try:
    assert urllib.request.urlopen(os.environ['GOOGLE_GEMINI_BASE_URL']).read()==b'view_file'
except urllib.error.HTTPError as exc:
    emit({'event':'result','result':{'conversation_id':sid,'status':'ERROR','error':'HTTP '+str(exc.code)}})
    sys.exit(1)
answer=pathlib.Path('sample').read_text()
emit({'step_update':{'conversation_id':sid,'step_type':'tool','state':'DONE','tool_name':'view_file','tool_info':{'output':answer}}})
emit({'event':'result','result':{'conversation_id':sid,'status':'SUCCESS','response':answer}})
""")
        save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
        (tmp_path/worker/'sample').write_text('blue skua')
        for i in range(3):
            m=ResearchManager(tmp_path,c,env)
            try:
                m.states[worker]['next']=0
                assert m.launch_cli_job(worker)
                drain(m)
                assert m.states[worker]['token']=='test-saved'
            finally:m.close()
        state=read(tmp_path/worker/'.runtime/state.json')
        assert accounts==[503,503,200]
        assert state['last_response_excerpt']=='blue skua'
        rot=state['gemini_rotation']
        assert rot['model_observations'][rot['model']][rot['enabled'][0]]['http_status']==503
        assert rot['observations'][rot['enabled'][1]]['status']=='working'
        assert not rot['exhausted']
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


@pytest.mark.parametrize('output', ['Error: no such file', 'Command exited with code 2', '{"exitCode":1}',
                                    {'error':'no permission'}, {'success': False}])
def test_done_tool_with_failed_output_is_not_recovery(output):
    assert not g.successful_tool({'step_type':'tool','state':'DONE',
                                 'tool_info':{'name':'run_command','output':output}})

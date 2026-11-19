"""Run actual CLIs against local mock providers; never contact paid APIs."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import read, save, alive
from main import parser, build_config, prepare_workspaces
from recovery import ResearchManager
from messages import Messages


def wait_for(manager, predicate, seconds=40):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        manager.poll_cli_jobs()
        if predicate():return
        time.sleep(.03)
    states={n:{k:s.get(k) for k in ('status','last_error','last_response_excerpt')} for n,s in manager.states.items()}
    raise AssertionError(str(states))


@pytest.mark.parametrize('kind,supervisor',[('codex',False),('codex',True),('claude',False),('gemini',False)])
def test_live_native_messages_keep_process_and_conversation(tmp_path,kind,supervisor,monkeypatch):
    suffix='ANTIGRAVITY' if kind=='gemini' else kind.upper()
    binary=os.environ.get('RSTATION_TEST_'+suffix+'_BIN')
    if not binary:pytest.skip('Set RSTATION_TEST_'+suffix+'_BIN')
    calls=[];release=threading.Event()
    class API(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                data=b'{"input_tokens":100}';self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            calls.append(body)
            release.wait(30)
            text='ACK_OPERATOR_MESSAGE' if 'RESEARCH_PIVOT_SENTINEL' in json.dumps(body) else 'Initial response'
            if kind=='gemini':
                data={'candidates':[{'content':{'role':'model','parts':[{'text':text}]},'finishReason':'STOP'}],
                      'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':4}}
                payload=('data: '+json.dumps(data)+'\n\n').encode()
            else:
                if kind=='claude':
                    events=[('message_start',{'message':{'id':'msg_test','type':'message','role':'assistant','model':body['model'],'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}}),
                        ('content_block_start',{'index':0,'content_block':{'type':'text','text':''}}),
                        ('content_block_delta',{'index':0,'delta':{'type':'text_delta','text':text}}),
                        ('content_block_stop',{'index':0}),('message_delta',{'delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':4}}),('message_stop',{})]
                else:
                    item={'id':'msg_test','type':'message','role':'assistant','status':'completed','content':[{'type':'output_text','text':text,'annotations':[]}]}
                    events=[('response.created',{'response':{'id':'resp_test','status':'in_progress','output':[]}}),
                        ('response.output_item.added',{'output_index':0,'item':item}),
                        ('response.output_text.delta',{'item_id':'msg_test','output_index':0,'content_index':0,'delta':text}),
                        ('response.output_item.done',{'output_index':0,'item':item}),
                        ('response.completed',{'response':{'id':'resp_test','status':'completed','output':[item],'usage':{'input_tokens':10,'output_tokens':4,'total_tokens':14}}})]
                payload=''.join('event: '+n+'\ndata: '+json.dumps({'type':n,**v})+'\n\n' for n,v in events).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    api=ThreadingHTTPServer(('127.0.0.1',0),API)
    thread=threading.Thread(target=api.serve_forever,daemon=True);thread.start()
    args=parser().parse_args(['start','test','--codex',str(int(kind=='codex')),'--claude',str(int(kind=='claude')),'--gemini',str(int(kind=='gemini'))])
    config=build_config(args,{'category':'open_ended'});config['model_api_proxy']='direct'
    if kind=='codex':config['workers'][0]['model']='gpt-6-astra'
    role=kind.upper();url=f'http://127.0.0.1:{api.server_port}'
    env={'PATH':os.environ['PATH'],'HOME':str(tmp_path),'CODEX_BIN_PATH':binary,'CLAUDE_BIN_PATH':binary,'ANTIGRAVITY_BIN_PATH':binary,
         'RSTATION_WORKER_'+role+'_API_KEY':'dummy-native-test','RSTATION_WORKER_'+role+'_BASE_URL':url+('/v1' if kind=='codex' else '')}
    save(tmp_path/'run.json',config);prepare_workspaces(tmp_path,config)
    if supervisor:
        save(tmp_path/'codex_1/.runtime/supervision.json', {'supervisor':True})
    manager=ResearchManager(tmp_path,config,env);name=kind+'_1';mail=Messages(tmp_path/name)
    try:
        assert manager.launch_cli_job(name)
        wait_for(manager,lambda:bool(calls))
        session=manager.active_sessions[name];pid=session.process.pid
        native=read(tmp_path/name/'.runtime/live_input.json');assert alive(native['pid'],native['token'])
        first=mail.add('RESEARCH_PIVOT_SENTINEL: test live delivery while the initial model request is still running.',
                       delivery_key='supervision:mock:1' if supervisor else None)
        if kind == 'claude':
            # The operator instruction must stay queued while Claude is waiting
            # on its first provider response; sending stdin now can lose it.
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                manager.poll_cli_jobs(); time.sleep(.03)
            assert mail.list()[0]['status'] == 'queued'
        else:
            wait_for(manager,lambda:mail.list()[0]['status'] in {'sent','received'})
        assert session.process.pid==pid and session.process.poll() is None
        if supervisor:
            wait_for(manager,lambda:mail.list()[0]['status']=='received')
            followup=mail.add('RESEARCH_PIVOT_SENTINEL: another private supervisory message during this same turn.',
                              delivery_key='supervision:mock:2')
            wait_for(manager,lambda:mail.list()[1]['status']=='received')
            assert len(calls)==1 and not release.is_set()
            assert mail.list()[0]['status']=='received'
            assert manager.active_sessions[name].process.pid==pid
        release.set()
        wait_for(manager,lambda:mail.list()[0]['status']=='delivered')
        if supervisor:
            wait_for(manager,lambda:next(m for m in mail.list() if m['id']==followup)['status']=='delivered')
        wait_for(manager,lambda:any('RESEARCH_PIVOT_SENTINEL' in json.dumps(c) for c in calls))
        token=manager.states[name]['token'];assert token
        if kind=='codex':
            assert calls[0]['model']=='gpt-6-astra'
            # The loaded controller keeps its old config; only the run file changes.
            updated={**config,'workers':[{**config['workers'][0],'model':'gpt-6.2-astra'}]}
            save(tmp_path/'run.json',updated)
        second=mail.add('RESEARCH_PIVOT_SENTINEL: second operator message, no restart.')
        wait_for(manager,lambda:next(m for m in mail.list() if m['id']==second)['status']=='delivered')
        assert first==1 and second==(3 if supervisor else 2)
        assert manager.states[name]['token']==token
        assert manager.active_sessions[name].process.pid==pid
        assert read(tmp_path/name/'.runtime/live_input.json')['pid']==native['pid']
        assert 'ACK_OPERATOR_MESSAGE' in Path(session.transcript_path).read_text()
        if kind=='codex':
            expected='gpt-6-astra' if supervisor else 'gpt-6.2-astra'
            wait_for(manager,lambda:any(c['model']==expected and 'second operator message' in json.dumps(c) for c in calls))
            switched=[c for c in calls if c['model']==expected and 'second operator message' in json.dumps(c)]
            assert switched[-1]['reasoning']['effort']=='max'
            assert config['workers'][0]['model']=='gpt-6-astra'
            assert manager.states[name]['token']==token
            assert manager.active_sessions[name].process.pid==pid
            assert read(tmp_path/name/'.runtime/live_input.json')['pid']==native['pid']
        if supervisor:
            # The actual Codex app-server stays alive but makes no inference
            # requests during an explicit supervisor wait. A queued message
            # wakes the same conversation, before the 30-minute deadline.
            wait_path=tmp_path/name/'.runtime/supervisor-wait.json'
            save(wait_path,{'until':time.time()+1800})
            baseline=len(calls);deadline=time.monotonic()+6
            while time.monotonic()<deadline:
                manager.poll_cli_jobs();time.sleep(.03)
            assert len(calls)==baseline and session.process.poll() is None
            wake_number=mail.add('RESEARCH_PIVOT_SENTINEL: wake the waiting supervisor.')
            wait_for(manager,lambda:next(m for m in mail.list() if m['id']==wake_number)['status']=='delivered')
            assert not wait_path.exists() and manager.states[name]['token']==token
        # Recover the same native conversation after an ordinary CLI failure.
        manager.pause_worker(name)
        manager.states[name]['next']=0
        if kind=='gemini':
            save(tmp_path/'run.json',{**config,'gemini_context_tokens':300000})
            profile=tmp_path/name/'.home/.gemini/antigravity-cli/settings.json'
            original_build=manager._build_cli_job_launch_spec
            def old_controller_profile(*args):
                spec=original_build(*args)
                settings=read(profile)
                settings['customModelsConfig']['customModels']['gemini-api://'+config['workers'][0]['model']]['maxTokens']=131072
                save(profile,settings)
                return spec
            monkeypatch.setattr(manager,'_build_cli_job_launch_spec',old_controller_profile)
        third=mail.add('RESEARCH_PIVOT_SENTINEL: RESUME_MESSAGE_FIRST: survive pause and resume.')
        fourth=mail.add('RESEARCH_PIVOT_SENTINEL: NEWEST_PRIORITY_SAME_INPUT') if kind in {'claude','gemini'} else None
        resumed_request=len(calls)
        assert manager.launch_cli_job(name)
        wait_for(manager,lambda:next(m for m in mail.list() if m['id']==third)['status']=='delivered')
        assert manager.states[name]['token']==token
        if kind=='codex':
            expected='gpt-6-astra' if supervisor else 'gpt-6.2-astra'
            wait_for(manager,lambda:any(c['model']==expected for c in calls[resumed_request:]))
            assert calls[resumed_request]['model']==expected
            assert calls[resumed_request]['reasoning']['effort']=='max'
        if kind in {'claude','gemini'}:
            assert 'RESUME_MESSAGE_FIRST' in json.dumps(calls[resumed_request])
            assert 'NEWEST_PRIORITY_SAME_INPUT' in json.dumps(calls[resumed_request])
            wait_for(manager,lambda:next(m for m in mail.list() if m['id']==fourth)['status']=='delivered')
        if kind=='gemini':
            assert read(profile)['customModelsConfig']['customModels']['gemini-api://'+config['workers'][0]['model']]['maxTokens']==300000
            logs=list((tmp_path/name/'.home/.gemini/antigravity-cli/log').glob('*.log'))
            assert any('max_tokens:300000' in p.read_text() or 'max_tokens: 300000' in p.read_text() for p in logs)
        if kind in {'claude','gemini'}:
            # A provider-forced fresh session must receive its full assignment
            # and the backlog in its first actual model request, not after an
            # arbitrarily long initial research turn.
            manager.pause_worker(name)
            fresh=tmp_path/'fresh';save(fresh/'run.json',config);prepare_workspaces(fresh,config)
            fresh_mail=Messages(fresh/name)
            fresh_mail.add('FRESH_FIRST_GUIDANCE_SENTINEL')
            fresh_mail.add('FRESH_LATEST_GUIDANCE_SENTINEL')
            fresh_manager=ResearchManager(fresh,config,env)
            first_request=len(calls)
            try:
                assert fresh_manager.launch_cli_job(name)
                wait_for(fresh_manager,lambda:all(m['status']=='delivered' for m in fresh_mail.list()))
                body=calls[first_request]
                assert 'FRESH_FIRST_GUIDANCE_SENTINEL' in json.dumps(body)
                assert 'FRESH_LATEST_GUIDANCE_SENTINEL' in json.dumps(body)
                assignment=(fresh/name/'.runtime/launch_000001/prompt.txt').read_text()
                def strings(value):
                    if isinstance(value,str):yield value
                    elif isinstance(value,dict):
                        for child in value.values():yield from strings(child)
                    elif isinstance(value,list):
                        for child in value:yield from strings(child)
                assert any(assignment in value for value in strings(body))
            finally:
                fresh_manager.close()
    finally:
        release.set();manager.close();api.shutdown();api.server_close();thread.join(timeout=5)

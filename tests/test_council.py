"""Council session persistence, failures, real concurrent writes and default isolation."""
import concurrent.futures
import json
import multiprocessing
import os
import signal
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import save, read, initial_prompt, alive
from main import parser, build_config
from council import Store, configure, enabled, load_input, worker_cli, start_service
from council_service import Engine, CouncilJob
from messages import Messages


def fake_cli(tmp_path,kind):
    path=tmp_path/('fake-'+kind)
    path.write_text('#!'+sys.executable+'\n'+'''
import json,os,sys,pathlib,time
kind=pathlib.Path(sys.argv[0]).name.split('-')[-1]
root=pathlib.Path(os.environ['CODEX_HOME']).parent if kind=='codex' and os.environ.get('CODEX_HOME') else pathlib.Path.cwd().parent
counter=root/'calls.json'
calls=json.loads(counter.read_text()) if counter.exists() else []
prompt=sys.argv[sys.argv.index('-p')+1] if kind=='gemini' else sys.stdin.read()
calls.append({'argv':sys.argv,'prompt':prompt,'key':os.environ.get({'codex':'CODEX_API_KEY','claude':'ANTHROPIC_API_KEY','gemini':'GEMINI_API_KEY'}[kind]),'env':list(os.environ)})
counter.write_text(json.dumps(calls))
token='native-session'
event={'codex':{'type':'thread.started','thread_id':token},'claude':{'type':'system','session_id':token},'gemini':{'conversation_id':token}}[kind]
print(json.dumps(event),flush=True)
if (root/'hang').exists():time.sleep(60)
if (root/'fail').exists():
 print(json.dumps({'type':'error','message':'session not found' if (root/'missing').exists() else 'connection failed'}),flush=True)
 sys.exit(1)
answer='Consultation '+str(len(calls))+': '+prompt[-100:]
if kind=='codex':
 print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':answer}}),flush=True)
 print(json.dumps({'type':'turn.completed'}),flush=True)
elif kind=='claude':print(json.dumps({'type':'result','result':answer,'session_id':token,'is_error':False}),flush=True)
else:print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':answer,'conversation_id':token}}),flush=True)
''')
    path.chmod(0o755);return str(path)


def setup_run(tmp_path):
    args=parser().parse_args(['start','test_problem','--name','council_test','--codex','1','--claude','1','--gemini','1'])
    config=build_config(args,{'category':'open_ended'})
    run=tmp_path/'run';run.mkdir()
    save(run/'run.json',config)
    for w in config['workers']:(run/w['name']).mkdir()
    env={'PATH':os.environ['PATH'],'HOME':str(tmp_path),'UNRELATED_SECRET':'should-not-leak'}
    for kind,var in [('codex','CODEX_BIN_PATH'),('claude','CLAUDE_BIN_PATH'),('gemini','ANTIGRAVITY_BIN_PATH')]:
        env[var]=fake_cli(tmp_path,kind)
        env['RSTATION_WORKER_'+kind.upper()+'_API_KEY']='test-primary'
        env['RSTATION_WORKER_'+kind.upper()+'_BASE_URL']='http://127.0.0.1:1'
        env['RSTATION_WORKER_'+kind.upper()+'_BACKUP_API_KEY']='test-backup'
    return run,config,env


def activate(run,env,**kwargs):
    configure(run,'all',True,env,statement='Task: use Thinker; no alternative RL approach.',notify=False,**kwargs)


def drain(engine,condition,seconds=8):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        engine.tick()
        if condition():return
        time.sleep(.02)
    raise AssertionError(engine.store.snapshot())


def turn(store,key,number=1):return store.snapshot()['sessions'][key]['turns'][number-1]


def test_default_and_supervisor_never_enabled(tmp_path):
    run,c,env=setup_run(tmp_path)
    assert c['council']
    c['council']=False
    c['coder']=False
    for worker in c['workers']:
        assert not enabled(c,worker)
        assert './council' not in initial_prompt(c,worker,run/worker['name'])
    assert not (run/'.council').exists()


def test_council_setup_failure_fails_turn_without_killing_engine(tmp_path, monkeypatch):
    run,c,env=setup_run(tmp_path);activate(run,env)
    import council_service
    original=council_service.CouncilJob._build_cli_job_launch_spec
    def broken(self,*args):
        if self.key.endswith(':bad'):
            raise ValueError('private-route-key')
        return original(self,*args)
    monkeypatch.setattr(council_service.CouncilJob,'_build_cli_job_launch_spec',broken)
    store=Store(run);store.submit('codex_1','bad','Give advice.',family='gemini')
    engine=Engine(run,env)
    try:
        drain(engine,lambda:turn(store,'codex_1:bad')['status']=='failed')
        assert 'private-route-key' not in turn(store,'codex_1:bad')['error']
        store.submit('codex_1','good','Give advice.',family='gemini')
        drain(engine,lambda:turn(store,'codex_1:good')['status']=='complete')
    finally:engine.close()
    c.update(role='supervisor',council=True);save(run/'run.json',c)
    assert not enabled(c,{'council':True})
    with pytest.raises(ValueError,match='Supervisors'):configure(run,'all',True,env)
    with pytest.raises(ValueError,match='Supervisors'):Store(run).submit('codex_1','x','hello',family='gemini')
    with pytest.raises(ValueError,match='Supervisors'):Engine(run,env)


@pytest.mark.parametrize('kind,worker',[('codex','claude_1'),('claude','gemini_1'),('gemini','codex_1')])
def test_native_lifecycle_followup_recovery_and_notifications(tmp_path,kind,worker):
    run,c,env=setup_run(tmp_path);activate(run,env);store=Store(run)
    key=worker+':idea'
    assert store.submit(worker,'idea','Remember nonce ORCHID.',family=kind)==1
    engine=Engine(run,env)
    try:
        drain(engine,lambda:turn(store,key)['status']=='complete')
        native=store.root/worker/'idea'/'native.json'
        assert read(native)['token']=='native-session'
        assert store.submit(worker,'idea','Remember nonce ORCHID.',family=kind)==1
        assert store.resume(worker,'idea')==1
        assert store.submit(worker,'idea','What was the nonce?')==2
        drain(engine,lambda:turn(store,key,2)['status']=='complete')
        calls=read(native.parent/'calls.json')
        assert len(calls)==2
        assert 'native-session' in calls[1]['argv']
        assert 'Task: use Thinker' in calls[0]['prompt']
        assert calls[1]['prompt'].endswith('What was the nonce?')
        assert str((run/worker).resolve()) in calls[1]['prompt']
        assert 'read-only file tools' in calls[1]['prompt']
        assert all('UNRELATED_SECRET' not in call['env'] for call in calls)
        engine.tick();engine.tick()
        assert len(Messages(run/worker).list())==2
    finally:engine.close()


def test_family_model_validation_and_disabled_history(tmp_path,capsys):
    run,c,env=setup_run(tmp_path);store=Store(run)
    with pytest.raises(ValueError,match='disabled'):store.submit('codex_1','idea','brief',family='gemini')
    activate(run,env)
    with pytest.raises(ValueError,match='different model family'):store.submit('codex_1','idea','brief',family='codex')
    with pytest.raises(ValueError,match='unavailable'):store.submit('codex_1','idea','brief',family='gemini',model='unapproved')
    store.submit('codex_1','idea','brief',family='gemini')
    with pytest.raises(ValueError,match='different family'):store.submit('codex_1','idea','brief',family='claude')
    configure(run,'codex_1',False,env,notify=False)
    with pytest.raises(ValueError,match='disabled'):store.submit('codex_1','idea','new')
    worker_cli(run,'codex_1',['read','idea','--all']);assert 'brief' in capsys.readouterr().out
    with pytest.raises(ValueError,match='not found'):worker_cli(run,'claude_1',['read','idea'])
    assert './council' not in (run/'codex_1/RESEARCH_INSTRUCTIONS.md').read_text()


def submit_process(args):
    run,n=args
    return Store(run).submit('codex_1','idea','parallel question '+str(n))


def test_real_concurrent_submissions_and_idempotency(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env,limits={'max_pending':30});store=Store(run)
    store.submit('codex_1','idea','brief',family='gemini')
    with concurrent.futures.ProcessPoolExecutor(8,mp_context=multiprocessing.get_context('fork')) as pool:
        numbers=list(pool.map(submit_process,[(run,n) for n in range(20)]))
    assert sorted(numbers)==list(range(2,22))
    rows=store.snapshot()['sessions']['codex_1:idea']['turns']
    assert len(rows)==21 and len({r['digest'] for r in rows})==21
    with concurrent.futures.ProcessPoolExecutor(8,mp_context=multiprocessing.get_context('fork')) as pool:
        numbers=list(pool.map(submit_process,[(run,999)]*12))
    assert len(set(numbers))==1


def test_attachments_confined_and_snapshotted(tmp_path):
    run,c,env=setup_run(tmp_path);work=run/'codex_1'
    (work/'brief.md').write_text('hypothesis');(work/'data.csv').write_text('a,b\n1,2')
    result=load_input(work,None,work/'brief.md',[work/'data.csv'])
    assert 'hypothesis' in result and '1,2' in result
    (work/'private').symlink_to(tmp_path)
    with pytest.raises(ValueError,match='within your workspace'):load_input(work,None,work/'private/fake-codex',[])
    (work/'.env').write_text('secret')
    with pytest.raises(ValueError,match='credentials'):load_input(work,None,work/'.env',[])
    with pytest.raises(ValueError,match='new prompt'):load_input(work,None,None,[work/'data.csv'])


def test_failover_same_session_then_next_turn_primary(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env);store=Store(run)
    store.submit('codex_1','idea','brief',family='gemini')
    home=store.root/'codex_1/idea';home.mkdir(parents=True);(home/'fail').touch()
    engine=Engine(run,env)
    try:
        for attempt in (1,2):
            drain(engine,lambda:read(home/'native.json',{}).get('attempts')==attempt and read(home/'native.json',{}).get('status')=='retrying')
            job=engine.jobs['codex_1:idea'];job.state['next']=0;job.persist()
        (home/'fail').unlink()
        drain(engine,lambda:turn(store,'codex_1:idea')['status']=='complete')
        assert turn(store,'codex_1:idea')['provider']==2
        store.submit('codex_1','idea','follow-up')
        drain(engine,lambda:turn(store,'codex_1:idea',2)['status']=='complete')
        calls=read(home/'calls.json')
        assert [x['key'] for x in calls]==['test-primary','test-primary','test-backup','test-primary']
        assert all('native-session' in x['argv'] for x in calls[1:])
    finally:engine.close()


def test_cancel_and_engine_restart_preserve_session(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env);store=Store(run)
    store.submit('codex_1','idea','brief',family='claude')
    home=store.root/'codex_1/idea';home.mkdir(parents=True);(home/'hang').touch()
    engine=Engine(run,env)
    drain(engine,lambda:read(home/'native.json',{}).get('token')=='native-session')
    engine.close();(home/'hang').unlink()
    engine=Engine(run,env)
    try:
        drain(engine,lambda:turn(store,'codex_1:idea')['status']=='complete')
        assert read(home/'native.json')['token']=='native-session'
        (home/'hang').touch();store.submit('codex_1','idea','next question')
        drain(engine,lambda:read(home/'native.json',{}).get('turn')==2 and read(home/'native.json',{}).get('status')=='running')
        store.cancel('codex_1','idea')
        drain(engine,lambda:turn(store,'codex_1:idea',2)['status']=='cancelled')
        assert read(home/'native.json')['token']=='native-session'
    finally:engine.close()


def test_missing_session_no_fresh_conversation(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env);store=Store(run)
    store.submit('codex_1','idea','brief',family='gemini')
    home=store.root/'codex_1/idea';home.mkdir(parents=True)
    (home/'fail').touch();(home/'missing').touch();engine=Engine(run,env)
    try:
        drain(engine,lambda:turn(store,'codex_1:idea')['status']=='failed')
        assert turn(store,'codex_1:idea')['error']=='session_unavailable'
        with pytest.raises(ValueError,match='unavailable'):store.resume('codex_1','idea')
        assert len(read(home/'calls.json'))==1
    finally:engine.close()


def test_pending_turn_order_and_limit(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env,limits={'max_pending':2});store=Store(run)
    store.submit('codex_1','idea','first',family='gemini');store.submit('codex_1','idea','second')
    with pytest.raises(ValueError,match='full'):store.submit('codex_1','idea','third')
    engine=Engine(run,env)
    try:
        drain(engine,lambda:turn(store,'codex_1:idea',2)['status']=='complete')
        calls=read(store.root/'codex_1/idea/calls.json')
        assert calls[0]['prompt'].endswith('first') and calls[1]['prompt'].endswith('second')
    finally:engine.close()


def test_max_attempts_and_wallclock_deadline(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env,limits={'max_attempts':1,'turn_seconds':60});store=Store(run)
    store.submit('codex_1','idea','brief',family='gemini')
    home=store.root/'codex_1/idea';home.mkdir(parents=True);(home/'fail').touch();engine=Engine(run,env)
    try:
        drain(engine,lambda:turn(store,'codex_1:idea')['status']=='failed')
        assert turn(store,'codex_1:idea')['error']=='retry_limit_exhausted'
        (home/'fail').unlink();(home/'hang').touch();store.resume('codex_1','idea')
        drain(engine,lambda:'codex_1:idea' in engine.jobs)
        engine.jobs['codex_1:idea'].state['started']=time.time()-100
        engine.tick()
        assert turn(store,'codex_1:idea')['error']=='time_limit_exhausted'
    finally:engine.close()


def test_disable_does_not_launch_queued_job(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env);store=Store(run)
    store.submit('codex_1','idea','brief',family='gemini');engine=Engine(run,env)
    try:
        engine.tick()  # Constructs the job; native process has not launched.
        configure(run,'codex_1',False,env,notify=False)
        engine.tick()
        assert not engine.jobs
        assert not (store.root/'codex_1/idea/calls.json').exists()
        assert turn(store,'codex_1:idea')['status']=='queued'
    finally:engine.close()


def test_stalled_attempt_rotates_before_turn_deadline(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env,limits={'attempt_seconds':1});store=Store(run)
    store.submit('codex_1','idea','brief',family='gemini')
    home=store.root/'codex_1/idea';home.mkdir(parents=True);(home/'hang').touch();engine=Engine(run,env)
    try:
        drain(engine,lambda:read(home/'native.json',{}).get('status')=='retrying')
        assert turn(store,'codex_1:idea')['status']=='running'
        assert read(home/'native.json')['token']=='native-session'
    finally:engine.close()


def test_watchdog_recovers_killed_engine_without_duplicate_conversation(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env);store=Store(run)
    store.submit('codex_1','idea','brief',family='claude')
    home=store.root/'codex_1/idea';home.mkdir(parents=True);(home/'hang').touch()
    start_service(run,env)
    ctl=read(store.root/'controller.json');native={}
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            native=read(home/'native.json',{})
            if native.get('token'):break
            time.sleep(.05)
        assert native.get('token')=='native-session'
        original=read(store.root/'engine.json')
        os.kill(original['pid'],signal.SIGKILL)
        (home/'hang').unlink()
        deadline=time.monotonic()+20
        while time.monotonic()<deadline and turn(store,'codex_1:idea')['status']!='complete':time.sleep(.1)
        assert turn(store,'codex_1:idea')['status']=='complete'
        assert read(store.root/'engine.json')['pid']!=original['pid']
        calls=read(home/'calls.json')
        assert len(calls)==2 and 'native-session' in calls[-1]['argv']
    finally:
        os.kill(ctl['pid'],signal.SIGTERM)
        deadline=time.monotonic()+10
        while alive(ctl['pid'],ctl['token']) and time.monotonic()<deadline:time.sleep(.1)
        assert not alive(ctl['pid'],ctl['token'])


def test_forced_fallback_probe_handles_multiple_backup_proxy_fields(tmp_path):
    from live_council_probe import fallback_environment
    from recovery import routes
    run,c,env=setup_run(tmp_path)
    env.update(RSTATION_WORKER_GEMINI_BACKUP_API_KEY='backup-one;backup-two',
        RSTATION_WORKER_GEMINI_BACKUP_BASE_URL='http://127.0.0.1:2;http://127.0.0.1:3',
        RSTATION_WORKER_GEMINI_BACKUP_HTTP_PROXY=';http://127.0.0.1:4',
        RSTATION_WORKER_GEMINI_BACKUP_HTTPS_PROXY=';http://127.0.0.1:4')
    changed=fallback_environment(env,'http://127.0.0.1:5')
    for kind in ('codex','claude','gemini'):
        selected=routes(kind,changed)
        assert len(selected)==2 and selected[0]['base_url']=='http://127.0.0.1:5'
        assert selected[1]['api_key']=='test-primary'
    assert env['RSTATION_WORKER_GEMINI_BACKUP_API_KEY']=='backup-one;backup-two'


@pytest.mark.parametrize('family', ['codex','claude','gemini'])
def test_interrupted_fragments_replay_restatement_and_empty_failure(tmp_path,family):
    from council_service import response_text, assemble_responses
    def transcript(number,text,success=False):
        p=tmp_path/f'{number}.jsonl'
        if family=='codex':
            events=[{'type':'item.completed','item':{'type':'agent_message','text':text}}, {'type':'turn.completed' if success else 'turn.failed'}]
        elif family=='claude':
            events=[{'type':'assistant','message':{'content':[{'type':'thinking','thinking':'PRIVATE_REASONING'},{'type':'text','text':text}]}},
                    {'type':'result','is_error':not success,'result':text if success else 'provider error'}]
        else:
            events=[{'event':'step_update','step_update':{'step_type':'user_input','step_index':1}},
                    {'event':'step_update','step_update':{'step_type':'agent_response','step_index':2,'text_delta':text[:8]}},
                    {'event':'step_update','step_update':{'step_type':'agent_response','step_index':2,'text_delta':text[8:]}},
                    {'event':'result','result':{'status':'SUCCESS' if success else 'ERROR','response':text}}]
        p.write_text('\n'.join(map(json.dumps,events)));return p
    first=transcript(1,'Section 1. First evidence. Section 2. Mechanism.')
    duplicate=transcript(2,response_text(first))
    final=transcript(3,'Section 3. The final recommendation.',True)
    answer=assemble_responses([first,duplicate,final])
    assert answer.count('Section 1.')==1 and 'Section 2.' in answer and 'Section 3.' in answer
    assert 'provider error' not in answer and 'PRIVATE_REASONING' not in answer
    full=transcript(4,response_text(first)+' '+response_text(final),True)
    assert assemble_responses([first,full])==response_text(full)
    overlap=transcript(5,'Section 2. Mechanism. Section 3. Complete.',True)
    assert assemble_responses([first,overlap])=='Section 1. First evidence. Section 2. Mechanism. Section 3. Complete.'
    empty=transcript(6,'')
    assert assemble_responses([first,empty,final])==answer


def test_gemini_delta_only_recovery_does_not_replay_previous_native_turn(tmp_path):
    from council_service import response_text
    p=tmp_path/'transcript.jsonl'
    events=[{'event':'result','result':{'status':'SUCCESS','response':'old question'}}]
    for index,text in [(2,'tool preamble'),(4,'partial scientific answer')]:
        events += [{'event':'step_update','step_update':{'step_type':'user_input','step_index':index-1}},
                   {'event':'step_update','step_update':{'step_type':'agent_response','step_index':index,'text_delta':text}}]
    p.write_text('\n'.join(map(json.dumps,events)))
    assert response_text(p)=='partial scientific answer'


def test_answer_boundary_survives_engine_restart_and_explicit_retry(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env);store=Store(run)
    store.submit('codex_1','ideas','first question',family='gemini');key='codex_1:ideas'
    engine=Engine(run,env)
    try:drain(engine,lambda:turn(store,key)['status']=='complete')
    finally:engine.close()
    store.submit('codex_1','ideas','second question')
    session=store.snapshot()['sessions'][key]
    job=CouncilJob(store,key,session,session['turns'][1],c,env)
    assert job.state['answer_start']==2
    # An interrupted second question has an answer even if the engine dies.
    p=job.root/'attempt-000002/transcript.jsonl';p.parent.mkdir()
    p.write_text(json.dumps({'event':'result','result':{'status':'ERROR','response':'Second answer part one.'}})+'\n')
    job.state.update(launches=2,attempts=1,status='retrying');job.persist()
    restored=CouncilJob(store,key,session,session['turns'][1],c,env)
    assert restored.state['answer_start']==2
    # A legacy saved state has no explicit answer boundary yet.
    restored.state.pop('answer_start');restored.fail('retry_limit_exhausted');store.resume('codex_1','ideas')
    session=store.snapshot()['sessions'][key]
    restored=CouncilJob(store,key,session,session['turns'][1],c,env)
    assert restored.state['answer_start']==2
    p=restored.root/'attempt-000003/transcript.jsonl';p.parent.mkdir()
    p.write_text(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Second answer part two.'}})+'\n')
    restored.state.update(launches=3,attempts=1);restored.complete()
    answer=turn(store,key,2)['answer']
    assert 'part one' in answer and 'part two' in answer and 'first question' not in answer


@pytest.mark.parametrize('kind,worker',[('codex','claude_1'),('claude','gemini_1'),('gemini','codex_1')])
def test_each_adviser_receives_same_group_scope_on_fresh_and_resumed_turns(tmp_path,kind,worker):
    run,c,env=setup_run(tmp_path);c['disable_web']=True;save(run/'run.json',c)
    activate(run,env);store=Store(run);store.submit(worker,'peer','Read ../codex_1/PLAN.md.',family=kind)
    engine=Engine(run,env);key=worker+':peer'
    try:
        drain(engine,lambda:turn(store,key)['status']=='complete')
        store.submit(worker,'peer','Read the updated peer notes.')
        drain(engine,lambda:turn(store,key,2)['status']=='complete')
        calls=read(store.root/worker/'peer/calls.json')
        for call in calls:
            assert all(str((run/w['name']).resolve()) in call['prompt'] for w in c['workers'])
            assert 'other runs' in call['prompt'] and 'hidden files' in call['prompt']
        if kind=='codex':
            assert all(str((run/w['name']).resolve()) in ' '.join(calls[0]['argv']) for w in c['workers'])
    finally:engine.close()


def test_workspace_scope_rejects_other_groups_and_escaping_symlinks(tmp_path):
    from council_service import research_roots
    run,c,env=setup_run(tmp_path)
    (run/'gemini_1').rmdir();(run/'gemini_1').symlink_to(tmp_path/'other_group')
    with pytest.raises(ValueError,match='direct directory'):research_roots(run,c,'claude_1')
    c['workers']=[{'name':'../other_group'}]
    with pytest.raises(ValueError,match='direct directory'):research_roots(run,c,'claude_1')

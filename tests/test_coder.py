"""Coder model pins, isolated changes, helper recovery and skill lifecycle."""
import json
import multiprocessing
import os
from pathlib import Path
import signal
import sys
import time

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import read,save,alive,initial_prompt
from coder import Store,configure,worker_cli
from coder_service import Engine
from coder_workspace import prepare,changes
from capabilities import install_skills,ensure_services,health,restore
from main import build_config,parser
from test_council import setup_run,activate,drain,turn


def test_defaults_optout_and_skills_follow_configuration(tmp_path):
    run,c,env=setup_run(tmp_path)
    assert c['coder'] and c['council'] and c['coder_model']=='gpt-6.2-sol'
    for w in c['workers']:
        work=run/w['name'];(work/'AGENTS.md').write_text('Existing research notes.\n')
        install_skills(run,c,w);install_skills(run,c,w)
        assert (work/'AGENTS.md').read_text().count('rstation research instructions -->')==2
        assert (work/'AGENTS.md').read_text().startswith('Existing research notes.')
        for name in ('coder','council'):
            assert (work/'.agents/skills'/name/'SKILL.md').exists()
            assert (work/'.codex/skills'/name/'SKILL.md').samefile(work/'.agents/skills'/name/'SKILL.md')
            assert str(work/'.agents/skills'/name/'SKILL.md') in (work/'AGENTS.md').read_text()
            assert str(work/'AGENTS.md') in initial_prompt(c,w,work)
        w.update(coder=False,council=False);install_skills(run,c,w)
        assert not (work/'.agents/skills/coder/SKILL.md').exists()
        assert '.agents/skills/' not in (work/'AGENTS.md').read_text()
    c=build_config(parser().parse_args(['start','test','--no-coder','--no-council']),{'category':'open_ended'})
    assert not c['coder'] and not c['council']


def test_coder_all_parent_families_pin_model_and_followup(tmp_path,monkeypatch):
    monkeypatch.setattr(Store,"session_fields",lambda *args: {"workspace_mode":"snapshot"})
    run,c,env=setup_run(tmp_path);configure(run,'all',True,env,notify=False)
    store=Store(run);engine=Engine(run,env)
    try:
        for w in c['workers']:
            name=w['name'];(run/name/'code.py').write_text('value=1\n')
            store.submit(name,'task','Implement a check.',path='.')
        drain(engine,lambda:all(t['turns'][-1]['status']=='complete' for t in store.snapshot()['sessions'].values()))
        for w in c['workers']:
            name=w['name'];root=store.root/name/'task';state=read(root/'native.json');token=state['token']
            assert token
            call=read(root/'calls.json')[-1];assert call['argv'][call['argv'].index('--model')+1]=='gpt-6.2-sol'
            assert '--search' in call['argv']
            assert call['argv'][call['argv'].index('--sandbox')+1]=='danger-full-access'
            assert 'UNRELATED_SECRET' not in call['env']
            (root/'workspace/code.py').write_text('value=2\n')
            assert (run/name/'code.py').read_text()=='value=1\n'
            assert '+value=2' in store.changes(name,'task')
            assert store.changes(name,'task',apply=True)=='Applied 1 changed files.'
            assert (run/name/'code.py').read_text()=='value=2\n'
            store.submit(name,'task','Add another check.')
            drain(engine,lambda:turn(store,name+':task',2)['status']=='complete')
            assert read(root/'native.json')['token']==token
        configure(run,'all',True,env,model='gpt-6-astra',notify=False)
        store.submit('codex_1','newtask','Other implementation.',path='.')
        assert store.snapshot()['sessions']['codex_1:newtask']['model']=='gpt-6-astra'
        assert store.snapshot()['sessions']['codex_1:task']['model']=='gpt-6.2-sol'
        with pytest.raises(ValueError,match='operator'):store.submit('codex_1','bad','test',model='claude-opus-5-5')
    finally:engine.close()


def test_snapshot_conflicts_private_files_and_symlink_escape(tmp_path):
    root=tmp_path/'task';root.mkdir();work=tmp_path/'worker';work.mkdir()
    (work/'code.py').write_text('before\n');(work/'.env').write_text('private')
    (work/'credentials.json').write_text('private')
    outside=tmp_path/'outside';outside.mkdir();(outside/'other.py').write_text('untouched')
    (work/'linked').symlink_to(outside,target_is_directory=True)
    prepare(root,work,'Implement code.')
    scratch=root/'workspace';assert not (scratch/'.env').exists();assert not (scratch/'credentials.json').exists()
    assert not (scratch/'linked').exists()
    (scratch/'code.py').write_text('coder\n');(scratch/'new.py').write_text('new\n')
    (work/'code.py').write_text('researcher\n')
    with pytest.raises(ValueError,match='changed since'):changes(root,apply=True)
    assert not (work/'new.py').exists() and (work/'code.py').read_text()=='researcher\n'
    (work/'code.py').write_text('before\n');(work/'new.py').symlink_to(outside/'other.py')
    with pytest.raises(ValueError,match='symlink'):changes(root,apply=True)
    assert (outside/'other.py').read_text()=='untouched'


@pytest.mark.parametrize('failure', ['oversize', 'oserror', 'valueerror'])
def test_coder_setup_failure_is_terminal_and_other_tasks_continue(tmp_path, monkeypatch, failure):
    import coder_workspace
    import coder_service
    monkeypatch.setattr(Store,'session_fields',lambda *args: {'workspace_mode':'snapshot'})
    run,c,env=setup_run(tmp_path);configure(run,'all',True,env,notify=False)
    work=run/'codex_1';small=work/'code';small.mkdir()
    (small/'value.py').write_text('value=1\n')
    if failure=='oversize':
        monkeypatch.setattr(coder_workspace,'LIMIT',1024)
        (work/'large.dat').write_bytes(b'x'*1025)
    else:
        original=coder_service.prepare
        def broken(root,work,prompt):
            if Path(root).name=='bad':
                raise (OSError if failure=='oserror' else ValueError)('private-route-key')
            return original(root,work,prompt)
        monkeypatch.setattr(coder_service,'prepare',broken)
    store=Store(run);engine=Engine(run,env)
    try:
        store.submit('codex_1','bad','Implement a check.',path='.')
        drain(engine,lambda:turn(store,'codex_1:bad')['status']=='failed')
        failed=turn(store,'codex_1:bad')
        assert failed['finished'] and failed['notified']
        assert 'private-route-key' not in failed['error']
        assert ('smaller --path' if failure=='oversize' else 'launch setup failed') in failed['error']
        assert not (store.root/'codex_1/bad/calls.json').exists()
        store.submit('codex_1','valid','Implement a check.',path='code')
        drain(engine,lambda:turn(store,'codex_1:valid')['status']=='complete')
        assert turn(store,'codex_1:bad')['status']=='failed'
        assert read(store.root/'codex_1/bad/native.json')['attempts']==1
        assert (work/'code/value.py').read_text()=='value=1\n'
    finally:engine.close()


def test_restore_missing_council_and_preserve_native_state(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env)
    from council import Store as CouncilStore
    store=CouncilStore(run);store.submit('codex_1','advice','Question',family='claude')
    native=store.root/'codex_1/advice/native.json';save(native,dict(token='saved-native',pid=999999,pid_token='old'))
    target=tmp_path/'migrated';target.mkdir();save(target/'run.json',c)
    for w in c['workers']:(target/w['name']).mkdir()
    restore(target,'council',run)
    assert CouncilStore(target).snapshot()==store.snapshot()
    restored=read(target/'.council/codex_1/advice/native.json')
    assert restored['token']=='saved-native' and restored['pid'] is None
    with pytest.raises(ValueError,match='already has'):restore(target,'council',run)


def test_coder_recovers_exhausted_legacy_setup_without_waiting_for_timeout(tmp_path):
    run,c,env=setup_run(tmp_path);configure(run,'all',True,env,notify=False)
    store=Store(run);store.submit('codex_1','stuck','Implement a check.',path='.')
    engine=Engine(run,env);engine.tick()
    job=engine.jobs['codex_1:stuck']
    job.state.update(status='starting',attempts=job.policy['max_attempts'],pid=None)
    job.persist();store.update_turn(job.key,1,status='running')
    engine.close()
    recovered=Engine(run,env)
    try:
        drain(recovered,lambda:turn(store,job.key)['status']=='failed')
        assert turn(store,job.key)['error']=='retry_limit_exhausted'
        assert turn(store,job.key)['notified']
        assert not (job.root/'calls.json').exists()
        store.submit('codex_1','next','Implement a check.',path='.')
        drain(recovered,lambda:turn(store,'codex_1:next')['status']=='complete')
    finally:recovered.close()


def test_controller_restarts_missing_council_service(tmp_path):
    run,c,env=setup_run(tmp_path);c['coder']=False;save(run/'run.json',c);activate(run,env)
    from council import Store as CouncilStore
    store=CouncilStore(run);store.submit('codex_1','queued','Advice please',family='claude')
    try:
        assert ensure_services(run,env)=={}
        deadline=time.time()+10
        while time.time()<deadline and turn(store,'codex_1:queued')['status']!='complete':time.sleep(.1)
        assert turn(store,'codex_1:queued')['status']=='complete'
        ctl=read(store.root/'controller.json');os.kill(ctl['pid'],signal.SIGTERM)
        deadline=time.time()+10
        while alive(ctl['pid'],ctl['token']) and time.time()<deadline:time.sleep(.1)
        assert ensure_services(run,env)=={}
        assert read(store.root/'controller.json')['pid']!=ctl['pid']
    finally:
        ctl=read(store.root/'controller.json')
        if alive(ctl.get('pid'),ctl.get('token')):os.kill(ctl['pid'],signal.SIGTERM)


def test_missing_policy_is_visible_without_erasing_saved_history(tmp_path):
    run,c,env=setup_run(tmp_path)
    save(run/'.council/store.json',{'sessions':{'saved':{'worker':'codex_1','turns':[]}}})
    errors=ensure_services(run,env)
    assert set(errors)=={'coder','council'}
    assert 'saved' in read(run/'.council/store.json')['sessions']


def _submit_coder_task(run, index):
    return Store(run).submit('codex_1','shared','Implement one task.',path='.')


def test_coder_concurrent_submission_and_read_receipt(tmp_path,capsys):
    from concurrent.futures import ProcessPoolExecutor
    run,c,env=setup_run(tmp_path);configure(run,'all',True,env,notify=False)
    with ProcessPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(_submit_coder_task,[run]*12,range(12)))==[1]*12
    store=Store(run);engine=Engine(run,env)
    try:
        drain(engine,lambda:turn(store,'codex_1:shared')['status']=='complete')
        assert not turn(store,'codex_1:shared').get('read_at')
        worker_cli(run,'codex_1',['read','shared'])
        assert turn(store,'codex_1:shared')['read_at']
        assert len(store.snapshot()['sessions'])==1
    finally:engine.close()


@pytest.mark.parametrize('capability',['coder','council'])
def test_helpers_pause_stop_and_cancel_preserve_conversation(tmp_path,capability):
    import importlib
    run,c,env=setup_run(tmp_path)
    module=importlib.import_module(capability)
    if capability=='coder':configure(run,'all',True,env,notify=False)
    else:activate(run,env)
    store=module.Store(run);kind='codex' if capability=='coder' else 'claude'
    store.submit('codex_1','long','Wait for followup.',family=kind)
    root=store.root/'codex_1/long';root.mkdir(parents=True);(root/'hang').touch()
    engine=importlib.import_module(capability+'_service').Engine(run,env)
    try:
        drain(engine,lambda:(root/'calls.json').exists())
        # Capture a real native session before the pause interrupts its process.
        drain(engine,lambda:bool(read(root/'native.json').get('token')))
        token=read(root/'native.json')['token']
        pause=run/'codex_1/.runtime/PAUSE';pause.parent.mkdir(exist_ok=True);pause.touch()
        engine.tick();assert not engine.jobs
        engine.tick();assert len(read(root/'calls.json'))==1
        pause.unlink();(root/'hang').unlink()
        drain(engine,lambda:turn(store,'codex_1:long')['status']=='complete')
        assert read(root/'native.json')['token']==token
        store.submit('codex_1','cancelled','Do not run this.',family=kind)
        store.cancel('codex_1','cancelled');engine.tick()
        assert turn(store,'codex_1:cancelled')['status']=='cancelled'
        store.submit('codex_1','stopped','Wait.',family=kind)
        (run/'STOP').touch();engine.tick();assert not engine.jobs
        assert turn(store,'codex_1:stopped')['status']=='queued'
    finally:engine.close()


def test_snapshot_detects_source_edits_during_copy(tmp_path,monkeypatch):
    import coder_workspace as workspace
    root=tmp_path/'task';root.mkdir();work=tmp_path/'worker';work.mkdir()
    (work/'a.py').write_text('original')
    original=workspace.shutil.copy2
    def changing(source,target):
        result=original(source,target);Path(source).write_text('new researcher edit');return result
    monkeypatch.setattr(workspace.shutil,'copy2',changing)
    with pytest.raises(ValueError,match='taking the snapshot'):prepare(root,work,'Code')
    assert not (root/'files.json').exists()
    assert (work/'a.py').read_text()=='new researcher edit'


def test_apply_rolls_back_only_its_writes_on_mid_apply_conflict(tmp_path,monkeypatch):
    import coder_workspace as workspace
    root=tmp_path/'task';root.mkdir();work=tmp_path/'worker';work.mkdir()
    for n in ('a.py','b.py'):(work/n).write_text('original')
    prepare(root,work,'Code')
    for n in ('a.py','b.py'):(root/'workspace'/n).write_text('coder')
    write=workspace.write_file
    def competing(path,content,mode):
        write(path,content,mode)
        if path.name=='a.py':(work/'b.py').write_text('researcher')
    monkeypatch.setattr(workspace,'write_file',competing)
    with pytest.raises(ValueError,match='changed since'):changes(root,apply=True)
    assert (work/'a.py').read_text()=='original'
    assert (work/'b.py').read_text()=='researcher'


def test_restore_checks_all_target_history_before_moving(tmp_path):
    run,c,env=setup_run(tmp_path);activate(run,env)
    from council import Store as CouncilStore
    store=CouncilStore(run)
    for worker in ('codex_1','claude_1'):
        store.submit(worker,'saved','Question',family='gemini')
        save(store.root/worker/'saved/native.json',dict(token='saved',pid=None))
    target=tmp_path/'target';target.mkdir();save(target/'run.json',c)
    existing=target/'.council/claude_1/keep.txt';existing.parent.mkdir(parents=True);existing.write_text('keep')
    with pytest.raises(ValueError,match='saved helper files'):restore(target,'council',run)
    assert not (target/'.council/codex_1').exists()
    assert existing.read_text()=='keep'


def test_persistent_assignment_survives_reinstall_without_repeating_resume(tmp_path):
    run,c,env=setup_run(tmp_path);w=c['workers'][0];work=run/w['name']
    c.update(instructions='Use the frozen baseline in /data/baseline.',disable_web=True)
    w['invitation_code']='private-worker-invitation'
    save(run/'run.json',c)
    (work/'AGENTS.md').write_text('Existing scientific notes.\n')
    install_skills(run,c,w);first=(work/'AGENTS.md').read_text()
    assert 'frozen baseline' in first and 'Do not use external websites' in first
    assert 'private-worker-invitation' not in first
    assert 'private-worker-invitation' in initial_prompt(c,w,work)
    for filename in ('CLAUDE.md','GEMINI.md'):
        assert 'Read AGENTS.md' in (work/filename).read_text()
        assert 'frozen baseline' not in (work/filename).read_text()
    install_skills(run,c,w)
    assert (work/'AGENTS.md').read_text()==first
    assert first.startswith('Existing scientific notes.')
    # A later config change refreshes durable access rules without losing notes.
    c['instructions']='Use the new owner-approved baseline in /data/new.'
    install_skills(run,c,w)
    text=(work/'AGENTS.md').read_text()
    assert 'frozen baseline' not in text and '/data/new' in text
    assert text.startswith('Existing scientific notes.')


@pytest.mark.parametrize('worker',['codex_1','claude_1','gemini_1'])
def test_direct_coder_edits_large_workspace_in_existing_python_environment(tmp_path,worker):
    import subprocess
    run,c,env=setup_run(tmp_path);configure(run,'all',True,env,notify=False)
    work=run/worker
    # The research data is larger than the old copy cap; it must stay in place.
    with (work/'dataset.bin').open('wb') as f:f.truncate(65*1024*1024)
    subprocess.run([sys.executable,'-m','venv','--without-pip',str(work/'.venv')],check=True)
    python=work/'.venv/bin/python'
    library=subprocess.check_output([str(python),'-c','import sysconfig;print(sysconfig.get_path("purelib"))'],text=True).strip()
    Path(library,'research_dependency.py').write_text('VALUE=42\n')
    native=tmp_path/'direct-native'
    native.write_text('#!'+sys.executable+'\n'+'''import os,json,pathlib,subprocess,sys
root=pathlib.Path(os.environ['CODEX_HOME']).parent
prompt=sys.stdin.read()
value=subprocess.check_output(['python','-c','import research_dependency;print(research_dependency.VALUE)'],text=True).strip()
pathlib.Path('result.py').write_text('answer='+value+'\\n')
(root/'observed.json').write_text(json.dumps({'cwd':str(pathlib.Path.cwd()),'home':os.environ['HOME'],'venv':os.environ['VIRTUAL_ENV'],'argv':sys.argv,'env':list(os.environ)}))
print(json.dumps({'type':'thread.started','thread_id':'direct-native-session'}),flush=True)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Edited result.py; dependency test passed.'}}),flush=True)
print(json.dumps({'type':'turn.completed'}),flush=True)
''');native.chmod(0o755);env['CODEX_BIN_PATH']=str(native)
    store=Store(run);store.submit(worker,'direct','Implement result.',path='.')
    engine=Engine(run,env)
    try:
        drain(engine,lambda:turn(store,worker+':direct')['status']=='complete')
        root=store.root/worker/'direct';observed=read(root/'observed.json')
        assert observed['cwd']==str(work.resolve())
        assert observed['home']==str(work/'.home') and observed['venv']==str(work/'.venv')
        assert 'UNRELATED_SECRET' not in observed['env']
        assert '--search' in observed['argv'] and 'gpt-6.2-sol' in observed['argv']
        assert (work/'result.py').read_text()=='answer=42\n'
        assert not (root/'workspace').exists() and not (root/'files.json').exists()
        assert 'no apply step' in store.changes(worker,'direct',apply=True)
        store.submit(worker,'direct','Check the implementation again.')
        drain(engine,lambda:turn(store,worker+':direct',2)['status']=='complete')
        assert read(root/'native.json')['token']=='direct-native-session'
    finally:engine.close()


@pytest.mark.parametrize('has_session',[True,False])
@pytest.mark.parametrize('failure',[
    'HTTP 401 Unauthorized', 'HTTP 429 Too Many Requests', 'HTTP 503 Service Unavailable',
    'connection reset by peer', 'Transport error: timeout', 'DNS resolution failed',
])
def test_coder_api_and_connection_failures_rotate_and_resume_past_eight_attempts(tmp_path,monkeypatch,failure,has_session):
    from coder_service import CoderJob
    monkeypatch.setattr(CoderJob,'_resume_backoff_schedule',staticmethod(lambda:[0]))
    run,c,env=setup_run(tmp_path)
    native=tmp_path/'retry-native'
    native.write_text('#!'+sys.executable+'\n'+'''
import json,os,pathlib,sys
root=pathlib.Path(os.environ['CODEX_HOME']).parent
sys.stdin.read()
p=root/'calls.json';calls=json.loads(p.read_text()) if p.exists() else []
calls.append({'argv':sys.argv,'key':os.environ['CODEX_API_KEY'],'cwd':os.getcwd()});p.write_text(json.dumps(calls))
if HAS_SESSION or len(calls)>9:print(json.dumps({'type':'thread.started','thread_id':'saved-coder-session'}),flush=True)
if len(calls)<=9:
 print(json.dumps({'type':'error','message':FAILURE}),flush=True)
 sys.exit(1)
pathlib.Path('coded.txt').write_text('complete after rotation')
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Implemented and checked.'}}),flush=True)
print(json.dumps({'type':'turn.completed'}),flush=True)
'''.replace('FAILURE',repr(failure)).replace('HAS_SESSION',repr(has_session)))
    native.chmod(0o755);env['CODEX_BIN_PATH']=str(native)
    policy=configure(run,'all',True,env,notify=False)
    assert policy['turn_seconds']==7200
    store=Store(run);store.submit('claude_1','retry','Implement the task directly.')
    engine=Engine(run,env);root=store.root/'claude_1/retry'
    try:
        drain(engine,lambda:read(root/'native.json',{}).get('attempts',0)>=5)
        engine.close()  # Recovery must keep routing and the same saved conversation.
        engine=Engine(run,env)
        drain(engine,lambda:turn(store,'claude_1:retry')['status']=='complete')
        calls=read(root/'calls.json')
        assert len(calls)>=10
        assert [x['key'] for x in calls[:4]]==['test-primary','test-primary','test-backup','test-backup']
        assert all(('saved-coder-session' in x['argv'])==has_session for x in calls[1:])
        assert all(x['cwd']==str(run/'claude_1') for x in calls)
        assert read(root/'native.json')['token']=='saved-coder-session'
        assert (run/'claude_1/coded.txt').read_text()=='complete after rotation'
    finally:engine.close()


def test_coder_two_hour_budget_keeps_work_after_one_hour(tmp_path):
    run,c,env=setup_run(tmp_path);configure(run,'all',True,env,notify=False)
    store=Store(run);store.submit('codex_1','long','Implement the task.')
    engine=Engine(run,env)
    try:
        engine.tick();job=engine.jobs['codex_1:long']
        job.state['started']=time.time()-3601
        engine.tick()
        assert turn(store,'codex_1:long')['status']=='running'
        job.state['started']=time.time()-7201
        engine.tick()
        assert turn(store,'codex_1:long')['error']=='time_limit_exhausted'
    finally:engine.close()


def test_coder_starts_thirteen_independent_tasks_without_capacity_queue(tmp_path):
    run,c,env=setup_run(tmp_path)
    # Existing installations may still have the old limits on disk.
    configure(run,'all',True,env,notify=False)
    store=Store(run);policy=store.policy();policy.update(max_concurrent=1,max_pending=12)
    save(store.root/'policy.json',policy)
    native=tmp_path/'concurrent-native'
    native.write_text('#!'+sys.executable+'\n'+'''
import json,os,pathlib,sys,time
root=pathlib.Path(os.environ['CODEX_HOME']).parent
sys.stdin.read()
print(json.dumps({'type':'thread.started','thread_id':root.name+'-saved-session'}),flush=True)
(root/'started').write_text(str(os.getpid()))
end=time.monotonic()+20
while not pathlib.Path('release').exists():
 if time.monotonic()>end:sys.exit(2)
 time.sleep(.02)
pathlib.Path(root.name+'.txt').write_text('done')
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Implemented.'}}),flush=True)
print(json.dumps({'type':'turn.completed'}),flush=True)
''');native.chmod(0o755);env['CODEX_BIN_PATH']=str(native)
    for i in range(13):store.submit('codex_1','task'+str(i),'Edit distinct output file.')
    engine=Engine(run,env)
    try:
        drain(engine,lambda:all((store.root/'codex_1'/('task'+str(i))/'started').exists() for i in range(13)))
        assert len(engine.jobs)==13
        assert all(t['turns'][-1]['status']=='running' for t in store.snapshot()['sessions'].values())
        # Follow-ups in one native conversation still wait for its current turn.
        store.submit('codex_1','task0','Check that file again.')
        engine.tick()
        assert read(store.root/'codex_1/task0/native.json')['launches']==1
        # Cancellation affects just the selected native task.
        store.cancel('codex_1','task12')
        drain(engine,lambda:turn(store,'codex_1:task12')['status']=='cancelled')
        assert len(engine.jobs)==12
        (run/'codex_1/release').touch()
        drain(engine,lambda:all(s['turns'][-1]['status'] in ('complete','cancelled') for s in store.snapshot()['sessions'].values()))
        assert all((run/'codex_1'/('task'+str(i)+'.txt')).exists() for i in range(12))
        assert not (run/'codex_1/task12.txt').exists()
        assert read(store.root/'codex_1/task0/native.json')['token']=='task0-saved-session'
    finally:engine.close()
    configure(run,'all',True,env,notify=False)
    assert not {'max_concurrent','max_pending'} & store.policy().keys()


def test_shell_launches_independent_local_coders_without_shared_service(tmp_path):
    import subprocess
    from coder import LocalStore,start_service
    run,c,env=setup_run(tmp_path);configure(run,'all',True,env,notify=False)
    native=tmp_path/'local-native'
    native.write_text('#!'+sys.executable+'\n'+'''
import json,os,pathlib,sys,time
root=pathlib.Path(os.environ['CODEX_HOME']).parent
sys.stdin.read()
print(json.dumps({'type':'thread.started','thread_id':root.name+'-native'}),flush=True)
(root/'ready').write_text(str(os.getpid()))
end=time.monotonic()+25
while not pathlib.Path('release').exists():
 if time.monotonic()>end:sys.exit(1)
 time.sleep(.02)
pathlib.Path(root.name+'.txt').write_text('local')
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Done.'}}),flush=True)
print(json.dumps({'type':'turn.completed'}),flush=True)
''');native.chmod(0o755);env['CODEX_BIN_PATH']=str(native)
    start_service(run,env);work=run/'claude_1';local=LocalStore(run,'claude_1')
    try:
        for i in range(13):
            p=subprocess.run([str(work/'coder'),'run','--session','local'+str(i),'--prompt','Write your distinct file.'],cwd=work,capture_output=True,text=True,timeout=10)
            assert p.returncode==0,p.stderr
            assert 'offline' not in p.stdout
        end=time.monotonic()+10
        while time.monotonic()<end and not all((local.root/'claude_1'/('local'+str(i))/'ready').exists() for i in range(13)):time.sleep(.05)
        assert all((local.root/'claude_1'/('local'+str(i))/'ready').exists() for i in range(13))
        assert not (run/'.coder/controller.json').exists()
        assert not (run/'.coder/engine.json').exists()
        assert not Store(run).snapshot()['sessions']
        assert len(local.snapshot()['sessions'])==13
        assert all(t['turns'][-1]['status']=='running' for t in local.snapshot()['sessions'].values())
        (work/'release').touch()
        end=time.monotonic()+10
        while time.monotonic()<end and not all(s['turns'][-1]['status']=='complete' for s in local.snapshot()['sessions'].values()):time.sleep(.05)
        assert all(s['turns'][-1]['status']=='complete' for s in local.snapshot()['sessions'].values())
        for i in range(13):assert (work/('local'+str(i)+'.txt')).read_text()=='local'
        # A completed native session restarts directly for its follow-up.
        p=subprocess.run([str(work/'coder'),'resume','local0','--prompt','Check again.'],cwd=work,capture_output=True,text=True,timeout=10)
        assert p.returncode==0,p.stderr
        end=time.monotonic()+10
        while time.monotonic()<end and local.snapshot()['sessions']['claude_1:local0']['turns'][-1]['status']!='complete':time.sleep(.05)
        assert len(local.snapshot()['sessions']['claude_1:local0']['turns'])==2
        assert local.snapshot()['sessions']['claude_1:local0']['turns'][-1]['status']=='complete'
        assert read(local.root/'claude_1/local0/native.json')['token']=='local0-native'
        configure(run,'claude_1',False,env,notify=False)
        assert local.policy()['enabled']==[]
        with pytest.raises(ValueError,match='disabled'):local.submit('claude_1','disabled','Do work')
    finally:
        (work/'release').touch()
        for session in local.snapshot()['sessions'].values():local.cancel('claude_1',session['name'])

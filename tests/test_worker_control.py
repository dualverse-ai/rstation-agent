import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest

from common import read, save, pid_token
from recovery import ResearchManager
from station.workers.job_manager import CliJobFailure, CliJobLaunchDecision
from test_workers import cfg, env, drain
from worker_control import queue_request, process_requests, status, offer_checkpoint, handle_failure

SID = 'saved-native-session'


def setup(tmp_path, kind='codex', count=1):
    c = cfg(tmp_path, kind)
    original = c['workers'][0]
    c['workers'] = [{**original, 'name': f'{kind}_{i+1}'} for i in range(count)]
    e = env(tmp_path, kind)
    e.update({f'RSTATION_WORKER_{kind.upper()}_BACKUP_API_KEY': 'backup-secret',
              f'RSTATION_WORKER_{kind.upper()}_BACKUP_BASE_URL': 'http://127.0.0.1:2'})
    save(tmp_path / 'run.json', c)
    for w in c['workers']:
        save(tmp_path / w['name'] / '.runtime/state.json', dict(spawn=1,resumes=0,launches=0,token=SID+'-'+w['name'],next=0,route_count=0,status='continuing'))
    return c, e, ResearchManager(tmp_path, c, e)


def queue(run,e,**kw):
    return queue_request(run,'codex_1',expected_revision=0,environment=e,**kw)


@pytest.mark.parametrize('kind', ['codex','claude'])
def test_model_route_resume_actual_cli_and_controller_restart(tmp_path,kind):
    c,e,m=setup(tmp_path,kind)
    name=kind+'_1'; sid=m.states[name]['token']; original=(tmp_path/'run.json').read_bytes()
    binary=Path(e[kind.upper()+'_BIN_PATH'])
    binary.write_text('#!'+sys.executable+'\n'+'''import json,sys,os,pathlib
p=pathlib.Path.cwd(); (p/'actual.json').write_text(json.dumps({'argv':sys.argv,'base':os.environ.get('CODEX_BASE_URL') or os.environ.get('ANTHROPIC_BASE_URL')}))
'''+(f"print(json.dumps({{'type':'thread.started','thread_id':{sid!r}}}));print(json.dumps({{'type':'turn.completed'}}))\n" if kind=='codex' else f"print(json.dumps({{'type':'system','subtype':'init','session_id':{sid!r}}}));print(json.dumps({{'type':'result','subtype':'success','session_id':{sid!r},'is_error':False,'result':'Hello'}}))\n"))
    req=queue_request(tmp_path,name,expected_revision=0,model='new-model',provider_route=2,change_route=True,environment=e)
    process_requests(m)
    assert m.states[name]['worker_control']['revision']==1
    assert (tmp_path/'run.json').read_bytes()==original
    assert m.launch_cli_job(name);drain(m)
    actual=read(tmp_path/name/'actual.json')
    assert sid in actual['argv'] and 'new-model' in actual['argv']
    assert actual['base']=='http://127.0.0.1:2'
    m.close();m=ResearchManager(tmp_path,read(tmp_path/'run.json'),e)
    try:
        assert m.worker(name)['model']=='new-model'
        assert m.states[name]['token']==sid
        assert status(tmp_path,name)['revision']==1
    finally:m.close()


def test_two_busy_siblings_and_gpu_reservations_untouched(tmp_path):
    c,e,m=setup(tmp_path,count=3)
    jobs=[]
    try:
        for name in ('codex_2','codex_3'):
            proc=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
            jobs.append(proc)
            m.states[name].update(pid=proc.pid,pid_token=pid_token(proc.pid),status='running');m.persist(name)
            m.active_sessions[name]=SimpleNamespace(process=proc)
            save(tmp_path/name/'.runtime/gpu-jobs/job.json',dict(pid=proc.pid,token=pid_token(proc.pid)))
        from station.eval_research.gpu_coordinator import GPUCoordinator
        ledger=Path(c['gpu_coord_file'])
        gpu=GPUCoordinator(str(ledger),[0,1],'siblings',300)
        assert gpu.allocate('busy-research',2)==[0,1]
        before={p:p.read_bytes() for p in tmp_path.rglob('*.json') if 'codex_1' not in str(p)}
        queue(tmp_path,e,model='new-model',restart=True);process_requests(m)
        assert m.worker('codex_1')['model']=='new-model'
        assert all(p.poll() is None for p in jobs)
        assert all(p.read_bytes()==v for p,v in before.items())
        assert set(m.active_sessions)=={'codex_2','codex_3'}
    finally:
        m.active_sessions.clear()
        for p in jobs:p.terminate();p.wait()
        m.close()


def test_target_gpu_job_defers_and_pause_is_preserved(tmp_path):
    c,e,m=setup(tmp_path)
    p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    f=tmp_path/'codex_1/.runtime';(f/'PAUSE').touch()
    save(f/'gpu-jobs/job.json',dict(pid=p.pid,token=pid_token(p.pid)))
    try:
        queue(tmp_path,e,model='new-model');process_requests(m)
        assert m.worker('codex_1')['model']!='new-model' and p.poll() is None
        p.terminate();p.wait();process_requests(m)
        assert m.worker('codex_1')['model']=='new-model'
        assert (f/'PAUSE').exists() and m.states['codex_1']['status']=='paused'
    finally:
        if p.poll() is None:p.terminate();p.wait()
        m.close()


def test_pending_request_survives_restart_and_duplicate(tmp_path):
    c,e,m=setup(tmp_path)
    a=queue(tmp_path,e,restart=True)
    assert queue(tmp_path,e,restart=True)==a
    with pytest.raises(ValueError,match='pending'):queue(tmp_path,e,model='other')
    m.close();m=ResearchManager(tmp_path,c,e)
    try:
        process_requests(m)
        assert status(tmp_path,'codex_1')['receipt']['status']=='applied'
        assert queue(tmp_path,e,restart=True)['id']==a['id']
        assert m.states['codex_1']['worker_control']['revision']==1
    finally:m.close()


@pytest.mark.parametrize('change',['sid','model','pool','revision'])
def test_conflicts_rejected_without_launch_or_settings_change(tmp_path,change):
    c,e,m=setup(tmp_path)
    queue(tmp_path,e,model='new-model')
    if change=='sid':m.states['codex_1']['token']='other'
    if change=='model':
        changed=copy.deepcopy(c);changed['workers'][0]['model']='changed';save(tmp_path/'run.json',changed)
    if change=='pool':m.route_lists['codex'][0]['api_key']='other'
    if change=='revision':m.states['codex_1']['worker_control']={'revision':5}
    try:
        process_requests(m)
        assert status(tmp_path,'codex_1')['receipt']['status']=='rejected'
        assert m.worker('codex_1')['model']!='new-model'
        assert not m.active_sessions
    finally:m.close()


def test_commit_before_receipt_crash_replays_once(tmp_path,monkeypatch):
    import worker_control
    c,e,m=setup(tmp_path);queue(tmp_path,e,restart=True)
    real=worker_control.write
    def crash(folder,name,data):
        if name=='worker-control-receipt.json':raise OSError('simulated crash')
        real(folder,name,data)
    monkeypatch.setattr(worker_control,'write',crash)
    process_requests(m)
    assert (tmp_path/'codex_1/.runtime/worker-control-request.json').exists()
    assert m.states['codex_1']['worker_control']['revision']==1
    m.close();monkeypatch.setattr(worker_control,'write',real)
    m=ResearchManager(tmp_path,c,e)
    try:
        process_requests(m)
        assert status(tmp_path,'codex_1')['revision']==1
        assert status(tmp_path,'codex_1')['receipt']['status']=='applied'
    finally:m.close()


def test_live_wrapper_only_offers_completed_turn_checkpoint(tmp_path):
    c,e,m=setup(tmp_path);req=queue(tmp_path,e,restart=True)
    cli=SimpleNamespace(args=SimpleNamespace(workspace=str(tmp_path/'codex_1')),busy=True,output=bytearray())
    assert not offer_checkpoint(cli)
    cli.busy=False;cli.output.extend(b'new input')
    assert not offer_checkpoint(cli)
    cli.output.clear();assert offer_checkpoint(cli)
    assert read(tmp_path/'codex_1/.runtime/worker-control-ready.json')['request_id']==req['id']
    m.close()


def test_active_worker_requires_own_checkpoint_then_guarded_reclaim(tmp_path):
    c,e,m=setup(tmp_path);name='codex_1';folder=tmp_path/name/'.runtime'
    p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    session=SimpleNamespace(process=p,transcript_handle=None,stderr_handle=None)
    m.states[name].update(pid=p.pid,pid_token=pid_token(p.pid));m.persist(name)
    m.active_sessions[name]=session
    (folder/'launch_000000').mkdir();(folder/'launch_000000/transcript.jsonl').write_text('')
    try:
        req=queue(tmp_path,e,restart=True);process_requests(m)
        assert p.poll() is None and 'worker_control' not in m.states[name]
        save(folder/'worker-control-ready.json',dict(request_id=req['id'],pid=p.pid,token=pid_token(p.pid)))
        process_requests(m)
        assert p.poll() is not None and name not in m.active_sessions
        assert m.states[name]['token']==SID+'-'+name
    finally:
        m.active_sessions.clear()
        if p.poll() is None:p.terminate();p.wait()
        m.close()


@pytest.mark.parametrize('category',['provider_unavailable','session_unavailable','model_refusal','infra_transient'])
def test_failure_retains_exact_session_and_blocks_nontransient(tmp_path,category):
    c,e,m=setup(tmp_path);queue(tmp_path,e,provider_route=2,change_route=True);process_requests(m)
    s=m.states['codex_1'];sid=s['token']
    f=CliJobFailure(category,category=='provider_unavailable','test')
    assert handle_failure(m,'codex_1',f)
    assert s['token']==sid
    assert s['status']==('retrying' if category=='provider_unavailable' else 'blocked')
    m.persist('codex_1');m.close();m=ResearchManager(tmp_path,c,e)
    try:
        assert m.states['codex_1']['token']==sid
        assert m.states['codex_1']['worker_control']['provider_route']==2
    finally:m.close()


def test_sibling_change_does_not_invalidate_target(tmp_path):
    c,e,m=setup(tmp_path,count=2);queue(tmp_path,e,model='new-model')
    c['workers'][1]['model']='different-sibling';save(tmp_path/'run.json',c)
    process_requests(m)
    assert m.worker('codex_1')['model']=='new-model';m.close()


def test_no_credentials_in_request_receipt_or_saved_settings(tmp_path):
    c,e,m=setup(tmp_path);queue(tmp_path,e,provider_route=2,change_route=True);process_requests(m)
    serial=''.join(p.read_text() for p in (tmp_path/'codex_1/.runtime').rglob('*.json'))
    assert 'backup-secret' not in serial and 'test-codex' not in serial and '127.0.0.1' not in serial
    m.close()


def test_concurrent_operator_requests_have_single_winner(tmp_path):
    c,e,m=setup(tmp_path)
    code="""import sys,json
from worker_control import queue_request
try:
 r=queue_request(sys.argv[1],'codex_1',expected_revision=0,model=sys.argv[2]);print('queued')
except ValueError:print('conflict')
"""
    children=[subprocess.Popen([sys.executable,'-c',code,str(tmp_path),model],env=e,stdout=subprocess.PIPE,text=True) for model in ('model-a','model-b')]
    results=[p.communicate(timeout=10)[0].strip() for p in children]
    assert sorted(results)==['conflict','queued']
    process_requests(m);assert m.worker('codex_1')['model'] in ('model-a','model-b');m.close()


def test_gemini_model_change_preserves_native_store_context_and_notices(tmp_path):
    from test_antigravity import config, environment
    from messages import Messages
    import sqlite3
    FRESH='99999999-2222-4333-8444-555555555555'
    c=config(tmp_path);e=environment(tmp_path);work=tmp_path/'gemini_1'
    save(work/'.runtime/state.json',dict(spawn=4,resumes=0,launches=0,token=FRESH,next=0,route_count=0,status='paused'))
    (work/'.runtime/PAUSE').touch()
    store=work/'.home/.gemini/antigravity-cli/conversations'/f'{FRESH}.db'
    store.parent.mkdir(parents=True)
    with sqlite3.connect(store) as db:db.execute('CREATE TABLE steps (idx INTEGER, evidence TEXT)')
    stores=[store]
    c['gemini_context_tokens']=700000;save(tmp_path/'run.json',c)
    e.update(RSTATION_WORKER_GEMINI_BACKUP_API_KEY='backup',RSTATION_WORKER_GEMINI_BACKUP_BASE_URL='http://127.0.0.1:2')
    before=[p.read_bytes() for p in stores]
    Messages(work).add('Owner notice stays queued.')
    notices=(work/'.runtime/operator_messages.json').read_bytes()
    m=ResearchManager(tmp_path,c,e)
    try:
        queue_request(tmp_path,'gemini_1',expected_revision=0,model='gemini-4-carbon',provider_route=2,change_route=True,environment=e)
        process_requests(m)
        decision=CliJobLaunchDecision('resume',4,1,FRESH)
        claimed=m._claim_cli_job_launch('gemini_1','launch_000009',decision)
        spec=m._build_cli_job_launch_spec('gemini_1','launch_000009',decision,claimed)
        prepared_launch=m._prepare_cli_job_launch(spec,decision,m._get_cli_job_backend)
        assert FRESH in prepared_launch.command
        assert spec.model_name=='gemini-4-carbon'
        assert spec.env['GOOGLE_GEMINI_BASE_URL']=='http://127.0.0.1:2'
        assert c['gemini_context_tokens']==700000
        assert [p.read_bytes() for p in stores]==before
        assert (work/'.runtime/operator_messages.json').read_bytes()==notices
        assert (work/'.runtime/PAUSE').exists()
    finally:m.close()


def test_changed_pool_after_application_blocks_without_fresh_session(tmp_path):
    c,e,m=setup(tmp_path);queue(tmp_path,e,restart=True);process_requests(m)
    sid=m.states['codex_1']['token'];m.route_lists['codex'].reverse()
    assert not m.launch_cli_job('codex_1')
    assert m.states['codex_1']['token']==sid
    assert m.states['codex_1']['blocked_reason']=='worker_control_conflict'
    m.close()


def test_local_exec_failure_keeps_change_and_exact_sid(tmp_path,monkeypatch):
    c,e,m=setup(tmp_path);queue(tmp_path,e,model='new-model');process_requests(m)
    sid=m.states['codex_1']['token']
    Path(e['CODEX_BIN_PATH']).chmod(0o600)
    try:
        assert not m.launch_cli_job('codex_1')
    except PermissionError:
        pass  # Older host bases surface this to the controller's retry catch.
    assert m.states['codex_1']['token']==sid and m.worker('codex_1')['model']=='new-model'
    m.close()


def test_explicit_route_repair_after_failure_and_refusal_stays_blocked(tmp_path):
    c,e,m=setup(tmp_path);queue(tmp_path,e,restart=True);process_requests(m)
    handle_failure(m,'codex_1',CliJobFailure('infra_transient',False,'400'))
    queue_request(tmp_path,'codex_1',expected_revision=1,provider_route=2,change_route=True,environment=e)
    process_requests(m)
    assert not m.states['codex_1'].get('blocked_reason')
    handle_failure(m,'codex_1',CliJobFailure('model_refusal',False,'refusal'))
    with pytest.raises(ValueError,match='block'):
        queue_request(tmp_path,'codex_1',expected_revision=2,restart=True,environment=e)
    m.close()


def test_monitor_displays_effective_worker_model(tmp_path):
    from monitor import snapshot
    c,e,m=setup(tmp_path);queue(tmp_path,e,model='new-model');process_requests(m)
    assert snapshot(tmp_path)['workers'][0]['model']=='new-model'
    m.close()


def test_live_turn_finishes_before_target_checkpoint_and_notice_is_retained(tmp_path,monkeypatch):
    import controller_guard
    from messages import Messages
    c,e,m=setup(tmp_path,'claude');m.close()
    c['live_input']=True;save(tmp_path/'run.json',c)
    work=tmp_path/'claude_1';sid=SID+'-claude_1'
    binary=Path(e['CLAUDE_BIN_PATH'])
    binary.write_text('#!'+sys.executable+'\n'+f'''import json,sys,pathlib,time
p=pathlib.Path.cwd()
(p/'native-args.json').write_text(json.dumps(sys.argv))
print(json.dumps({{'type':'system','subtype':'init','session_id':{sid!r}}}),flush=True)
for line in sys.stdin:
 event=json.loads(line)
 (p/'turn-started').touch()
 while not (p/'finish-turn').exists():time.sleep(.02)
 print(json.dumps({{'type':'result','subtype':'success','is_error':False,'session_id':{sid!r},'result':'Completed'}}),flush=True)
''')
    real=controller_guard.native_child
    def native(info,children=None):
        if str(binary) in info['args'][:3]:return True
        return real(info,children)
    monkeypatch.setattr(controller_guard,'native_child',native)
    m=ResearchManager(tmp_path,c,e)
    def until(check,seconds=8):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            m.poll_cli_jobs();process_requests(m)
            if check():return
            time.sleep(.02)
        raise AssertionError('Checkpoint did not arrive')
    try:
        assert m.launch_cli_job('claude_1')
        until(lambda:(work/'turn-started').exists())
        note=Messages(work).add('Preserve this operator notice.')
        queue_request(tmp_path,'claude_1',expected_revision=0,model='new-model',environment=e)
        for _ in range(10):m.poll_cli_jobs();process_requests(m);time.sleep(.02)
        assert m.worker('claude_1')['model']!='new-model'
        assert m.active_sessions['claude_1'].process.poll() is None
        (work/'finish-turn').touch()
        until(lambda:'worker_control' in m.states['claude_1'])
        assert m.states['claude_1']['token']==sid
        assert m.worker('claude_1')['model']=='new-model'
        assert Messages(work).list()[0]['id']==note
        assert Messages(work).list()[0]['status']=='queued'
        assert not m.active_sessions
        assert m.launch_cli_job('claude_1')
        until(lambda:'new-model' in read(work/'native-args.json',[]))
        assert sid in read(work/'native-args.json')
    finally:m.close()


def test_supported_group_resume_uses_effective_model_without_invalidating_base(tmp_path,monkeypatch):
    import main as app
    import capabilities
    c,e,m=setup(tmp_path);queue(tmp_path,e,model='new-model',provider_route=2,change_route=True);process_requests(m);m.close()
    observed=[]
    monkeypatch.setattr(app,'run_dir',lambda name:tmp_path)
    monkeypatch.setattr(app,'problem_info',lambda *a,**k:{'category':c['category']})
    monkeypatch.setattr(app,'safe_environment',lambda:e)
    def preflight(config,environment):
        assert config['workers'][0]['model']=='new-model'
        config['workers'][0]['route_seed']=1
    monkeypatch.setattr(app,'preflight',preflight)
    monkeypatch.setattr(app,'prepare_workspaces',lambda *a:None)
    monkeypatch.setattr(app,'start_controller',lambda run,config,env,keep:observed.append(copy.deepcopy(config)))
    monkeypatch.setattr(capabilities,'ensure_services',lambda *a:{})
    app.main(['resume','test_run','--keep-experiments'])
    assert observed and observed[0]['workers'][0]==c['workers'][0]
    restored=ResearchManager(tmp_path,read(tmp_path/'run.json'),e)
    try:
        assert restored.worker('codex_1')['model']=='new-model'
        assert restored._load_cli_job_state('codex_1').resume_launch_eligible
        assert not restored.states['codex_1'].get('blocked_reason')
    finally:restored.close()


def test_dead_leader_with_surviving_tool_defers_change_and_old_launch(tmp_path):
    import signal
    c,e,m=setup(tmp_path)
    child_file=tmp_path/'orphan-pid'
    leader=subprocess.Popen([sys.executable,'-c',
        "import subprocess,sys,pathlib; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid))",str(child_file)],start_new_session=True)
    leader.wait(timeout=5);child=int(child_file.read_text())
    m.states['codex_1'].update(last_process_group=leader.pid,pid=None,resumes=14);m.persist('codex_1')
    try:
        queue(tmp_path,e,model='new-model');process_requests(m)
        assert m.worker('codex_1')['model']!='new-model'
        assert not m.launch_cli_job('codex_1')
        assert m.states['codex_1']['resumes']==14
        os.kill(child,signal.SIGTERM)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            process_requests(m)
            if m.worker('codex_1')['model']=='new-model':break
            time.sleep(.02)
        assert m.worker('codex_1')['model']=='new-model'
    finally:
        try:os.killpg(leader.pid,signal.SIGKILL)
        except ProcessLookupError:pass
        m.close()


def test_gpu_helper_and_unreleased_reservation_defer_target(tmp_path):
    from station.eval_research.gpu_coordinator import GPUCoordinator
    c,e,m=setup(tmp_path)
    helper=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
    folder=tmp_path/'codex_1/.runtime';job=folder/'gpu-jobs/finishing.json'
    gpu=GPUCoordinator(c['gpu_coord_file'],[0],'target-job',300);assert gpu.allocate('experiment',1)==[0]
    save(job,dict(owner='target-job',helper_pid=helper.pid,helper_token=pid_token(helper.pid),pid=None))
    try:
        queue(tmp_path,e,restart=True);process_requests(m)
        assert 'worker_control' not in m.states['codex_1']
        helper.terminate();helper.wait();process_requests(m)
        assert 'worker_control' not in m.states['codex_1']
        gpu.deallocate('experiment');job.unlink();process_requests(m)
        assert 'worker_control' in m.states['codex_1']
    finally:
        if helper.poll() is None:helper.terminate();helper.wait()
        m.close()


def test_status_reports_existing_adoption_route_pin(tmp_path):
    c,e,m=setup(tmp_path)
    m.states['codex_1']['session_adoption']={'provider_route':{'number':2}}
    m.persist('codex_1')
    assert status(tmp_path,'codex_1')['provider_route']==2
    queue(tmp_path,e,model='new-model');process_requests(m)
    assert status(tmp_path,'codex_1')['provider_route']==2
    m.close()


@pytest.mark.parametrize('category',['session_unavailable','model_refusal','provider_unavailable','infra_transient'])
def test_failure_during_pending_change_never_discards_sid(tmp_path,monkeypatch,category):
    c,e,m=setup(tmp_path)
    binary=Path(e['CODEX_BIN_PATH']);work=tmp_path/'codex_1';sid=m.states['codex_1']['token']
    binary.write_text('#!'+sys.executable+'\n'+f'''import json,sys,pathlib,time
p=pathlib.Path.cwd();print(json.dumps({{'type':'thread.started','thread_id':{sid!r}}}),flush=True)
(p/'active-turn').touch()
while not (p/'fail-turn').exists():time.sleep(.01)
print(json.dumps({{'type':'error','message':'No session found'}}),flush=True)
sys.exit(1)
''')
    def failure(name,session):
        if category=='session_unavailable':m.states[name]['session_unavailable']=True
        return CliJobFailure(category,category=='provider_unavailable','native failure')
    monkeypatch.setattr(m,'_classify_cli_job_failure',failure)
    try:
        assert m.launch_cli_job('codex_1')
        deadline=time.monotonic()+5
        while not (work/'active-turn').exists() and time.monotonic()<deadline:time.sleep(.01)
        assert (work/'active-turn').exists()
        queue(tmp_path,e,model='new-model')
        (work/'fail-turn').touch();drain(m);process_requests(m)
        assert m.states['codex_1']['token']==sid
        if category=='provider_unavailable':
            assert status(tmp_path,'codex_1')['receipt']['status']=='applied'
        else:
            assert status(tmp_path,'codex_1')['receipt']['status']=='rejected'
            assert not m.launch_cli_job('codex_1')
        m.close();m=ResearchManager(tmp_path,c,e)
        assert m.states['codex_1']['token']==sid
        if category!='provider_unavailable':assert not m.launch_cli_job('codex_1')
    finally:m.close()


def test_operator_queue_serializes_with_native_failure_state_change(tmp_path,monkeypatch):
    import threading
    c,e,m=setup(tmp_path)
    entered=threading.Event();release=threading.Event();errors=[]
    m.states['codex_1']['session_unavailable']=True
    def pause(name):entered.set();assert release.wait(5)
    monkeypatch.setattr(m,'_record_primary_failure',pause)
    monkeypatch.setattr(m,'_maybe_recover_session',lambda *args:False)
    def fail():
        try:m._schedule_cli_job_fresh_attempt('codex_1',None,CliJobFailure('session_unavailable',False,'missing'))
        except Exception as exc:errors.append(exc)
    thread=threading.Thread(target=fail);thread.start();assert entered.wait(5)
    code="""import sys
from worker_control import queue_request
try:queue_request(sys.argv[1],'codex_1',expected_revision=0,restart=True);print('queued')
except ValueError:print('rejected')
"""
    child=subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],env=e,stdout=subprocess.PIPE,text=True)
    try:
        time.sleep(.2);assert child.poll() is None
        release.set();thread.join(5)
        assert child.communicate(timeout=5)[0].strip()=='rejected'
        assert not errors
        assert not (tmp_path/'codex_1/.runtime/worker-control-request.json').exists()
    finally:
        release.set();thread.join(5)
        if child.poll() is None:child.terminate();child.wait()
        m.close()

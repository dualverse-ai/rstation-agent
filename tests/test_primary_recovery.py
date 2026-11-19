"""Preferred-provider recovery without changing researcher conversations."""
from dataclasses import replace
import json
from pathlib import Path
import time
import pytest

from test_workers import cfg, env, drain
from common import save
import recovery
from recovery import ResearchManager
from station.workers.job_manager import CliJobLaunchDecision, CliJobFailure


def manager(tmp_path, kind='claude'):
    config=cfg(tmp_path,kind);environment=env(tmp_path,kind)
    role=kind.upper()
    environment.update({f'RSTATION_WORKER_{role}_BACKUP_API_KEY':'backup',
                        f'RSTATION_WORKER_{role}_BACKUP_BASE_URL':'http://127.0.0.1:2'})
    m=ResearchManager(tmp_path,config,environment)
    name=kind+'_1'
    m.states[name].update(token='saved-research-conversation',spawn=1,launches=2,route_index=1,route_offset=1,last_success=time.time())
    return m,name


def build(m,name,count=1):
    d=CliJobLaunchDecision('resume',1,count,'saved-research-conversation',count)
    return m._build_cli_job_launch_spec(name,'launch_test',d,m.states[name])


@pytest.mark.parametrize('kind',['claude','codex'])
def test_recovered_primary_keeps_conversation_and_workspace(tmp_path,monkeypatch,kind):
    m,name=manager(tmp_path,kind);calls=[]
    monkeypatch.setattr(m,'_probe_primary',lambda worker:calls.append(worker['name']) or True)
    try:
        spec=build(m,name)
        assert spec.env['ANTHROPIC_API_KEY' if kind=='claude' else 'CODEX_API_KEY']=='test-'+kind
        assert spec.prompt=='Continue your research.' and spec.workspace_root==str(tmp_path/name)
        assert m.states[name]['token']=='saved-research-conversation'
        assert m.states[name]['route_index']==0 and calls==[name]
        assert 'test-'+kind not in (tmp_path/name/'.runtime/state.json').read_text()
    finally:m.close()


def test_failed_probe_keeps_backup_and_cooldown_survives_restart(tmp_path,monkeypatch):
    m,name=manager(tmp_path);calls=[]
    monkeypatch.setattr(m,'_probe_primary',lambda worker:calls.append(1) or False)
    try:
        assert build(m,name).env['ANTHROPIC_API_KEY']=='backup'
        assert build(m,name).env['ANTHROPIC_API_KEY']=='backup'
        assert calls==[1]
        m.persist(name)
        config,environment=m.config,m.env
    finally:m.close()
    restarted=ResearchManager(tmp_path,config,environment)
    monkeypatch.setattr(restarted,'_probe_primary',lambda worker:pytest.fail('Repeated probe before cooldown'))
    try:
        assert build(restarted,name).env['ANTHROPIC_API_KEY']=='backup'
    finally:restarted.close()


def test_probe_runs_again_after_thirty_minutes(tmp_path,monkeypatch):
    m,name=manager(tmp_path);calls=[]
    monkeypatch.setattr(m,'_probe_primary',lambda worker:calls.append(1) or True)
    try:
        m.states[name].update(primary_checked_at=time.time()-recovery.PRIMARY_RECOVERY_SECONDS-1,primary_healthy=False)
        assert build(m,name).env['ANTHROPIC_API_KEY']=='test-claude'
        assert calls==[1]
    finally:m.close()


def test_failed_real_continuation_invalidates_probe_success(tmp_path,monkeypatch):
    m,name=manager(tmp_path)
    monkeypatch.setattr(m,'_probe_primary',lambda worker:True)
    try:
        build(m,name)
        m._schedule_cli_job_resume(name,None,CliJobFailure('provider',True,'failed',resume_token='saved-research-conversation',resume_count=1))
        assert not m.states[name]['primary_healthy']
        monkeypatch.setattr(m,'_probe_primary',lambda worker:pytest.fail('Should honor cooldown'))
        assert build(m,name,count=2).env['ANTHROPIC_API_KEY']=='backup'
        assert m.states[name]['token']=='saved-research-conversation'
    finally:m.close()


def test_backup_preflight_seed_does_not_become_permanent_primary(tmp_path,monkeypatch):
    m,name=manager(tmp_path)
    m.config['workers'][0]['route_seed']=1;m.states[name]['route_offset']=0
    monkeypatch.setattr(m,'_probe_primary',lambda worker:True)
    try:
        assert build(m,name,count=4).env['ANTHROPIC_API_KEY']=='test-claude'
        m._record_primary_failure(name)
        assert build(m,name,count=6).env['ANTHROPIC_API_KEY']=='backup'
    finally:m.close()


def test_probe_failure_does_not_break_launch_or_expose_key(tmp_path,monkeypatch):
    m,name=manager(tmp_path)
    def fail(worker):raise RuntimeError('test-claude secret provider response')
    monkeypatch.setattr(m,'_probe_primary',fail)
    try:
        assert build(m,name).env['ANTHROPIC_API_KEY']=='backup'
        text=(tmp_path/name/'.runtime/console.log').read_text()
        assert 'RuntimeError' in text and 'secret provider' not in text
    finally:m.close()


def test_probe_result_shared_by_same_model_workers(tmp_path,monkeypatch):
    m,name=manager(tmp_path)
    other={**m.worker(name),'name':'claude_2'};m.config['workers'].append(other)
    m.states['claude_2']=dict(m.states[name]);(tmp_path/'claude_2/.runtime').mkdir(parents=True)
    calls=[];monkeypatch.setattr(m,'_probe_primary',lambda worker:calls.append(1) or True)
    try:
        build(m,name);build(m,'claude_2')
        assert calls==[1] and m.states['claude_2']['route_index']==0
    finally:m.close()


def test_real_probe_isolated_and_timeout_reaped(tmp_path,monkeypatch):
    m,name=manager(tmp_path)
    binary=Path(m.bin_paths['claude'][0])
    binary.write_text('#!'+__import__('sys').executable+'\n'+'''import json,time
print(json.dumps({"type":"system","subtype":"init","session_id":"probe-only"}),flush=True)
time.sleep(60)
''');binary.chmod(0o755)
    monkeypatch.setattr(recovery,'PRIMARY_PROBE_SECONDS',.25)
    try:
        started=time.monotonic()
        idle=recovery.constants.CLI_WORKER_TRANSCRIPT_IDLE_TIMEOUT_SECONDS
        assert not m._probe_primary(m.worker(name))
        assert recovery.constants.CLI_WORKER_TRANSCRIPT_IDLE_TIMEOUT_SECONDS==idle
        assert time.monotonic()-started<8
        assert m.states[name]['token']=='saved-research-conversation'
        assert not (tmp_path/name/'.claude').exists()
    finally:m.close()


def test_real_probe_success_uses_primary_and_never_research_session(tmp_path):
    m,name=manager(tmp_path)
    binary=Path(m.bin_paths['claude'][0])
    binary.write_text('#!'+__import__('sys').executable+'\n'+'''import json,os,sys
assert os.environ['ANTHROPIC_API_KEY']=='test-claude'
assert '--resume' not in sys.argv
print(json.dumps({"type":"system","subtype":"init","session_id":"probe-only"}),flush=True)
print(json.dumps({"type":"result","session_id":"probe-only","is_error":False,"result":"Hello"}),flush=True)
''');binary.chmod(0o755)
    try:
        idle=recovery.constants.CLI_WORKER_TRANSCRIPT_IDLE_TIMEOUT_SECONDS
        assert m._probe_primary(m.worker(name))
        assert recovery.constants.CLI_WORKER_TRANSCRIPT_IDLE_TIMEOUT_SECONDS==idle
        assert m.states[name]['token']=='saved-research-conversation'
        assert not m.active_sessions
    finally:m.close()


def endless_backup(tmp_path, monkeypatch):
    """A live backup with a controllable tool and native turn completion."""
    m,name=manager(tmp_path)
    binary=Path(m.bin_paths['claude'][0])
    binary.write_text('#!'+__import__('sys').executable+'\n'+'''
import json,os,pathlib,time,sys
root=pathlib.Path.cwd()
print(json.dumps({'type':'system','subtype':'init','session_id':'saved-research-conversation'}),flush=True)
if os.environ['ANTHROPIC_API_KEY']=='backup':
 (root/'memory').write_text('retained scientific result')
 print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','id':'experiment','name':'Bash'}]}}),flush=True)
 while not (root/'finish-tool').exists():time.sleep(.02)
 print(json.dumps({'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'experiment'}]}}),flush=True)
 while not (root/'finish-turn').exists():
  (root/'heartbeat').write_text(str(time.monotonic_ns()))
  time.sleep(.02)
 print(json.dumps({'type':'result','session_id':'saved-research-conversation','is_error':False,'result':'backup completed'}),flush=True)
else:
 assert '--resume' in sys.argv and 'saved-research-conversation' in sys.argv
 assert (root/'memory').read_text()=='retained scientific result'
 print(json.dumps({'type':'result','session_id':'saved-research-conversation','is_error':False,'result':'continued'}),flush=True)
''');binary.chmod(0o755)
    m.states[name].update(primary_checked_at=time.time(),primary_healthy=False,next=0)
    assert m.launch_cli_job(name)
    deadline=time.monotonic()+5
    while not m.pending_tools.get(name) and time.monotonic()<deadline:
        m.poll_cli_jobs();time.sleep(.02)
    assert m.pending_tools[name]=={'experiment'}
    calls=[]
    monkeypatch.setattr(m,'_probe_primary',lambda worker:calls.append(worker['name']) or True)
    m.states[name]['primary_checked_at']=0
    return m,name,calls


def finish_backup_tool(m,name,tmp_path):
    (tmp_path/name/'finish-tool').touch()
    deadline=time.monotonic()+3
    heartbeat=tmp_path/name/'heartbeat'
    while (m.pending_tools[name] or not heartbeat.exists()) and time.monotonic()<deadline:
        m.poll_cli_jobs();time.sleep(.02)
    assert not m.pending_tools[name] and heartbeat.exists()
    return heartbeat


def test_successful_probe_does_not_interrupt_productive_backup(tmp_path,monkeypatch):
    m,name,calls=endless_backup(tmp_path,monkeypatch)
    try:
        session=m.active_sessions[name]
        model=m.worker(name)['model']
        # Another worker of the same model can leave a cached healthy probe.
        assert m._primary_recovered(name)
        m.poll_cli_jobs()
        assert calls==[name] and session.process.poll() is None
        heartbeat=finish_backup_tool(m,name,tmp_path)
        before=heartbeat.read_text()
        for _ in range(3):
            m.poll_cli_jobs();time.sleep(.04)
        assert heartbeat.read_text()!=before  # Native work kept running, unfrozen.
        assert m.active_sessions[name] is session and session.process.poll() is None
        assert m.states[name]['route_index']==1 and m.states[name]['status']=='running'
        assert m.states[name]['token']=='saved-research-conversation'
        assert m.worker(name)['model']==model
        assert (tmp_path/name/'memory').read_text()=='retained scientific result'
        assert calls==[name]
    finally:m.close()


def test_primary_recovery_waits_for_native_turn_completion(tmp_path,monkeypatch):
    m,name,calls=endless_backup(tmp_path,monkeypatch)
    try:
        session=m.active_sessions[name]
        for _ in range(3):m.poll_cli_jobs()
        assert not calls and session.process.poll() is None
        finish_backup_tool(m,name,tmp_path)
        for _ in range(3):m.poll_cli_jobs()
        assert not calls and session.process.poll() is None
        assert m.states[name]['route_index']==1
        (tmp_path/name/'finish-turn').touch()
        drain(m)
        assert session.process.returncode==0 and not calls
        assert m.states[name]['status']=='continuing'
        m.states[name]['next']=0
        assert m.launch_cli_job(name);drain(m)
        assert m.states[name]['route_index']==0 and calls==[name]
        assert m.states[name]['token']=='saved-research-conversation'
        assert (tmp_path/name/'memory').read_text()=='retained scientific result'
    finally:m.close()


def test_releasing_gpu_reservation_does_not_trigger_primary_switch(tmp_path,monkeypatch):
    from gpu import reserve_slots
    m,name,calls=endless_backup(tmp_path,monkeypatch)
    m.worker(name)['gpus']=[0]
    slots=reserve_slots(tmp_path/name/'.runtime',1,1)
    assert slots
    try:
        session=m.active_sessions[name]
        assert m._primary_recovered(name)
        finish_backup_tool(m,name,tmp_path)
        assert session.process.poll() is None and calls==[name]
        for slot in slots:slot.close()
        slots=[]
        for _ in range(3):m.poll_cli_jobs()
        assert session.process.poll() is None and m.states[name]['route_index']==1
    finally:
        for slot in slots:slot.close()
        m.close()


def test_periodic_check_respects_operator_pause(tmp_path,monkeypatch):
    m,name,calls=endless_backup(tmp_path,monkeypatch)
    try:
        (tmp_path/name/'.runtime/PAUSE').touch()
        m.poll_cli_jobs()
        assert not calls
    finally:m.close()


@pytest.mark.parametrize('backend,start,end',[
    ('claude',{'type':'assistant','message':{'content':[{'type':'tool_use','id':'a'}]}},
     {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'a'}]}}),
    ('codex',{'type':'item.started','item':{'id':'a','type':'command_execution'}},
     {'type':'item.completed','item':{'id':'a','type':'command_execution'}}),
    ('gemini',{'step_update':{'step_index':4,'step_type':'tool','state':'ACTIVE'}},
     {'step_update':{'step_index':4,'step_type':'tool','state':'DONE'}}),
])
def test_tracks_native_tool_boundaries(tmp_path,backend,start,end):
    m,name=manager(tmp_path)
    try:
        m._record_pending_tools(name,start,backend)
        m._record_pending_tools(name,start,backend)
        assert len(m.pending_tools[name])==1
        m._record_pending_tools(name,end,backend)
        assert not m.pending_tools[name]
    finally:m.close()


def test_codex_plan_item_is_not_a_pending_tool(tmp_path):
    m,name=manager(tmp_path)
    try:
        m._record_pending_tools(name,{'type':'item.started','item':{'id':'plan','type':'todo_list'}},'codex')
        assert not m.pending_tools[name]
    finally:m.close()

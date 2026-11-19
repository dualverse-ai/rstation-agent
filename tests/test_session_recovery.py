"""Bounded conversation recovery after repeated failures of every provider."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

from test_primary_recovery import manager, build
from test_workers import drain
from common import pid_token, read, save
import recovery
from recovery import ResearchManager
from station.workers.job_manager import CliJobFailure


@pytest.fixture
def researcher(tmp_path, monkeypatch):
    m,name=manager(tmp_path)
    now=[100000.0]
    monkeypatch.setattr(recovery.time,'time',lambda:now[0])
    m.states[name].update(last_success=now[0]-3600,last_response=now[0]-3600)
    monkeypatch.setattr(m,'_probe_primary',lambda worker:False)
    yield m,name,now
    m.close()


def fail(m,name,now,*,route=0,delay=400,session=None,fresh=False,advance=True):
    state=m.states[name]
    if advance:state['launches']+=1
    state['route_index']=route
    failure=CliJobFailure('infra_transient',True,'provider error',
                         resume_token=state.get('token'),resume_count=1)
    method=m._schedule_cli_job_fresh_attempt if fresh else m._schedule_cli_job_resume
    method(name,session,failure)
    now[0]+=delay


def sustained_failure(m,name,now,**kwargs):
    for index in range(6):fail(m,name,now,route=index%2,**kwargs)


@pytest.mark.parametrize('fresh',[False,True])
def test_all_route_failures_archive_state_and_keep_research(researcher,tmp_path,fresh):
    m,name,now=researcher;state=m.states[name];work=tmp_path/name
    (work/'CONTINUATION.md').write_text('The experiment tests hypothesis B.')
    (work/'result.csv').write_text('accuracy,0.7\n')
    save(work/'.rstation/credentials.json',{'key':'private-test-account'})
    m.config['gemini_context_tokens']=300000
    model=m.worker(name)['model']
    sustained_failure(m,name,now,fresh=fresh)
    assert state['token'] is None and state['session_unavailable']
    assert state['resumes']==state['route_count']==m._endpoint_resume_counts[name]==0
    assert state['session_recovery']['failures']==6
    archive=work/state['session_recovery']['archive']
    assert read(archive/'state.json')['token']=='saved-research-conversation'
    assert (archive/'CONTINUATION.md').read_text()==(work/'CONTINUATION.md').read_text()
    assert (work/'result.csv').read_text()=='accuracy,0.7\n'
    assert read(work/'.rstation/credentials.json')['key']=='private-test-account'
    assert m.worker(name)['model']==model and m.config['gemini_context_tokens']==300000
    assert 'private-test-account' not in (work/'.runtime/console.log').read_text()


def test_transient_or_incomplete_route_failures_keep_conversation(researcher):
    m,name,now=researcher;state=m.states[name]
    fail(m,name,now,delay=10)
    assert state['token']=='saved-research-conversation'
    # Even a long primary outage does not justify resetting before backups.
    for _ in range(8):fail(m,name,now,route=0)
    assert state['token']=='saved-research-conversation'
    assert 'session_recovery' not in state


def test_quick_all_route_failures_keep_conversation(researcher):
    m,name,now=researcher
    sustained_failure(m,name,now,delay=1)
    assert m.states[name]['token']=='saved-research-conversation'
    assert 'session_recovery' not in m.states[name]


@pytest.mark.parametrize('key',['last_response','last_tool_completed','last_success'])
def test_substantive_progress_resets_failure_window(researcher,key):
    m,name,now=researcher;state=m.states[name]
    for index in range(5):fail(m,name,now,route=index%2)
    state[key]=now[0]
    fail(m,name,now,route=1)
    assert state['token']=='saved-research-conversation'
    assert state['session_failure_streak']['failures']==1
    assert state['session_failure_streak']['since']==state[key]


def test_global_outage_does_not_churn_fresh_conversations(researcher):
    m,name,now=researcher;state=m.states[name]
    sustained_failure(m,name,now)
    first=state['session_recovery'].copy()
    # New native session exists but never responds, even after six hours.
    state.update(token='new-empty-conversation')
    state.pop('session_unavailable')
    now[0]+=recovery.SESSION_RECOVERY_COOLDOWN_SECONDS
    sustained_failure(m,name,now)
    assert state['token']=='new-empty-conversation'
    assert state['session_recovery']==first
    # Actual progress rearms recovery, but a new full failure window is needed.
    state['last_response']=now[0]
    sustained_failure(m,name,now)
    assert state['token'] is None
    assert state['session_recovery']['previous_token']=='new-empty-conversation'


def test_progress_does_not_bypass_reset_cooldown(researcher):
    m,name,now=researcher;state=m.states[name]
    sustained_failure(m,name,now)
    first=state['session_recovery'].copy()
    state.update(token='new-conversation',last_response=now[0])
    state.pop('session_unavailable')
    sustained_failure(m,name,now)
    assert state['token']=='new-conversation' and state['session_recovery']==first


@pytest.mark.parametrize('protection',['stop','pause','supervisor','gpu','live_cli','orphan'])
def test_intentional_waits_and_live_work_prevent_reset(researcher,tmp_path,monkeypatch,protection):
    m,name,now=researcher;work=tmp_path/name
    session=None
    if protection=='stop':(tmp_path/'STOP').touch()
    if protection=='pause':(work/'.runtime/PAUSE').touch()
    if protection=='supervisor':monkeypatch.setattr('supervision.sleeping',lambda work:True)
    if protection=='gpu':save(work/'.runtime/gpu-jobs/experiment.json',dict(pid=os.getpid(),token=pid_token(os.getpid())))
    if protection in {'live_cli','orphan'}:
        session=SimpleNamespace(process=SimpleNamespace(pid=999999999,poll=lambda:None if protection=='live_cli' else 1))
    if protection=='orphan':monkeypatch.setattr('live_watchdog.group_has_work',lambda group,excluded:True)
    sustained_failure(m,name,now,session=session)
    assert m.states[name]['token']=='saved-research-conversation'
    # Synthetic retry-budget exhaustion must not bypass a dead wrapper's group.
    if protection!='live_cli':
        fail(m,name,now,fresh=True,advance=False)
        assert m.states[name]['token']=='saved-research-conversation'


def test_exhaustion_without_new_execution_is_not_an_extra_failure(researcher):
    m,name,now=researcher
    fail(m,name,now,delay=3600)
    for _ in range(8):fail(m,name,now,fresh=True,advance=False)
    assert m.states[name]['session_failure_streak']['failures']==1
    assert m.states[name]['token']=='saved-research-conversation'


def test_greeting_probe_does_not_skip_untried_backup(researcher,monkeypatch):
    m,name,now=researcher
    fail(m,name,now,route=0)
    m.states[name]['route_offset']=0
    monkeypatch.setattr(m,'_probe_primary',lambda worker:pytest.fail('Untried backup must get a real continuation.'))
    assert build(m,name,count=2).env['ANTHROPIC_API_KEY']=='backup'


def test_failure_streak_and_old_session_survive_controller_restart(researcher,tmp_path):
    m,name,now=researcher
    for index in range(3):fail(m,name,now,route=index%2)
    fresh=ResearchManager(tmp_path,m.config,m.env)
    try:
        for index in range(3):fail(fresh,name,now,route=index%2)
        assert fresh.states[name]['token'] is None
        launch=fresh.states[name]['launches']
        transcript=tmp_path/name/'.runtime'/f'launch_{launch:06d}'/'transcript.jsonl'
        transcript.parent.mkdir(parents=True,exist_ok=True)
        transcript.write_text(json.dumps({'type':'result','session_id':'saved-research-conversation'})+'\n')
        # The old transcript must not resurrect the discarded resume token.
        restarted=ResearchManager(tmp_path,m.config,m.env)
        try:
            assert restarted.states[name]['token'] is None
            assert restarted.states[name]['session_recovery']['previous_token']=='saved-research-conversation'
        finally:restarted.close()
    finally:fresh.close()


def test_fresh_native_launch_continues_existing_workspace(researcher,tmp_path):
    m,name,now=researcher;state=m.states[name];work=tmp_path/name
    (work/'CONTINUATION.md').write_text('Read result.csv, keep the existing account.')
    # The fake CLI's second invocation responds successfully.
    (work/'counter').write_text('1')
    sustained_failure(m,name,now)
    assert m.launch_cli_job(name)
    drain(m)
    assert (work/'prompt2').read_text()=='fresh'
    assert state['status']=='continuing'
    assert state['last_success']==now[0]
    assert 'session_unavailable' not in state
    prompt=(work/'.runtime'/f"launch_{state['launches']:06d}"/'prompt.txt').read_text()
    assert 'CONTINUATION.md' in prompt and 'session-recovery/' in prompt
    assert (work/'CONTINUATION.md').read_text()=='Read result.csv, keep the existing account.'


@pytest.mark.parametrize('kind',['codex','claude','gemini'])
def test_recovery_policy_applies_to_every_worker_family(researcher,kind):
    m,name,now=researcher
    m.route_lists[kind]=m.route_lists['claude']
    m.worker(name)['kind']=kind
    sustained_failure(m,name,now)
    assert m.states[name]['token'] is None
    assert m.states[name]['session_recovery']['failures']==6


@pytest.mark.parametrize('step',[
    {'step_type':'agent_response','state':'ACTIVE','text_delta':'The result supports hypothesis B.'},
    {'step_type':'tool','state':'DONE','tool_name':'read_file'},
])
def test_gemini_response_or_finished_tool_rearms_failure_window(researcher,step):
    m,name,now=researcher
    for index in range(5):fail(m,name,now,route=index%2)
    m._record_gemini_event(name,{'step_update':step},now[0])
    fail(m,name,now,route=1)
    assert m.states[name]['token']=='saved-research-conversation'
    assert m.states[name]['session_failure_streak']['failures']==1


def test_gemini_error_and_stream_events_are_not_research_progress(researcher):
    m,name,now=researcher
    for index in range(6):
        m._record_gemini_event(name,{'event':'result','result':{'status':'ERROR','response':'HTTP 524'}},now[0])
        m._record_gemini_event(name,{'step_update':{'step_type':'agent_response','state':'ACTIVE'}},now[0])
        fail(m,name,now,route=index%2)
    assert m.states[name]['token'] is None


def test_exited_wrapper_with_real_orphan_experiment_is_protected(researcher,tmp_path):
    m,name,now=researcher
    script="import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])"
    wrapper=subprocess.Popen([sys.executable,'-c',script],start_new_session=True)
    try:
        wrapper.wait(timeout=5)
        session=SimpleNamespace(process=wrapper)
        sustained_failure(m,name,now,session=session)
        assert m.states[name]['token']=='saved-research-conversation'
        fail(m,name,now,fresh=True,advance=False)
        assert m.states[name]['token']=='saved-research-conversation'
    finally:
        try:os.killpg(wrapper.pid,signal.SIGTERM)
        except ProcessLookupError:pass

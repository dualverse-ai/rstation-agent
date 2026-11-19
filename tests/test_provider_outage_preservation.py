"""Explicit provider outages never justify discarding a research conversation."""
import json
from types import SimpleNamespace

import pytest

from test_session_recovery import researcher, fail, sustained_failure
from recovery import ResearchManager
from station.workers.job_manager import CliJobFailure


def terminal_session(tmp_path, events, stderr=''):
    transcript=tmp_path/'outage-transcript.jsonl'
    transcript.write_text(''.join(json.dumps(e)+'\n' for e in events))
    error=tmp_path/'outage-stderr.txt';error.write_text(stderr)
    return SimpleNamespace(backend='claude',transcript_path=str(transcript),
                           stderr_path=str(error),transcript_idle_timeout_reason=None)


@pytest.mark.parametrize('event',[
    {'type':'system','subtype':'api_retry','error_status':503},
    {'type':'assistant','error':'unknown','message':{'content':[{'type':'text','text':'API Error: 503 unavailable'}]}},
    {'type':'result','is_error':True,'result':'HTTP 502 Bad Gateway'},
    {'type':'result','is_error':True,'result':'HTTP 429 Too Many Requests'},
    {'type':'result','is_error':True,'result':{'error':{'type':'overloaded_error'}}},
    {'type':'turn.failed','error':{'status_code':524}},
])
@pytest.mark.parametrize('fresh',[False,True])
def test_native_provider_failures_keep_original_session(researcher,tmp_path,event,fresh):
    m,name,now=researcher;state=m.states[name]
    session=terminal_session(tmp_path,[event])
    for i in range(12):
        state.update(launches=state['launches']+1,route_index=i%2)
        failure=m._classify_cli_job_failure(name,session)
        assert failure.category=='provider_unavailable' and failure.is_retryable_infra
        failure.resume_token=state['token'];failure.resume_count=1
        method=m._schedule_cli_job_fresh_attempt if fresh else m._schedule_cli_job_resume
        method(name,session,failure)
        now[0]+=4000
    assert state['token']=='saved-research-conversation'
    assert state['status']=='retrying' and state['next']>now[0]-4000
    assert 'session_recovery' not in state and not state.get('session_unavailable')
    assert 'session_failure_streak' not in state
    assert not (tmp_path/name/'.runtime/session-recovery').exists()


def test_legacy_http_error_and_budget_exhaustion_survive_restart(researcher,tmp_path):
    m,name,now=researcher;state=m.states[name]
    # Old state may already be one execution short of automatic context loss.
    for i in range(5):fail(m,name,now,route=i%2)
    state['last_error']='Claude API retry, HTTP 503'
    fail(m,name,now,route=1)
    restarted=ResearchManager(tmp_path,m.config,m.env)
    try:
        # Shared lifecycle's synthetic exhaustion has no transcript or HTTP text.
        failure=CliJobFailure('resume_limit_exceeded',False,'Resume budget exhausted.')
        for _ in range(10):
            restarted._on_cli_job_attempts_exhausted(name,None,failure)
            now[0]+=4000
        saved=restarted.states[name]
        assert saved['token']=='saved-research-conversation'
        assert saved['status']=='retrying' and 'session_recovery' not in saved
    finally:restarted.close()


def test_progress_rearms_unknown_session_failure_without_stale_http_error(researcher):
    m,name,now=researcher;state=m.states[name]
    state['last_error']='Claude API retry, HTTP 503'
    sustained_failure(m,name,now)
    assert state['token']=='saved-research-conversation'
    state['last_response']=now[0]
    # The old display error remains; it must not block unrelated later recovery.
    sustained_failure(m,name,now)
    assert state['token'] is None
    assert state['session_recovery']['failures']==6


def test_proven_missing_session_still_recovers_after_outage(researcher,tmp_path):
    m,name,now=researcher;state=m.states[name]
    state['last_error']='Claude API retry, HTTP 503'
    fail(m,name,now)
    session=terminal_session(tmp_path,[{'type':'result','is_error':True,
                                      'result':'No conversation found with session ID abc'}])
    failure=m._classify_cli_job_failure(name,session)
    assert failure.category=='session_unavailable'
    m._schedule_cli_job_fresh_attempt(name,session,failure)
    assert state['token'] is None and state['session_unavailable']


def test_research_text_about_http_errors_is_not_provider_evidence(researcher,tmp_path):
    m,name,_=researcher
    session=terminal_session(tmp_path,[{'type':'assistant','message':{'content':[
        {'type':'text','text':'The experiment simulates HTTP 503 and overloaded_error.'}]}},
        {'type':'result','is_error':True,'result':'Unspecified failure'}])
    assert m._classify_cli_job_failure(name,session).category=='infra_transient'

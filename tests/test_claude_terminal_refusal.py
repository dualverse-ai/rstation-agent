import json
import os
import time
from types import SimpleNamespace
import pytest
from test_workers import ResearchManager, cfg, env
from fleet_collect import collect, native_terminal_refusal, error
from fleet_dashboard import health

REFUSAL={'type':'result','subtype':'success','is_error':True,'stop_reason':'refusal','result':"API Error: Opus 5.5’s safeguards flagged this message PRIVATE"}
RETRY={'type':'system','subtype':'api_retry','error_status':503}

@pytest.mark.parametrize('later,expected',[(None,True),({'type':'result','is_error':False,'result':'Done'},False),({'type':'result','is_error':True,'result':'HTTP 429'},False)])
@pytest.mark.parametrize('mode',['resume','fresh'])
def test_terminal_refusal_overrides_retry_without_changing_session(tmp_path,later,expected,mode):
    config=cfg(tmp_path,'claude');config['claude_context_tokens']=500000
    m=ResearchManager(tmp_path,config,env(tmp_path,'claude'))
    events=[RETRY,REFUSAL]+([later] if later else [])
    p=tmp_path/'events.jsonl';p.write_text(''.join(json.dumps(e)+'\n' for e in events))
    session=SimpleNamespace(backend='claude',transcript_path=str(p),stderr_path=str(tmp_path/'none'),transcript_idle_timeout_reason=None)
    try:
        state=m.states['claude_1'];state.update(token='saved',spawn=1)
        failure=m._classify_cli_job_failure('claude_1',session)
        assert (failure.category=='model_refusal')==expected
        if expected:
            assert not m.completed(session)
            getattr(m,'_schedule_cli_job_resume' if mode=='resume' else '_schedule_cli_job_fresh_attempt')('claude_1',session,failure)
            assert state['token']=='saved' and state['blocked_reason']=='model_refusal'
            assert not state.get('session_unavailable')
            assert not m.launch_cli_job('claude_1')
    finally:m.close()


def test_collector_uses_current_terminal_refusal_without_exporting_text(tmp_path):
    root=tmp_path/'root';run=root/'agent_data/run';rt=run/'claude_1/.runtime';launch=rt/'launch_000105';launch.mkdir(parents=True)
    (run/'run.json').write_text(json.dumps({'workers':[{'name':'claude_1','kind':'claude','model':'opus'}],'problem':'math'}))
    started=time.time()-30
    state={'status':'blocked','started':started,'launches':105,'last_error':'HTTP 503','last_failure_category':'provider_unavailable','last_response':started}
    (rt/'state.json').write_text(json.dumps(state))
    p=launch/'transcript.jsonl';p.write_text(''.join(json.dumps(e)+'\n' for e in [RETRY,REFUSAL]))
    result=collect([str(root)],'h6');row=result['workers'][0]
    assert row['error']=={'category':'model_refusal','http_status':None}
    assert row['native_provider_retry'] is None
    assert 'PRIVATE' not in json.dumps(result)
    assert health(row,time.time())==('transient_error','Native model refusal; operator review required')
    row['last_response']=time.time()-21601
    assert health(row,time.time())[0]=='needs_attention'
    assert not native_terminal_refusal(rt,dict(state,launches=106),'claude',time.time())
    assert not native_terminal_refusal(rt,state,'gemini',time.time())
    os.utime(p,(started-1,started-1))
    assert not native_terminal_refusal(rt,state,'claude',time.time())
    p.write_text(json.dumps(REFUSAL)+'\n'+json.dumps({'type':'result','is_error':False})+'\n')
    assert not native_terminal_refusal(rt,state,'claude',time.time())
    p.write_text(json.dumps(REFUSAL))
    assert not native_terminal_refusal(rt,state,'claude',time.time())
    assert error(dict(state,blocked_reason='model_refusal'))=={'category':'model_refusal','http_status':None}

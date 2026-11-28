from types import SimpleNamespace
import json
import pytest
from claude_preference import Preference
from live_cli import LiveCLI

SUCCESS={'type':'result','subtype':'success','is_error':False}
def response(model='claude-opus-5',stop='end_turn'):
    return {'type':'assistant','message':{'model':model,'stop_reason':stop}}


def test_completed_native_fallback_restores_preference_once_after_cooldown():
    p=Preference()
    p.observe({'type':'system','subtype':'model_refusal_fallback'},100)
    p.observe(response(),110);p.observe(SUCCESS,111)
    assert p.select('claude-opus-5-5',idle=True,now=289) is None
    assert p.select('claude-opus-5-5',idle=False,now=290) is None
    request=p.select('claude-opus-5-5',idle=True,now=290)
    assert request['request']=={'subtype':'set_model','model':'claude-opus-5-5'}
    assert p.waiting(294)
    p.observe({'type':'control_response','response':{'request_id':'unrelated','subtype':'success'}},294)
    assert p.waiting(294)
    p.observe({'type':'control_response','response':{'request_id':request['request_id'],'subtype':'success'}},294)
    assert not p.waiting(294)
    assert p.select('claude-opus-5-5',idle=True,now=1000) is None

@pytest.mark.parametrize('events',[
    [response(stop='tool_use'),SUCCESS],
    [response(stop='refusal'),{'type':'result','subtype':'success','is_error':True,'stop_reason':'refusal'}],
    [response(),{'type':'result','subtype':'error','is_error':True}],
    [response('claude-opus-5-5'),SUCCESS],
    [response('unknown'),SUCCESS],
    [dict(response(),parent_tool_use_id='child'),SUCCESS],
    [response(),SUCCESS,{'type':'user'}],
    [SUCCESS],
])
def test_no_selection_for_refusal_error_tools_pending_input_or_unknown_model(events):
    p=Preference()
    for e in events:p.observe(e,1)
    assert p.select('claude-opus-5-5',idle=True,now=500) is None


def test_failed_or_missing_ack_does_not_block_research_or_repeat_selection():
    for ack in (None,'error'):
        p=Preference();p.observe(response(),1);p.observe(SUCCESS,2)
        req=p.select('claude-opus-5-5',idle=True,now=181)
        if ack:p.observe({'type':'control_response','response':{'request_id':req['request_id'],'subtype':ack}},182)
        assert not p.waiting(186)
        assert p.select('claude-opus-5-5',idle=True,now=500) is None


def test_live_wrapper_queues_only_native_control_after_success(tmp_path):
    args=SimpleNamespace(workspace=str(tmp_path),kind='claude',model='claude-opus-5-5')
    cli=LiveCLI(args)
    cli.busy=True
    cli.stream_event(response())
    cli.claude_preference.not_before=0
    cli.stream_event(SUCCESS)
    sent=json.loads(cli.output)
    assert sent['type']=='control_request' and sent['request']['subtype']=='set_model'
    assert not cli.busy and not cli.sent
    cli.output.clear()
    with pytest.raises(RuntimeError):
        cli.stream_event({'type':'result','subtype':'success','is_error':True,'stop_reason':'refusal'})
    assert not cli.output

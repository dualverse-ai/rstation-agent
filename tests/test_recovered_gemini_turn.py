import pytest
from antigravity import completed_turn


def step(kind,index,**fields):
 return {'step_update':{'step_type':kind,'step_index':index,'state':'DONE',**fields}}


def recovered():
 return [step('user_input',1),step('agent_response',2,text_delta='Earlier reply.'),
         step('tool',3),step('agent_response',4,state='ERROR'),
         step('error_message',5),step('error_message',6),
         step('agent_response',7,state='ACTIVE',text_delta='Recovered '),
         step('agent_response',7,text_delta='reply.'),
         {'event':'result','result':{'status':'SUCCESS','response':'Earlier reply.\nRecovered reply.'}}]


@pytest.mark.parametrize('fault',[None,'fresh_error','explicit_error','unknown_error_field','missing_index','missing_user',
 'partial','trailing_tool','partial_finish','mismatch','error_result','terminal_error','refusal',
 'old_index','duplicate_reply','result_before_reply','new_user','active_error'])
def test_only_demonstrably_recovered_bare_errors_complete(fault):
 events=recovered()
 if fault=='fresh_error':events.insert(-1,step('error_message',8))
 if fault=='explicit_error':events[4]['step_update']['text_delta']='HTTP 403 refusal'
 if fault=='unknown_error_field':events[4]['step_update']['detail']='unrecognized'
 if fault=='missing_index':events[4]['step_update'].pop('step_index')
 if fault=='missing_user':events.pop(0)
 if fault=='partial':events[-2]['step_update']['state']='ACTIVE'
 if fault=='trailing_tool':events.insert(-1,step('tool',8))
 if fault=='partial_finish':events.insert(-1,step('finish',8,state='ACTIVE'))
 if fault=='mismatch':events[-1]['result']['response']='Unrelated terminal text'
 if fault=='error_result':events[-1]['result']['status']='ERROR'
 if fault=='terminal_error':events[-1]['result']['error']='current failure'
 if fault=='refusal':
  events[-2]['step_update']['text_delta']='Request is blocked due to the account restrictions for prohibited use policy'
  events[-3]['step_update']['text_delta']=''
  events[-1]['result']['response']=events[-2]['step_update']['text_delta']
 if fault=='old_index':
  events[-2]['step_update']['step_index']=4;events[-3]['step_update']['step_index']=4
 if fault=='duplicate_reply':events.insert(-1,step('agent_response',7,text_delta=''))
 if fault=='result_before_reply':events.insert(-2,events.pop())
 if fault=='new_user':events.append(step('user_input',8))
 if fault=='active_error':events[4]['step_update']['state']='ACTIVE'
 assert completed_turn(events) is (fault is None)


def test_last_reply_match_and_multiple_recoveries():
 events=recovered();events[-1]['result']['response']='Recovered reply.'
 assert completed_turn(events)
 events[-1:-1]=[step('error_message',8),step('agent_response',9,text_delta='Second recovery.')]
 events[-1]['result']['response']='Second recovery.'
 assert completed_turn(events)


def test_success_without_error_keeps_existing_contract():
 assert completed_turn([{'event':'result','result':{'status':'SUCCESS','response':'Hello'}}])


def test_error_finish_success_without_completed_reply_stays_failed():
 assert not completed_turn([step('user_input',1),step('error_message',2),step('finish',3),
                           {'event':'result','result':{'status':'SUCCESS','response':'Done'}}])


def test_success_can_match_native_raw_concatenation_including_blank_replies():
 events=recovered()
 events[1]['step_update']['text_delta']='Earlier reply.\n'
 events[3:3]=[step('agent_response',10,text_delta='\n')]
 events[-1]['result']['response']='Earlier reply.\n\nRecovered reply.'
 assert completed_turn(events)
 events[-1]['result']['response']='Earlier reply. Recovered reply.'
 assert not completed_turn(events)


@pytest.mark.parametrize('structured',[False,True])
def test_live_projection_preserves_explicit_error_evidence(tmp_path,monkeypatch,structured):
 import live_cli
 from test_messages import worker
 monkeypatch.setattr(live_cli,'emit',lambda event:None)
 cli=worker(tmp_path,'gemini');finished=[]
 monkeypatch.setattr(cli,'finish',lambda:finished.append(True))
 events=recovered()
 if structured:events[4]['step_update']['error']={'message':'PRIVATE_ERROR_BODY'}
 for event in events[:-1]:cli.stream_event(event)
 import json
 assert 'PRIVATE_ERROR_BODY' not in json.dumps(cli.gemini_events)
 if structured:
  with pytest.raises(RuntimeError,match='Gemini turn failed'):cli.stream_event(events[-1])
  assert not finished and not completed_turn(events)
 else:
  cli.stream_event(events[-1]);assert finished==[True] and completed_turn(events)

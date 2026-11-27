import pytest
from fleet_dashboard import ATTENTION_SECONDS,Store,attention,health
from test_fleet_dashboard import row


@pytest.mark.parametrize('family',['gemini','claude','codex'])
@pytest.mark.parametrize('state',['running','retrying','blocked','paused','stopped','starting'])
@pytest.mark.parametrize('delta,expected',[(-1,False),(0,True),(1,True)])
def test_six_hour_boundary_across_states_and_models(family,state,delta,expected):
 now=100000;last=now-ATTENTION_SECONDS-delta
 w=row(family=family,state=state,sampled_at=now,last_response=last)
 result=attention(w,now)
 assert result['attention_known'] and result['needs_attention'] is expected
 assert result['attention_since']==last and result['attention_due_at']==last+21600
 if state=='blocked':assert health(w,now)[0]=='down'


@pytest.mark.parametrize('change',[{'sampled_at':99699},{'sampled_at':100001},{'sampled_at':None},{'last_response':None},{'last_response':100001}])
def test_unknown_or_future_evidence_does_not_alert(change):
 w=row(sampled_at=100000,last_response=1);w.update(change)
 result=attention(w,100000)
 assert not result['attention_known'] and not result['needs_attention']


def test_retired_and_recent_tool_excluded():
 w=row(sampled_at=100000,last_response=1,intentional_stop=True)
 assert not attention(w,100000)['needs_attention']
 w.update(intentional_stop=False,last_tool=99999)
 assert not attention(w,100000)['needs_attention']


def test_first_seen_and_progress_survive_service_and_worker_restart(tmp_path):
 db=tmp_path/'history';store=Store(db)
 def record(at,**kw):
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=None,**kw)]})
 record(100000,state='blocked');w=store.view(100000)['workers'][0]
 assert w['attention_since']==100000 and not w['needs_attention'] and w['state']=='blocked'
 store=Store(db);record(121599,state='starting',started_at=121599)
 assert not store.view(121599)['workers'][0]['needs_attention']
 record(121600,state='blocked',started_at=121600)
 view=store.view(121600);w=view['workers'][0]
 assert w['needs_attention'] and w['attention_since']==100000
 assert view['need_attention']==1 and view['families'][0]['need_attention']==1
 record(121601,last_tool=121601)
 assert not store.view(121601)['workers'][0]['needs_attention']
 store=Store(db);record(121721,last_tool=None,started_at=121721)
 assert store.view(121721)['workers'][0]['attention_since']==121601
 assert not store.view(122022)['workers'][0]['attention_known']


def test_gpu_progress_resets_attention_without_faking_native_response(tmp_path):
 store=Store(tmp_path/'history')
 def record(at,step=None):
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=1,foreground_job=step is not None,job_progress_samples=[] if step is None else [{'id':'job','step':step}])]})
 record(100000,10);assert store.view(100000)['workers'][0]['needs_attention']
 record(100120,20);w=store.view(100120)['workers'][0]
 assert not w['needs_attention'] and w['attention_since']==100120 and w['last_response']==1
 record(100240);w=store.view(100240)['workers'][0]
 assert not w['needs_attention'] and w['health']=='down'
 record(121720);assert store.view(121720)['workers'][0]['needs_attention']


def test_existing_history_migrates_without_resetting_first_observation(tmp_path):
 store=Store(tmp_path/'history')
 import json
 with store.connect() as con:
  con.execute('INSERT INTO current VALUES(?,?)',('one',json.dumps(row(sampled_at=100000,last_response=None))))
  con.execute('INSERT INTO samples VALUES(?,?,?,?,?,?)',('one',90000,'unknown',None,None,0))
 store.record({'host':'h4','sampled_at':111600,'workers':[row(sampled_at=111600,last_response=None)]})
 w=store.view(111600)['workers'][0]
 assert w['attention_since']==90000 and w['needs_attention']

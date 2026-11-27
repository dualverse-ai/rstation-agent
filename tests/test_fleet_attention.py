import pytest
from fleet_dashboard import ATTENTION_SECONDS,Store,attention,health
from test_fleet_dashboard import row


@pytest.mark.parametrize('family',['gemini','claude','codex'])
@pytest.mark.parametrize('state',['running','continuing','retrying','blocked','paused','stopped','starting','unknown'])
@pytest.mark.parametrize('delta,expected',[(-1,False),(0,True),(1,True)])
def test_six_hour_boundary_across_states_and_models(family,state,delta,expected):
 now=100000;last=now-ATTENTION_SECONDS-delta
 w=row(family=family,state=state,sampled_at=now,last_response=last,last_tool=now,foreground_job=True,last_job_progress=now)
 result=attention(w,now)
 assert result['attention_known'] and result['needs_attention'] is expected
 assert result['attention_since']==last and result['attention_due_at']==last+21600
 assert (health(w,now)[0]=='needs_attention') is expected
 if not expected and state not in ('running','continuing'):assert health(w,now)[0]=='transient_error'


@pytest.mark.parametrize('sample',[99699,100001,None])
@pytest.mark.parametrize('response,expected',[(99999,'transient_error'),(78400,'needs_attention')])
def test_stale_or_missing_host_does_not_hide_worker_or_suppress_red(sample,response,expected):
 w=row(sampled_at=sample,last_response=response)
 assert health(w,100000)[0]==expected
 assert attention(w,100000)['needs_attention']==(expected=='needs_attention')


@pytest.mark.parametrize('response',[None,100001])
def test_no_valid_response_uses_first_observation(response):
 w=row(sampled_at=100000,last_response=response,first_observed_at=78400,last_tool=100000)
 assert attention(w,100000)['needs_attention']
 assert attention(w,100000)['attention_since']==78400
 w.pop('first_observed_at')
 assert health(w,100000)[0]=='working'  # A completed tool, but no invented clock.
 assert not attention(w,100000)['attention_known']


def test_retired_always_separate_even_stale():
 w=row(sampled_at=1,last_response=1,intentional_stop=True)
 assert health(w,100000)[0]=='intentional'
 assert not attention(w,100000)['needs_attention']


def test_clock_survives_restarts_and_only_real_responses_reset(tmp_path):
 db=tmp_path/'history';store=Store(db)
 def record(at,**kw):
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,**{'last_response':None,**kw})]})
 record(100000,state='blocked')
 assert store.view(100000)['workers'][0]['attention_since']==100000
 store=Store(db);record(121599,state='starting',started_at=121599)
 assert not store.view(121599)['workers'][0]['needs_attention']
 record(121600,state='blocked',started_at=121600,last_tool=121600)
 assert store.view(121600)['workers'][0]['needs_attention']
 record(121601,last_response=121601)
 assert store.view(121601)['workers'][0]['health']=='working'
 store=Store(db);record(121721,last_tool=121721,started_at=121721)
 assert store.view(121721)['workers'][0]['attention_since']==121601
 assert store.view(143201)['workers'][0]['needs_attention']


def test_gpu_progress_never_resets_response_clock(tmp_path):
 store=Store(tmp_path/'history')
 for at,step in [(100000,10),(100120,20)]:
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=78400,last_tool=at,foreground_job=True,job_progress_samples=[{'id':'job','step':step}])]})
 w=store.view(100120)['workers'][0]
 assert w['last_job_progress']==100120 and w['last_tool']==100120
 assert w['needs_attention'] and w['attention_since']==78400 and w['last_response']==78400


def test_existing_history_migrates_without_crediting_old_tool_clock(tmp_path):
 store=Store(tmp_path/'history')
 import json
 with store.connect() as con:
  con.execute('INSERT INTO current VALUES(?,?)',('one',json.dumps(row(sampled_at=100000,last_response=None,attention_progress_at=111599))))
  con.execute('INSERT INTO samples VALUES(?,?,?,?,?,?)',('one',90000,'unknown',None,None,0))
 store.record({'host':'h4','sampled_at':111600,'workers':[row(sampled_at=111600,last_response=None)]})
 w=store.view(111600)['workers'][0]
 assert w['attention_since']==90000 and w['needs_attention']


def test_global_family_counts_and_hook_exactly_partition_every_row(tmp_path):
 store=Store(tmp_path/'history');workers=[]
 for family in ('gemini','claude','codex'):
  for i,kw in enumerate([{}, {'state':'blocked'}, {'last_response':78400,'last_tool':100000}, {'intentional_stop':True,'sampled_at':1}]):
   workers.append(row(id=family+str(i),family=family,sampled_at=100000,last_response=99999))
   workers[-1].update(kw)
 store.record({'host':'h4','sampled_at':100000,'workers':workers})
 view=store.view(100000)
 assert view['counts']=={'working':3,'transient_error':3,'needs_attention':3,'intentional':3}
 assert view['active_total']==9 and view['need_attention']==3
 for family in view['families']:
  assert family['active_total']==3 and family['total']==4
  assert family['counts']=={'working':1,'transient_error':1,'needs_attention':1,'intentional':1}
  assert family['need_attention']==1
 for w in view['workers']:
  assert w['needs_attention']==(w['health']=='needs_attention')

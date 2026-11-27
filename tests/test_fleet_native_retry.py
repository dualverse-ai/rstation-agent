import json
import os
import pytest
import fleet_collect as collect
from fleet_dashboard import Store,health
from test_fleet_dashboard import row

NOW=100000
RETRY={'type':'system','subtype':'api_retry','error_status':503,'attempt':2,'error':'PRIVATE_PROVIDER_TEXT'}
RESPONSE={'type':'assistant','message':{'content':[{'type':'text','text':'PRIVATE_RESPONSE'}]}}

@pytest.fixture
def native(tmp_path,monkeypatch):
 runtime=tmp_path/'.runtime';path=runtime/'launch_000413/transcript.jsonl';path.parent.mkdir(parents=True)
 state={'launches':413,'started':99000,'pid':123,'pid_token':'token','last_response':99900,'status':'running'}
 monkeypatch.setattr(collect,'alive',lambda pid,token:pid==123 and token=='token')
 def write(events,mtime=NOW):
  path.write_text(''.join(json.dumps(e)+'\n' for e in events));os.utime(path,(mtime,mtime))
 def read():return collect.native_provider_retry(runtime,state,'claude',NOW)
 return runtime,path,state,write,read


def test_reported_tool_then_retry_sequence_is_yellow_and_private(native):
 runtime,path,state,write,read=native
 write([RETRY,RESPONSE,{'type':'result','subtype':'success'},RESPONSE,{'type':'user','message':{'content':[{'type':'tool_result','content':'PRIVATE_TOOL'}]}},RETRY])
 retry=read();assert retry['http_status']==503
 assert set(retry)=={'id','at','http_status'} and 'PRIVATE' not in json.dumps(retry)
 w=row(sampled_at=NOW,last_response=99900,last_tool=99990,state='running',native_provider_retry=retry)
 assert health(w,NOW)==('transient_error','Native provider retry · HTTP 503')
 w['last_response']=NOW-21600
 assert health(w,NOW)[0]=='needs_attention'


@pytest.mark.parametrize('success',[RESPONSE,{'type':'assistant','message':{'content':[{'type':'tool_use','id':'tool'}]}},{'type':'result','subtype':'success'},{'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':'ok'}}}])
def test_success_after_retry_clears_but_success_before_does_not(native,success):
 _,_,_,write,read=native
 write([RETRY,success]);assert read() is None
 write([success,RETRY]);assert read() is not None


@pytest.mark.parametrize('event',[{'type':'assistant','error':'overloaded','message':{'content':[{'type':'text','text':'error'}]}},{'type':'assistant','message':{'content':[]}},{'type':'result','subtype':'error','is_error':True},{'type':'system','subtype':'status'},{'type':'user','message':{'content':'owner message'}},{'type':'stream_event','event':{'type':'ping'}}])
def test_unrelated_or_failure_events_do_not_clear_retry(native,event):
 _,_,_,write,read=native;write([RETRY,event]);assert read()


@pytest.mark.parametrize('change',['old_launch','dead_process','stale_file','future_file','before_start','stale_event','future_event','new_session','partial'])
def test_stale_or_unbound_retry_ignored(native,change):
 _,path,state,write,read=native;write([RETRY])
 if change=='old_launch':state['launches']=414
 if change=='dead_process':state['pid_token']='wrong'
 if change=='stale_file':write([RETRY],NOW-301)
 if change=='future_file':write([RETRY],NOW+1)
 if change=='before_start':state['started']=NOW+1
 if change=='stale_event':write([dict(RETRY,timestamp=NOW-301)])
 if change=='future_event':write([dict(RETRY,timestamp=NOW+1)])
 if change=='new_session':write([RETRY,{'type':'system','subtype':'init'}])
 if change=='partial':
  path.write_text(json.dumps(RETRY));os.utime(path,(NOW,NOW))
 assert read() is None


def test_repeat_or_unrelated_writes_do_not_renew_observed_retry(tmp_path):
 store=Store(tmp_path/'db')
 def record(at,ident):
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=99999,family='claude',native_provider_retry={'id':ident,'at':at,'http_status':503})]})
 record(NOW,'retry1');record(NOW+300,'retry1')
 assert store.view(NOW+300)['workers'][0]['health']=='transient_error'
 record(NOW+301,'retry1');w=store.view(NOW+301)['workers'][0]
 assert w['health']=='working' and w['native_provider_retry']['at']==NOW
 assert 'id' not in w['native_provider_retry']
 record(NOW+302,'retry2');assert store.view(NOW+302)['workers'][0]['health']=='transient_error'


def test_full_collector_exposes_retry_without_text(tmp_path,monkeypatch):
 root=tmp_path/'kit';run=root/'agent_data/run';runtime=run/'claude_1/.runtime';launch=runtime/'launch_000001';launch.mkdir(parents=True)
 (run/'run.json').write_text(json.dumps({'workers':[{'name':'claude_1','kind':'claude'}]}))
 (runtime/'state.json').write_text(json.dumps({'status':'running','pid':123,'pid_token':'token','started':99000,'launches':1,'last_response':99999}))
 path=launch/'transcript.jsonl';path.write_text(json.dumps(RETRY)+'\n');os.utime(path,(NOW,NOW))
 monkeypatch.setattr(collect.time,'time',lambda:NOW)
 monkeypatch.setattr(collect,'alive',lambda pid,token:pid==123 and token=='token')
 monkeypatch.setattr(collect,'process_provider_environment',lambda *a:{})
 snapshot=collect.collect([root],'h4');w=snapshot['workers'][0]
 assert w['native_provider_retry']['http_status']==503
 assert w['state']=='running' and health(w,NOW)[0]=='transient_error'
 assert 'PRIVATE_PROVIDER_TEXT' not in json.dumps(snapshot)

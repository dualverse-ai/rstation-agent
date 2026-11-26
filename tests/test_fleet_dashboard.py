import json
from pathlib import Path
import sys
import threading
import urllib.request
import urllib.error
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import pytest
from fleet_collect import collect
from fleet_dashboard import health,Store,handler,ThreadingHTTPServer


def row(**kw):
 return {'id':'one','run':'run','worker':'gemini_1','host':'h4','state':'running','sampled_at':20000,'last_response':19900,'last_tool':None,'foreground_job':False,'intentional_stop':False,'family':'gemini','model':'carbon','route':1,'started_at':19800,'gpu':[],**kw}


@pytest.mark.parametrize('change,expected',[
 ({},'working'),({'last_response':5000},'down'),({'last_response':None},'unknown'),
 ({'sampled_at':19000},'unknown'),({'state':'retrying'},'degraded'),({'state':'blocked'},'down'),
 ({'state':'paused'},'down'),({'state':'paused','intentional_stop':True},'intentional'),
 ({'last_response':16000,'foreground_job':True},'working'),({'last_response':16000},'degraded'),
 ({'last_response':None,'last_tool':19900},'working'),({'process_alive':True,'last_response':5000},'down')])
def test_health_uses_work_not_process(change,expected):assert health(row(**change),20000)[0]==expected


def test_collector_exports_no_credentials_or_conversation(tmp_path):
 root=tmp_path/'toolkit';run=root/'agent_data/run';rt=run/'gemini_1/.runtime';rt.mkdir(parents=True)
 (run/'run.json').write_text(json.dumps({'workers':[{'name':'gemini_1','kind':'gemini','model':'carbon'}],'problem':'math','secret':'NEVER_EXPORT','storage_gb':50}))
 (rt/'state.json').write_text(json.dumps({'status':'retrying','last_response':1,'last_error':'HTTP 429 NEVER_EXPORT secret research','last_response_excerpt':'NEVER_EXPORT','token':'NATIVE-SECRET'}))
 (rt/'operator_messages.json').write_text(json.dumps({'messages':[{'text':'NEVER_EXPORT','status':'queued','source':'secret'}]}))
 (rt/'PAUSE').touch()
 result=collect([str(root)],'h4');text=json.dumps(result)
 assert 'NEVER_EXPORT' not in text and 'NATIVE-SECRET' not in text and 'secret research' not in text
 w=result['workers'][0];assert w['pending_messages']==1 and not w['intentional_stop']
 assert w['error']['http_status']==429


def test_stale_rows_history_gaps_and_coverage(tmp_path):
 store=Store(tmp_path/'db')
 for at in (19000,19120,19900):store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=1)]})
 v=store.view(20000);w=v['workers'][0]
 assert 0<w['observed_hours']<.04 and w['stalled_hours']==w['observed_hours']
 assert any(x is None for x in w['history'])
 assert store.view(20301)['workers'][0]['health']=='unknown'
 assert store.view(20301)['hosts'][0]['stale']


def test_route_panel_does_not_credit_previous_route_work(tmp_path):
 store=Store(tmp_path/'db');store.record({'host':'h4','sampled_at':20000,'workers':[row(last_response=19000,started_at=19900)]})
 assert store.view(20000)['providers'][0]['last_success'] is None


def test_concurrent_writer_reader(tmp_path):
 store=Store(tmp_path/'db');errors=[]
 def writer():
  try:
   for n in range(30):store.record({'host':'h4','sampled_at':20000+n,'workers':[row(sampled_at=20000+n)]})
  except Exception as e:errors.append(e)
 t=threading.Thread(target=writer);t.start()
 for _ in range(30):store.view(20050)
 t.join();assert not errors
 assert len(store.view(20050)['workers'])==1


def test_http_read_only_loopback_host_guard_and_no_cors(tmp_path):
 store=Store(tmp_path/'db');server=ThreadingHTTPServer(('127.0.0.1',0),handler(store));t=threading.Thread(target=server.serve_forever);t.start()
 url='http://127.0.0.1:'+str(server.server_port)
 try:
  with urllib.request.urlopen(url+'/api/snapshot') as r:
   assert r.status==200 and 'Access-Control-Allow-Origin' not in r.headers
   assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
  for asset,mime in [('/icon.svg','image/svg+xml'),('/favicon.ico','image/x-icon')]:
   with urllib.request.urlopen(url+asset) as response:
    assert response.status==200 and response.headers['Content-Type'].startswith(mime)
    assert response.read()
  for request in [urllib.request.Request(url,headers={'Host':'evil.example'}),urllib.request.Request(url+'/api/start',method='POST'),urllib.request.Request(url+'/../fleet_dashboard.py')]:
   with pytest.raises(urllib.error.HTTPError):urllib.request.urlopen(request)
 finally:server.shutdown();server.server_close();t.join()


def test_account_name_and_unknown_route(tmp_path):
 root=tmp_path/'kit';run=root/'agent_data/run';work=run/'gemini_1';(work/'.rstation').mkdir(parents=True)
 (run/'run.json').write_text(json.dumps({'workers':[{'name':'gemini_1','kind':'gemini'}]}))
 (work/'.rstation/credentials.json').write_text(json.dumps({'api_key':'DO_NOT_EXPORT','agent_name':'noether_schema'}))
 data=collect([str(root)],'h6');w=data['workers'][0]
 assert w['handle']=='noether_schema' and w['route'] is None
 assert 'DO_NOT_EXPORT' not in json.dumps(data)


def test_unknown_history_does_not_become_outage(tmp_path):
 store=Store(tmp_path/'db');store.record({'host':'h4','sampled_at':20000,'workers':[row(last_response=None)]})
 w=store.view(20000)['workers'][0]
 assert w['history'][-1]=='unknown' and w['stalled_hours']==0


def test_profile_fetch_does_not_block_host_tick(tmp_path,monkeypatch):
 import fleet_dashboard as f
 store=Store(tmp_path/'db');collector=f.Collector(store,{'hosts':{'h4':['unused']}})
 monkeypatch.setattr(f,'collect_host',lambda *args:{'host':'h4','sampled_at':20000,'workers':[row(handle='aster')]})
 def forbidden(*args):raise AssertionError('Profile network call in host collector')
 monkeypatch.setattr(f,'fetch_public',forbidden)
 collector.tick()
 assert len(store.view(20000)['workers'])==1


def test_owned_gpu_job_is_not_assumed_foreground(tmp_path,monkeypatch):
 import fleet_collect as f
 root=tmp_path/'kit';run=root/'agent_data/run';rt=run/'gemini_1/.runtime';(rt/'gpu-jobs').mkdir(parents=True)
 (run/'run.json').write_text(json.dumps({'workers':[{'name':'gemini_1','kind':'gemini'}]}))
 (rt/'state.json').write_text(json.dumps({'status':'running','last_response':1}))
 (rt/'gpu-jobs/job.json').write_text(json.dumps({'helper_pid':123,'helper_token':'tick','gpus':[0]}))
 monkeypatch.setattr(f,'alive',lambda pid,token:pid==123 and token=='tick')
 w=f.collect([str(root)],'h6')['workers'][0]
 assert w['owned_job'] and w['gpu']==[0] and not w['foreground_job']
 assert health(w,w['sampled_at'])[0]!='working'


@pytest.mark.parametrize('workers,status,working,attention',[
 ([row(last_response=19999)],'responding',1,0),
 ([row(last_response=19000,last_tool=19999)],'no_recent_response',1,0),
 ([row(sampled_at=19000,last_response=19999)],'unknown',0,0),
 ([row(last_response=None)],'unknown',0,0),
 ([row(state='paused',intentional_stop=True)],'inactive',0,0),
 ([row(last_response=19999),row(id='two',state='blocked',last_response=100)],'responding',1,1),
])
def test_family_summary_uses_recent_real_responses(workers,status,working,attention):
 from fleet_dashboard import family_summary
 for w in workers:w['health']=health(w,20000)[0]
 summary=family_summary(workers,20000)[0]
 assert summary['family']=='gemini' and summary['status']==status
 assert summary['counts']['working']==working and summary['need_attention']==attention


def test_absent_family_is_unknown_and_future_response_is_not_evidence():
 from fleet_dashboard import family_summary
 assert all(f['status']=='unknown' for f in family_summary([],20000))
 w=row(last_response=21000);w['health']='working'
 assert family_summary([w],20000)[0]['status']=='no_recent_response'

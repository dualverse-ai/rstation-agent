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
 ({},'working'),({'last_response':5000},'transient_error'),({'last_response':None},'transient_error'),
 ({'sampled_at':19000},'transient_error'),({'state':'retrying'},'transient_error'),({'state':'blocked'},'transient_error'),
 ({'state':'paused'},'transient_error'),({'state':'paused','intentional_stop':True},'intentional'),
 ({'last_response':16000,'foreground_job':True},'transient_error'),({'last_response':16000},'transient_error'),
 ({'last_response':None,'last_tool':19900},'working'),({'process_alive':True,'last_response':5000},'transient_error')])
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
 assert store.view(20301)['workers'][0]['health']=='transient_error'
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
 assert w['history'][-1]=='degraded' and w['stalled_hours']==0


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
 ([row(last_response=19999),row(id='two',state='blocked',last_response=100)],'responding',1,0),
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
 assert family_summary([w],20000)[0]['status']=='unknown'


def test_key_identity_never_exports_values_or_url_secrets(tmp_path,monkeypatch):
 import fleet_collect as f
 root=tmp_path/'kit';run=root/'agent_data/run';rt=run/'gemini_1/.runtime';rt.mkdir(parents=True)
 (run/'run.json').write_text(json.dumps({'workers':[{'name':'gemini_1','kind':'gemini','model':'carbon'}]}))
 (run/'controller.json').write_text(json.dumps({'pid':100,'token':'controller'}))
 (rt/'state.json').write_text(json.dumps({'status':'running','pid':101,'pid_token':'native','route_index':1,'started':19000,'last_response':19999}))
 secret='DO_NOT_EXPORT_PRIMARY';backup='DO_NOT_EXPORT_BACKUP'
 env={'RSTATION_WORKER_GEMINI_API_KEY':secret,'RSTATION_WORKER_GEMINI_BASE_URL':'https://relay.example/v1','RSTATION_WORKER_GEMINI_BACKUP_API_KEY':backup,'RSTATION_WORKER_GEMINI_BACKUP_BASE_URL':'https://user:PASS@backup.example/v1?key=QUERY_SECRET'}
 monkeypatch.setattr(f,'process_provider_environment',lambda pid,token:env if pid==100 else {'GEMINI_API_KEY':backup,'GOOGLE_GEMINI_BASE_URL':env['RSTATION_WORKER_GEMINI_BACKUP_BASE_URL']})
 result=f.collect([str(root)],'h6');encoded=json.dumps(result)
 assert all(s not in encoded for s in [secret,backup,'PASS','QUERY_SECRET'])
 keys=result['provider_keys'];assert len(keys)==2
 assert result['workers'][0]['provider_key_id']==next(k['id'] for k in keys if k['route']==2)
 assert next(k['endpoint'] for k in keys if k['route']==2)=='https://backup.example/v1'


def test_key_url_only_exposes_known_path():
 from fleet_collect import safe_endpoint
 assert safe_endpoint('https://relay.example/key-SECRET')=='https://relay.example'
 assert safe_endpoint('file:///private/key') is None
 assert safe_endpoint('https://user:SECRET@relay.example:8443/v1?secret=HIDDEN')=='https://relay.example:8443/v1'


def test_backup_success_does_not_make_primary_healthy():
 from fleet_dashboard import key_observation
 w=row(provider_key_id='backup',started_at=19800,last_response=19999)
 assert key_observation('primary',[w],20000)['status']=='unknown'
 assert key_observation('backup',[w],20000)['status']=='working'
 w['started_at']=20000
 assert key_observation('backup',[w],20000)['status']=='unknown'


def test_provider_errors_and_mixed_results_are_not_hidden():
 from fleet_dashboard import key_observation
 failed=row(provider_key_id='key',started_at=19900,last_response=18000,state='retrying',error={'http_status':429})
 ok=row(id='other',provider_key_id='key',started_at=19900,last_response=19990)
 assert key_observation('key',[failed],20000)['status']=='errors'
 assert key_observation('key',[failed,ok],20000)['status']=='mixed'
 failed['intentional_stop']=True
 assert key_observation('key',[failed],20000)['status']=='unknown'
 ok['sampled_at']=19000
 assert key_observation('key',[ok],20000)['status']=='unknown'


def test_key_history_preserves_success_after_switch_and_has_gaps(tmp_path):
 store=Store(tmp_path/'db');key={'id':'key','name':'Gemini primary','endpoint':'https://relay.example','family':'gemini','route':1,'source':'controller configuration'}
 store.record({'host':'h4','sampled_at':20000,'provider_keys':[key],'workers':[row(provider_key_id='key',last_response=19999)]})
 store.record({'host':'h4','sampled_at':20120,'provider_keys':[key],'workers':[row(provider_key_id='backup',sampled_at=20120,started_at=20100,last_response=20119)]})
 k=store.view(20120)['provider_keys'][0]
 assert k['last_response']==19999 and k['status']=='working' and k['workers']==0
 assert any(v is None for v in k['history'])
 assert store.view(20421)['provider_keys'][0]['status']=='unknown'


def test_concurrent_key_inventory_writes(tmp_path):
 store=Store(tmp_path/'db');errors=[]
 def write(host):
  try:
   for n in range(12):
    key={'id':'same','name':'Claude primary','endpoint':'https://relay.example','family':'claude'}
    store.record({'host':host,'sampled_at':20000+n,'provider_keys':[key],'workers':[row(id=host,host=host,family='claude',provider_key_id='same',sampled_at=20000+n)]})
  except Exception as e:errors.append(e)
 threads=[threading.Thread(target=write,args=(host,)) for host in ['h4','h6']]
 for t in threads:t.start()
 for _ in range(15):store.view(20020)
 for t in threads:t.join()
 assert not errors
 keys=store.view(20020)['provider_keys'];assert len(keys)==1 and keys[0]['hosts']==['h4','h6']


@pytest.fixture(autouse=True)
def no_live_profile_reads_in_tests(monkeypatch):
 import fleet_collect as f
 monkeypatch.setattr(f,'profile_environment',lambda root:{})


def test_unreadable_process_profile_needs_matching_saved_fingerprint(tmp_path,monkeypatch):
 import fleet_collect as f
 monkeypatch.setattr(f,'process_provider_environment',lambda *args:{})
 worker={'name':'gemini_1','kind':'gemini'}
 env={'RSTATION_WORKER_GEMINI_API_KEY':'FIXTURE_KEY','RSTATION_WORKER_GEMINI_BASE_URL':'https://relay.example'}
 state={'route_index':0,'providers':{'pool_sha256':'mismatch'}}
 keys,selected=f.worker_key_inventory(tmp_path,worker,state,{},env)
 assert len(keys)==1 and selected is None
 state['providers']['pool_sha256']=f.pool_fingerprint(f.configured_pool(env,'gemini'),env,'gemini')
 keys,selected=f.worker_key_inventory(tmp_path,worker,state,{},env)
 assert selected['id']==keys[0]['id'] and selected['source']=='matching saved route fingerprint'


def test_unknown_host_does_not_erase_observed_key_success(tmp_path):
 store=Store(tmp_path/'db');key={'id':'shared','name':'Codex primary','endpoint':'https://relay.example','family':'codex'}
 store.record({'host':'h4','sampled_at':20000,'provider_keys':[key],'workers':[row(provider_key_id='shared',last_response=19999)]})
 store.record({'host':'h6','sampled_at':20001,'provider_keys':[key],'workers':[]})
 result=store.view(20001)['provider_keys'][0]
 assert result['status']=='working' and result['history'][-1]=='working'


def test_exact_internal_key_label_survives_collection_and_store(tmp_path,monkeypatch):
 import fleet_collect as f
 root=tmp_path/'kit';run=root/'agent_data/run';rt=run/'claude_1/.runtime';rt.mkdir(parents=True)
 (run/'run.json').write_text(json.dumps({'workers':[{'name':'claude_1','kind':'claude'}]}))
 (run/'controller.json').write_text(json.dumps({'pid':100,'token':'ctl'}))
 env={'RSTATION_WORKER_CLAUDE_API_KEY':'FIXTURE_SECRET','RSTATION_WORKER_CLAUDE_BASE_URL':'https://relay.example'}
 monkeypatch.setattr(f,'process_provider_environment',lambda pid,token:env if pid==100 else {})
 ident=f.key_metadata('FIXTURE_SECRET','https://relay.example','claude',0,'test')['id']
 labels=root/'.provider-labels.json'
 labels.write_text(json.dumps({'labels':{ident:'ikun_claude_rstation','unrelated':'wrong'}}))
 first=f.collect([str(root)],'h4')
 assert first['provider_keys'][0]['label']=='ikun_claude_rstation'
 assert 'FIXTURE_SECRET' not in json.dumps(first)
 store=Store(tmp_path/'db');store.record(first)
 assert store.view()['provider_keys'][0]['labels']==['ikun_claude_rstation']
 labels.write_text(json.dumps({'labels':{ident:'ikun_claude_rstation_2'}}))
 assert f.collect([str(root)],'h4')['provider_keys'][0]['label']=='ikun_claude_rstation_2'
 labels.write_text(json.dumps({'labels':{ident:'sk-secret-looking-value'}}))
 assert 'label' not in f.collect([str(root)],'h4')['provider_keys'][0]


@pytest.mark.parametrize('name',[None,'RSTATION_WORKER_GEMINI_API_KEY','Gemini primary','stephen-rstation-worker-gemini','aabbcc','sk-secret','ikun_gemini','https://provider.example'])
def test_noncanonical_key_names_never_become_display_names(tmp_path,name):
 store=Store(tmp_path/'db')
 key={'id':'private-identity','name':'Gemini primary','label':name,'endpoint':'https://relay.example','family':'gemini'}
 store.record({'host':'h4','sampled_at':20000,'provider_keys':[key],'workers':[]})
 k=store.view(20001)['provider_keys'][0]
 assert k['display_name'] is None and k['name_status']=='missing'


def test_conflicting_canonical_names_do_not_pick_arbitrary_alias(tmp_path):
 store=Store(tmp_path/'db')
 for host,name in [('h4','ikun_gemini_rstation'),('h6','ikun_gemini_personal')]:
  key={'id':'same','name':'Gemini primary','label':name,'endpoint':'https://relay.example','family':'gemini'}
  store.record({'host':host,'sampled_at':20000,'provider_keys':[key],'workers':[]})
 k=store.view(20001)['provider_keys'][0]
 assert k['display_name'] is None and k['name_status']=='conflict'
 key['label']='ikun_gemini_rstation'
 store.record({'host':'h6','sampled_at':20002,'provider_keys':[key],'workers':[]})
 k=store.view(20003)['provider_keys'][0]
 assert k['display_name']=='ikun_gemini_rstation' and k['name_status']=='named'


def test_numeric_provider_prefix_is_a_valid_canonical_name():
 from fleet_collect import canonical_key_name
 assert canonical_key_name('4router_gemini_council')=='4router_gemini_council'


@pytest.mark.parametrize('family',['codex','claude','gemini'])
@pytest.mark.parametrize('dedicated',['missing','','   ','DEDICATED'])
def test_deployed_routes_ignore_conflicting_generic_fields(family,dedicated):
 from fleet_collect import configured_pool
 prefix=family.upper()+'_'
 env={prefix+'API_KEY':'GENERIC',prefix+'BASE_URL':'https://generic.example',prefix+'BACKUP_API_KEY':'GENERIC_BACKUP'}
 if dedicated!='missing':env['RSTATION_WORKER_'+prefix+'API_KEY']=dedicated
 env['RSTATION_WORKER_'+prefix+'BASE_URL']='https://dedicated.example'
 pool=configured_pool(env,family)
 if dedicated!='DEDICATED':assert pool==[]
 else:assert pool==[{'api_key':'DEDICATED','base_url':'https://dedicated.example','http_proxy':'','https_proxy':''}]


def test_route_backups_and_proxy_fingerprint_match_launcher():
 from fleet_collect import configured_pool,pool_fingerprint
 env={'RSTATION_WORKER_CODEX_API_KEY':' MAIN ','RSTATION_WORKER_CODEX_BASE_URL':'https://main.example',
      'RSTATION_WORKER_CODEX_BACKUP_API_KEY':' FIRST ; SECOND ',
      'RSTATION_WORKER_CODEX_BACKUP_BASE_URL':';https://second.example',
      'RSTATION_WORKER_CODEX_BACKUP_HTTP_PROXY':'http://proxy.example;',
      'CODEX_BACKUP_HTTPS_PROXY':'https://wrong.example'}
 pool=configured_pool(env,'codex')
 assert pool[1]=={'api_key':'FIRST','base_url':'https://main.example','http_proxy':'http://proxy.example','https_proxy':''}
 assert pool[2]=={'api_key':'SECOND','base_url':'https://second.example','http_proxy':'','https_proxy':''}
 import hashlib
 assert pool_fingerprint(pool)==hashlib.sha256(json.dumps(pool,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 env['RSTATION_WORKER_CODEX_BACKUP_BASE_URL']='https://one.example'
 assert configured_pool(env,'codex')==[]  # Launcher rejects count mismatch; no broadcast.


@pytest.mark.parametrize('revision,expected_key',[(0,'OLD'),(1,'NEW'),(2,None)])
def test_active_registry_revision_wins_over_controller_and_profile(tmp_path,monkeypatch,revision,expected_key):
 import fleet_collect as f
 monkeypatch.setattr(f,'process_provider_environment',lambda *args:{})
 path=tmp_path/'.provider-control';path.mkdir()
 def route(key):return {'id':'route-1','api_key':key,'base_url':'https://relay.example','http_proxy':'','https_proxy':''}
 (path/'providers.json').write_text(json.dumps({'workers':{'codex_1':{'revision':1,'before_revision':0,'before':[route('OLD')],'after':[route('NEW')]}}}))
 env={'RSTATION_WORKER_CODEX_API_KEY':'ENV','RSTATION_WORKER_CODEX_BASE_URL':'https://env.example','CODEX_API_KEY':'GENERIC','CODEX_BASE_URL':'https://generic.example'}
 keys,selected=f.worker_key_inventory(tmp_path,{'name':'codex_1','kind':'codex'},{'route_index':0,'providers':{'revision':revision}},env,env)
 if expected_key is None:assert keys==[] and selected is None
 else:assert selected['id']==f.key_metadata(expected_key,'https://relay.example','codex',0,'test')['id']


def test_known_empty_controller_environment_does_not_use_current_profile(tmp_path,monkeypatch):
 import fleet_collect as f
 monkeypatch.setattr(f,'process_provider_environment',lambda *args:{})
 env={'RSTATION_WORKER_CODEX_API_KEY':'','CODEX_API_KEY':'GENERIC','CODEX_BASE_URL':'https://generic.example'}
 profile={'RSTATION_WORKER_CODEX_API_KEY':'OTHER','RSTATION_WORKER_CODEX_BASE_URL':'https://other.example'}
 keys,selected=f.worker_key_inventory(tmp_path,{'name':'codex_1','kind':'codex'},{'route_index':0},env,profile)
 assert keys==[] and selected is None


def test_corrected_inventory_removes_old_generic_identity_without_erasing_history(tmp_path):
 store=Store(tmp_path/'db')
 def key(ident):return {'id':ident,'name':'Codex primary','endpoint':'https://relay.example','family':'codex'}
 store.record({'host':'h4','sampled_at':20000,'provider_keys':[key('generic')],'workers':[]})
 store.record({'host':'h6','sampled_at':20000,'provider_keys':[key('other-host')],'workers':[]})
 store.record({'host':'h4','sampled_at':20001,'provider_keys':[key('dedicated')],'workers':[]})
 assert {k['id'] for k in store.view(20001)['provider_keys']}=={'dedicated','other-host'}
 with store.connect() as con:assert con.execute("SELECT COUNT(*) FROM key_samples WHERE key_id='generic'").fetchone()[0]==1


@pytest.mark.parametrize('raw,expected',[
 ('IKUN_cc逆向_rStation','ikun_cc逆向_rstation'),
 ('Ikun_Claude Code-稳定_rStation_2','ikun_claude_code_稳定_rstation_2'),
 ('rightcode_gemini_expensive_personal','rightcode_gemini_expensive_personal'),
 ('4router_gemini_council','4router_gemini_council'),
])
def test_actual_token_group_names_normalize_and_survive_store(tmp_path,raw,expected):
 from fleet_collect import canonical_key_name
 assert canonical_key_name(raw)==expected
 store=Store(tmp_path/'db')
 key={'id':'identity','name':'Claude primary','label':raw,'endpoint':'https://relay.example','family':'claude'}
 store.record({'host':'h4','sampled_at':20000,'provider_keys':[key],'workers':[]})
 assert store.view(20001)['provider_keys'][0]['display_name']==expected


@pytest.mark.parametrize('value',[
 'ikun_cc逆向_rstation\n','ikun_cc逆向_rstation\t','ikun_cc逆向_rstation\u200b',
 'ikun_cc逆向_<script>','ikun_cc逆向_rstation/2','ikun__rstation','_ikun_cc逆向_rstation',
 'ikun_cc逆向_rstation_','ikun_cc逆向_rstation\u202e','ikun_🙂_rstation',
 'ikun_cc逆向_'+'a'*100,'stephen-rstation-worker-claude',
])
def test_whole_token_group_name_rejects_hidden_or_unsafe_content(value):
 from fleet_collect import canonical_key_name
 assert canonical_key_name(value) is None

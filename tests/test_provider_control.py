import copy
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
from common import read,save,pid_token
from recovery import ResearchManager
from station.workers.job_manager import CliJobFailure
from test_worker_control import setup
from worker_control import queue_request,process_requests,handle_failure
import provider_control as pc


def candidates():
 return [dict(id='route-1',api_key='NEW-PRIVATE-KEY',base_url='http://127.0.0.1:3'),dict(id='route-2',api_key='NEW-BACKUP-KEY',base_url='http://127.0.0.1:4')]


def update(run,e,entries=None,**kw):
 return pc.apply(run,candidates() if entries is None else entries,expected_revision=kw.pop('expected_revision',0),environment=e,validator=kw.pop('validator',lambda *a:None),**({'worker':'codex_1'} if not kw else kw))


def test_busy_target_and_siblings_unchanged_until_next_launch(tmp_path):
 c,e,m=setup(tmp_path,count=3);jobs=[]
 try:
  for name in ('codex_1','codex_2','codex_3'):
   p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True);jobs.append(p)
   m.active_sessions[name]=SimpleNamespace(process=p);m.states[name].update(pid=p.pid,pid_token=pid_token(p.pid));m.persist(name)
  old=copy.deepcopy(m.states);oldp=copy.deepcopy(m.provider_pool('codex_1'))
  update(tmp_path,e);pc.refresh(m,'codex_1')
  assert m.states==old and m.provider_pool('codex_1')==oldp
  jobs[0].terminate();jobs[0].wait();m.active_sessions.pop('codex_1');m.states['codex_1']['pid']=None
  pc.refresh(m,'codex_1')
  assert m.provider_pool('codex_1')[0]['api_key']=='NEW-PRIVATE-KEY'
  assert m.provider_pool('codex_2')==oldp
  assert all(m.states[n]==old[n] for n in ('codex_2','codex_3'))
  assert all(p.poll() is None for p in jobs[1:])
  assert m.states['codex_1']['token']==old['codex_1']['token']
 finally:
  m.active_sessions.clear()
  for p in jobs:
   if p.poll() is None:p.terminate();p.wait()
  m.close()


def test_family_commit_is_atomic_but_adoption_is_individual(tmp_path):
 c,e,m=setup(tmp_path,count=3);update(tmp_path,e,family='codex')
 assert pc.registry(tmp_path)['revision']==1
 pc.refresh(m,'codex_2')
 assert m.states['codex_2']['providers']['revision']==1
 assert m.states['codex_1']['providers']['revision']==0
 assert m.states['codex_3']['providers']['revision']==0
 m.close()


def test_preflight_failure_leaves_old_pool_and_no_registry(tmp_path):
 c,e,m=setup(tmp_path)
 before=copy.deepcopy(m.states)
 def bad(*args):raise ValueError('NEW-PRIVATE-KEY')
 with pytest.raises(ValueError) as exc:update(tmp_path,e,validator=bad)
 assert 'NEW-PRIVATE-KEY' not in str(exc.value)
 assert not (pc.folder(tmp_path)/'providers.json').exists()
 assert m.states==before
 m.close()


def test_state_changes_during_preflight_reject_commit(tmp_path):
 c,e,m=setup(tmp_path)
 def changes(*args):m.states['codex_1']['token']='different';m.persist('codex_1')
 with pytest.raises(ValueError,match='changed during'):update(tmp_path,e,validator=changes)
 assert not (pc.folder(tmp_path)/'providers.json').exists()
 m.close()


def test_reorder_preserves_pin_and_rejects_removal(tmp_path):
 c,e,m=setup(tmp_path)
 queue_request(tmp_path,'codex_1',expected_revision=0,provider_route=2,change_route=True,environment=e);process_requests(m)
 update(tmp_path,e,entries=list(reversed(candidates())))
 pc.refresh(m,'codex_1')
 assert m.states['codex_1']['worker_control']['provider_route']==1
 assert m.states['codex_1']['worker_control']['revision']==2
 with pytest.raises(ValueError,match='pinned'):update(tmp_path,e,entries=[candidates()[0]],expected_revision=1)
 m.close()


def test_pending_worker_request_and_stale_operator_revision(tmp_path):
 c,e,m=setup(tmp_path)
 queue_request(tmp_path,'codex_1',expected_revision=0,restart=True,environment=e)
 with pytest.raises(ValueError,match='pending'):update(tmp_path,e)
 process_requests(m);update(tmp_path,e)
 with pytest.raises(ValueError,match='pending'):queue_request(tmp_path,'codex_1',expected_revision=1,restart=True,environment=e)
 with pytest.raises(ValueError,match='changed'):update(tmp_path,e)
 m.close()


def test_crash_before_and_after_commit_retains_pools(tmp_path):
 c,e,m=setup(tmp_path);sid=m.states['codex_1']['token'];update(tmp_path,e);m.close()
 m=ResearchManager(tmp_path,c,e)
 assert m.provider_pool('codex_1')[0]['api_key']=='test-codex'
 pc.refresh(m,'codex_1');m.close()
 stale=dict(e);stale['RSTATION_WORKER_CODEX_API_KEY']='STALE'
 m=ResearchManager(tmp_path,c,stale)
 assert m.provider_pool('codex_1')[0]['api_key']=='NEW-PRIVATE-KEY'
 assert m.states['codex_1']['token']==sid
 m.close()


def test_real_native_launch_uses_new_credentials_same_sid_and_workspace(tmp_path):
 c,e,m=setup(tmp_path);sid=m.states['codex_1']['token'];work=tmp_path/'codex_1'
 Path(e['CODEX_BIN_PATH']).write_text('#!'+sys.executable+'\n'+'''import os,sys,pathlib,json
pathlib.Path('actual.json').write_text(json.dumps({'argv':sys.argv,'key':os.environ['CODEX_API_KEY'],'base':os.environ['CODEX_BASE_URL']}))
print(json.dumps({'type':'thread.started','thread_id':next(v for v in sys.argv if v.startswith('saved-native-session-'))}))
print(json.dumps({'type':'turn.completed'}))
''')
 update(tmp_path,e);assert m.launch_cli_job('codex_1')
 from test_workers import drain
 drain(m)
 actual=read(work/'actual.json')
 assert actual['key']=='NEW-PRIVATE-KEY' and actual['base']=='http://127.0.0.1:3' and sid in actual['argv']
 assert m.states['codex_1']['token']==sid
 m.close()


def test_actual_probe_requires_every_candidate_and_effective_model(tmp_path):
 c,e,m=setup(tmp_path)
 binary=Path(e['CODEX_BIN_PATH']);binary.write_text('#!'+sys.executable+'\n'+'''import os,json,sys
print(json.dumps({'type':'thread.started','thread_id':'disposable'}))
if os.environ['CODEX_API_KEY']=='BAD':
 print(json.dumps({'type':'error','message':'HTTP 401'}));sys.exit(1)
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Hello'}}))
print(json.dumps({'type':'turn.completed'}))
''')
 entries=candidates();entries[1]['api_key']='BAD'
 with pytest.raises(ValueError):update(tmp_path,e,entries=entries,validator=pc.probe)
 assert not (pc.folder(tmp_path)/'providers.json').exists()
 update(tmp_path,e,validator=pc.probe);assert pc.registry(tmp_path)['revision']==1
 m.close()


def test_secret_redaction_and_private_status(tmp_path):
 c,e,m=setup(tmp_path);update(tmp_path,e);pc.refresh(m,'codex_1')
 m.log('codex_1','test-codex NEW-PRIVATE-KEY NEW-BACKUP-KEY')
 text=(tmp_path/'codex_1/.runtime/console.log').read_text()+json.dumps(pc.status(tmp_path))+json.dumps(m.states)
 for secret in ('test-codex','NEW-PRIVATE-KEY','NEW-BACKUP-KEY'):assert secret not in text
 assert (pc.folder(tmp_path)/'providers.json').stat().st_mode & 0o077==0
 m.close()


@pytest.mark.parametrize('category',['provider_unavailable','session_unavailable','model_refusal','unknown'])
def test_failures_after_update_preserve_sid(tmp_path,category):
 c,e,m=setup(tmp_path);sid=m.states['codex_1']['token'];update(tmp_path,e);pc.refresh(m,'codex_1')
 assert handle_failure(m,'codex_1',CliJobFailure(category,category=='provider_unavailable','failure'))
 assert m.states['codex_1']['token']==sid
 assert m.states['codex_1']['status']==('retrying' if category=='provider_unavailable' else 'blocked')
 m.close()


def test_private_input_permissions_symlink_and_no_secret_error(tmp_path):
 p=tmp_path/'candidate.json';p.write_text(json.dumps({'providers':candidates()}));p.chmod(0o644)
 with pytest.raises(ValueError):pc.read_private(p)
 p.chmod(0o600);assert pc.read_private(p)['providers']==candidates()
 link=tmp_path/'link';link.symlink_to(p)
 with pytest.raises(OSError):pc.read_private(link)


def test_crash_during_state_commit_does_not_switch_memory(tmp_path,monkeypatch):
 c,e,m=setup(tmp_path);update(tmp_path,e);old=copy.deepcopy(m.states['codex_1']);persist=m.persist
 def fails(name):raise OSError('disk full')
 monkeypatch.setattr(m,'persist',fails)
 with pytest.raises(OSError):pc.refresh(m,'codex_1')
 assert m.states['codex_1']==old and m.provider_pool('codex_1')[0]['api_key']=='test-codex'
 monkeypatch.setattr(m,'persist',persist);pc.refresh(m,'codex_1');m.close()


def test_simultaneous_updates_one_wins(tmp_path):
 from concurrent.futures import ThreadPoolExecutor
 from threading import Barrier
 c,e,m=setup(tmp_path);barrier=Barrier(2)
 def validator(*args):barrier.wait(timeout=5)
 def submit():
  try:return update(tmp_path,e,validator=validator)
  except ValueError:return None
 with ThreadPoolExecutor(2) as executor:results=list(executor.map(lambda _:submit(),range(2)))
 assert sum(x is not None for x in results)==1
 assert pc.registry(tmp_path)['revision']==1
 m.close()


def test_candidate_snapshot_cannot_change_during_validation(tmp_path):
 c,e,m=setup(tmp_path);entries=candidates()
 def corrupt(config,worker,candidate,environment):candidate[0]['api_key']='NOT-VALIDATED';entries[0]['api_key']='ALSO-NOT-VALIDATED'
 update(tmp_path,e,entries=entries,validator=corrupt)
 assert pc.registry(tmp_path)['workers']['codex_1']['after'][0]['api_key']=='NEW-PRIVATE-KEY'
 m.close()


def test_validation_makes_exact_http_request_without_saved_research(tmp_path):
 from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
 from threading import Thread
 requests=[]
 class Handler(BaseHTTPRequestHandler):
  def do_POST(self):
   body=self.rfile.read(int(self.headers['Content-Length']));requests.append((self.headers['Authorization'],body))
   self.send_response(401 if self.headers['Authorization']=='Bearer BAD' else 200);self.end_headers();self.wfile.write(b'Hello')
  def log_message(self,*args):pass
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=Thread(target=server.serve_forever,daemon=True);thread.start()
 c,e,m=setup(tmp_path);Path(e['CODEX_BIN_PATH']).write_text('#!'+sys.executable+'\n'+'''import os,sys,json,urllib.request
r=urllib.request.Request(os.environ['CODEX_BASE_URL'],data=sys.stdin.buffer.read(),headers={'Authorization':'Bearer '+os.environ['CODEX_API_KEY']})
try:
 text=urllib.request.urlopen(r).read().decode()
except Exception:
 print(json.dumps({'type':'error','message':'authentication rejected'}));sys.exit(1)
print(json.dumps({'type':'thread.started','thread_id':'probe-only'}))
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}))
print(json.dumps({'type':'turn.completed'}))
''')
 entries=[dict(id='route-1',api_key='GOOD',base_url='http://127.0.0.1:'+str(server.server_port)),dict(id='route-2',api_key='BAD',base_url='http://127.0.0.1:'+str(server.server_port))]
 try:
  with pytest.raises(ValueError):update(tmp_path,e,entries=entries,validator=pc.probe)
  assert [x[0] for x in requests]==['Bearer GOOD','Bearer BAD']
  assert all(b'Hello' in body and b'saved-native-session' not in body for _,body in requests)
  assert not (pc.folder(tmp_path)/'providers.json').exists()
 finally:m.close();server.shutdown();server.server_close();thread.join()


def test_proxy_new_host_does_not_mutate_previous_router(tmp_path):
 from station.workers.job_manager import CliJobLaunchDecision
 c,e,m=setup(tmp_path);c['research_proxy']='http://127.0.0.1:8';save(tmp_path/'run.json',c)
 d=CliJobLaunchDecision('resume',1,0,m.states['codex_1']['token'],0)
 m._build_cli_job_launch_spec('codex_1','launch_test',d,m.states['codex_1']);old=m.routers['codex_1'];oldhosts=set(old.model_hosts)
 entries=candidates();entries[0]['base_url']='https://new-model.example/v1'
 update(tmp_path,e,entries=entries);pc.refresh(m,'codex_1')
 spec=m._build_cli_job_launch_spec('codex_1','launch_test',d,m.states['codex_1'])
 assert old.model_hosts==oldhosts and m.routers['codex_1'] is not old
 assert 'new-model.example' in m.routers['codex_1'].model_hosts
 assert 'new-model.example' in spec.env['NO_PROXY']
 m.close()


@pytest.mark.parametrize('category',['provider_unavailable','session_unavailable','model_refusal','unknown'])
def test_failure_while_update_pending_keeps_original_session(tmp_path,category):
 c,e,m=setup(tmp_path);sid=m.states['codex_1']['token'];update(tmp_path,e)
 assert handle_failure(m,'codex_1',CliJobFailure(category,category=='provider_unavailable','failure'))
 assert m.states['codex_1']['token']==sid
 if category!='provider_unavailable':assert m.states['codex_1']['worker_control_hold']['session_id']==sid
 m.close()


def test_existing_host_adoption_pin_survives_reordering(tmp_path):
 adoption=pytest.importorskip('session_adoption')
 c,e,m=setup(tmp_path)
 c['workers'][0]['kind']='gemini';m.route_lists['gemini']=m.route_lists['codex'];save(tmp_path/'run.json',c)
 s=m.states['codex_1'];s['session_adoption']={'session_id':s['token'],'status':'complete','provider_route':adoption.route_selection(c,m.provider_pool('codex_1'),2)};m.persist('codex_1')
 e.update(RSTATION_WORKER_GEMINI_API_KEY='test-codex',RSTATION_WORKER_GEMINI_BASE_URL='http://127.0.0.1:1',RSTATION_WORKER_GEMINI_BACKUP_API_KEY='backup-secret',RSTATION_WORKER_GEMINI_BACKUP_BASE_URL='http://127.0.0.1:2')
 update(tmp_path,e,entries=list(reversed(candidates())));pc.refresh(m,'codex_1')
 s=m.states['codex_1'];assert s['session_adoption']['provider_route']['number']==1
 assert adoption.check_adoption_route(tmp_path,'codex_1',s,c,m.provider_pool('codex_1'))
 m.close()


def test_gpu_reservations_context_and_notice_queue_unchanged(tmp_path):
 from station.eval_research.gpu_coordinator import GPUCoordinator
 c,e,m=setup(tmp_path);c['gemini_context_tokens']=700000;save(tmp_path/'run.json',c)
 coordinator=GPUCoordinator(c['gpu_coord_file'],[0,1],'active-experiment',300)
 assert coordinator.allocate('training',2)==[0,1]
 from messages import Messages
 Messages(tmp_path/'codex_1').add('Owner continuity notice',source='operator')
 before={p:p.read_bytes() for p in tmp_path.rglob('*.json') if p.name!='state.json'}
 update(tmp_path,e);pc.refresh(m,'codex_1')
 assert all(p.read_bytes()==value for p,value in before.items())
 assert m.config['gemini_context_tokens']==700000
 coordinator.deallocate('training');m.close()


def test_changed_or_missing_sid_after_applied_update_holds_on_restart(tmp_path):
 c,e,m=setup(tmp_path);sid=m.states['codex_1']['token'];update(tmp_path,e);pc.refresh(m,'codex_1');m.close()
 path=tmp_path/'codex_1/.runtime/state.json';state=read(path);state.update(token=None,session_unavailable=True);save(path,state)
 m=ResearchManager(tmp_path,c,e)
 assert not m.launch_cli_job('codex_1')
 assert m.states['codex_1']['token']==sid
 assert m.states['codex_1']['blocked_reason']=='provider_session_conflict'
 m.close()


def test_restart_keeps_unswitched_same_family_siblings_on_original_pool(tmp_path):
 c,e,m=setup(tmp_path,count=3);old=copy.deepcopy(m.provider_pool('codex_2'))
 update(tmp_path,e);pc.refresh(m,'codex_1');m.close()
 m=ResearchManager(tmp_path,c,e)
 assert m.provider_pool('codex_1')[0]['api_key']=='NEW-PRIVATE-KEY'
 assert m.provider_pool('codex_2')==old and m.provider_pool('codex_3')==old
 m.close()

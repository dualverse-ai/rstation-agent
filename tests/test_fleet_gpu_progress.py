"""Advancing GPU work is separate from native/provider response evidence."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

import fleet_collect as collect
from fleet_dashboard import Store,health,family_summary,key_observation
from test_fleet_dashboard import row


def test_training_step_only_accepts_numeric_step_records():
 assert collect.training_step(b'step=175 loss=3.2 dt=1100ms\nstep=250 loss=3.1\n')==250
 assert collect.training_step(b'Step 32/953 loss 1\n')==32
 for data in (b'heartbeat 123',b'copied text step=123',b'steps/sec=13',b'step=-1',b'step=12oops',b'loss=0.2'):
  assert collect.training_step(data) is None


@pytest.mark.parametrize('kind,events',[
 ('gemini',[{'step_update':{'step_type':'tool','step_index':42,'state':'ACTIVE'}},{'step_update':{'step_type':'tool','step_index':42,'state':'DONE'}}]),
 ('codex',[{'type':'item.started','item':{'id':'tool','type':'command_execution'}},{'type':'item.completed','item':{'id':'tool'}}]),
 ('claude',[{'type':'assistant','message':{'content':[{'type':'tool_use','id':'tool','name':'Bash'}]}},{'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'tool'}]}}]),
])
def test_pending_native_tool_is_required(tmp_path,kind,events):
 path=tmp_path/'launch_000001/transcript.jsonl';path.parent.mkdir()
 path.write_text(json.dumps(events[0])+'\n')
 assert collect.pending_command(tmp_path,{'launches':1},kind)
 with path.open('a') as f:f.write(json.dumps(events[1])+'\n')
 assert collect.pending_command(tmp_path,{'launches':1},kind) is None


@pytest.fixture
def live_job(tmp_path):
 work=tmp_path/'workspace';runtime=work/'.runtime';runtime.mkdir(parents=True)
 transcript=runtime/'launch_000001/transcript.jsonl';transcript.parent.mkdir()
 transcript.write_text(json.dumps({'step_update':{'step_type':'tool','step_index':42,'state':'ACTIVE'}})+'\n')
 log=work/'experiment.log'
 with log.open('wb') as output:
  child=subprocess.Popen([sys.executable,'-u','-c',"import sys;print('PRIVATE_LOG_TEXT');print('step=10 loss=2.0');\nfor line in sys.stdin: print('step='+line.strip()+' loss=1.0')"],stdin=subprocess.PIPE,stdout=output)
 def wait_step(step):
  deadline=time.monotonic()+5
  while time.monotonic()<deadline:
   if collect.training_step(log.read_bytes())==step:return
   time.sleep(.01)
  raise AssertionError('Fixture did not write step')
 wait_step(10)
 root=os.getpid();state={'pid':root,'pid_token':collect.process_info(root)['token'],'launches':1}
 job={'helper_pid':root,'helper_token':state['pid_token'],'pid':child.pid,'token':collect.process_info(child.pid)['token'],'gpus':[0]}
 receipt=runtime/'gpu-jobs/job.json';receipt.parent.mkdir();receipt.write_text(json.dumps(job))
 try:yield work,state,child,log,receipt,job,transcript,wait_step
 finally:
  child.stdin.close()
  try:child.wait(timeout=3)
  except subprocess.TimeoutExpired:child.kill();child.wait()


def test_live_descriptor_progress_keeps_response_age_and_provider_unknown(live_job,tmp_path):
 work,state,child,log,receipt,job,transcript,wait_step=live_job
 first=collect.gpu_progress_as_worker(work,state,'gemini')
 assert len(first)==1 and first[0]['step']==10
 assert all(v not in json.dumps(first) for v in ('PRIVATE_LOG_TEXT','experiment.log',str(work)))
 store=Store(tmp_path/'history')
 def record(at,samples):
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=100,last_tool=200,foreground_job=bool(samples),job_progress_samples=samples,provider_key_id='key')]})
 record(20000,first)
 assert store.view(20000)['workers'][0]['health']=='transient_error'
 child.stdin.write(b'20\n');child.stdin.flush();wait_step(20)
 second=collect.gpu_progress_as_worker(work,state,'gemini')
 assert second[0]['id']==first[0]['id'] and second[0]['step']==20
 record(20120,second);snapshot=store.view(20120);worker=snapshot['workers'][0]
 assert worker['health']=='working' and worker['last_job_progress']==20120
 assert worker['last_response']==100 and worker['last_tool']==200
 assert 'job_progress_samples' not in worker and worker['history'][-1]=='working'
 assert family_summary([worker],20120)[0]['status']=='no_recent_response'
 assert key_observation('key',[worker],20120)['status']=='unknown'
 # Continued collection of unchanged counters must not refresh progress.
 for at in range(20240,20841,120):record(at,second)
 assert store.view(20840)['workers'][0]['health']=='transient_error'
 assert store.view(21141)['workers'][0]['health']=='transient_error'


@pytest.mark.parametrize('change',['dead_token','foreign_parent','no_gpu','completed_tool','outside_log'])
def test_unverified_or_finished_job_cannot_claim_progress(live_job,tmp_path,change):
 work,state,child,log,receipt,job,transcript,wait_step=live_job
 if change=='dead_token':job['token']='wrong'
 if change=='foreign_parent':job['helper_pid']=child.pid;job['helper_token']=job['token'];state=dict(state,pid=child.pid,pid_token=job['token']);job['pid']=os.getpid();job['token']=collect.process_info(os.getpid())['token']
 if change=='no_gpu':job['gpus']=[]
 if change=='completed_tool':
  with transcript.open('a') as f:f.write(json.dumps({'step_update':{'step_type':'tool','step_index':42,'state':'DONE'}})+'\n')
 if change=='outside_log':
  moved=tmp_path/'outside.log';log.rename(moved);log.symlink_to(moved)
 receipt.write_text(json.dumps(job))
 assert collect.gpu_progress_as_worker(work,state,'gemini')==[]


@pytest.mark.parametrize('change',['new_job','reset_counter','host_gap','no_job','blocked','retrying','paused'])
def test_progress_does_not_hide_stalls_or_control_states(tmp_path,change):
 store=Store(tmp_path/'history')
 def record(at,step=10,ident='job',**kw):
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=100,foreground_job=True,job_progress_samples=[{'id':ident,'step':step}],**kw)]})
 record(20000);record(20120,20)
 assert store.view(20120)['workers'][0]['health']=='working'
 at=20240
 kwargs={}
 if change=='new_job':kwargs['ident']='other'
 elif change=='reset_counter':kwargs['step']=1
 elif change=='host_gap':at=21000;kwargs['step']=30
 elif change=='no_job':
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=100,foreground_job=False,job_progress_samples=[])]})
 else:kwargs['state']=change
 if change!='no_job':record(at,**kwargs)
 assert store.view(at)['workers'][0]['health']!='working'


def test_disappeared_advancing_job_cannot_credit_stationary_sibling(tmp_path):
 store=Store(tmp_path/'history')
 for at,values in [(20000,[('a',10),('b',10)]),(20120,[('a',20),('b',10)]),(20240,[('b',10)])]:
  store.record({'host':'h4','sampled_at':at,'workers':[row(sampled_at=at,last_response=100,foreground_job=True,job_progress_samples=[{'id':k,'step':v} for k,v in values])]})
 assert store.view(20240)['workers'][0]['health']=='transient_error'

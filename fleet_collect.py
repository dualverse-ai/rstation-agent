"""Read-only host collector. Output contains allowlisted operational metadata only."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time

STATES={'running','retrying','blocked','paused','stopped','continuing','starting','sleeping','not_started','unknown'}
ERRORS={'provider_unavailable','model_refusal','session_unavailable','infra_transient','local_launch_error','route_selection_conflict','session_adoption_failed','worker_control_failed','provider_session_conflict'}


def read(path):
 try:
  if path.stat().st_size>8*1024*1024:return {}
  value=json.loads(path.read_text());return value if isinstance(value,dict) else {}
 except (OSError,ValueError):return {}


def label(value):return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:/@+-]{1,160}',value) else None

def number(value):return value if type(value) in (int,float) and math.isfinite(value) and value>=0 else None

def stamp(value):
 if number(value) is not None:return value
 try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp() if isinstance(value,str) else None
 except ValueError:return None


def alive(pid,token):
 if not pid or not token:return False
 try:
  row=Path('/proc',str(int(pid)),'stat').read_text().rsplit(')',1)[1].split()
  return row[0] not in ('Z','X') and row[19]==str(token)
 except (OSError,ValueError,IndexError):return False


def error(state):
 category=state.get('last_failure_category')
 for key in ('worker_control_hold','session_adoption','worker_control'):
  category=state.get(key,{}).get('failure_category') or category
 if state.get('local_launch_error'):category='local_launch_error'
 if category not in ERRORS:category='unknown' if state.get('last_error') or state.get('blocked_reason') else None
 # Never emit provider text; extract only an HTTP status number.
 match=re.search(r'\b(?:HTTP(?:/\d(?:\.\d)?)?\s+|Error\s*:?\s*)(429|4\d\d|5\d\d)\b',str(state.get('last_error',''))[:4000],re.I)
 return {'category':category,'http_status':int(match[1]) if match else None}


def collect(roots,host):
 now=time.time();rows=[];failures=[]
 for root in roots:
  root=Path(root);data=root/'agent_data'
  if not data.is_dir():failures.append('missing_root');continue
  for config_path in sorted(data.glob('*/run.json')):
   run=config_path.parent;config=read(config_path);ctl=read(run/'controller.json')
   if not isinstance(config.get('workers'),list):continue
   for worker in config['workers']:
    if not isinstance(worker,dict) or not label(worker.get('name')) or Path(worker['name']).name!=worker['name']:continue
    name=worker['name'];work=run/name;runtime=work/'.runtime';state=read(runtime/'state.json')
    effective={**worker,**state.get('worker_control',{}).get('settings',{})}
    held=[];job_live=False
    for path in (runtime/'gpu-jobs').glob('*.json'):
     job=read(path)
     if alive(job.get('helper_pid'),job.get('helper_token')) or alive(job.get('pid'),job.get('token')):
      job_live=True;held.extend(g for g in job.get('gpus',[]) if type(g)==int and 0<=g<100)
    messages=read(runtime/'operator_messages.json').get('messages',[])
    messages=[m for m in messages if isinstance(m,dict)] if isinstance(messages,list) else []
    pending=sum(m.get('status') in ('queued','sent') for m in messages)
    received=max([stamp(m.get('received_at')) or 0 for m in messages if m.get('priority')=='owner'] or [0]) or None
    paused=(runtime/'PAUSE').exists();stopped=(run/'STOP').exists()
    status=state.get('status','unknown');status=status if status in STATES else 'unknown'
    if paused:status='paused'
    elif stopped:status='stopped'
    ctl_live=alive(ctl.get('pid'),ctl.get('token'));native_live=alive(state.get('pid'),state.get('pid_token'))
    if not ctl_live and not native_live and status not in ('paused','blocked'):status='stopped'
    handle=None
    for name_file in ('registration.json','.rstation.json','.rstation/credentials.json','.rstation/agent.json','rstation_credentials.json'):
     account=read(work/name_file);handle=label(account.get('handle') or account.get('agent_handle') or account.get('agent_name'))
     if handle:break
    row={'id':hashlib.sha256((host+str(root)+str(run.name)+name).encode()).hexdigest()[:20],
         'host':host,'installation':str(root),'run':label(run.name),'worker':name,'problem':label(config.get('problem')),
         'role':'supervisor' if config.get('role')=='supervisor' else 'researcher','model':label(effective.get('model')),
         'family':worker.get('kind') if worker.get('kind') in ('codex','claude','gemini') else 'unknown',
         'route':(state['route_index']+1) if type(state.get('route_index'))==int else None,
         'state':status,'controller_alive':ctl_live,'process_alive':native_live,'foreground_job':False,'owned_job':job_live,
         'last_response':number(state.get('last_response')),'last_tool':number(state.get('last_tool_completed')),
         'last_success':number(state.get('last_success')),'started_at':number(state.get('started')),
         'context_tokens':number(config.get('gemini_context_tokens')) if worker.get('kind')=='gemini' else None,
         'gpu':sorted(set(held)),'disk_bytes':number(state.get('workspace_bytes')),
         'disk_allowance_gb':number(worker.get('storage_gb',config.get('storage_gb'))),
         'pending_messages':pending,'last_owner_received':received,
         'controller_revision':label(ctl.get('code_revision')),'upgrade_pending':(run/'controller-upgrade.json').exists(),
         'provider_revision':number(state.get('providers',{}).get('revision')),
         'handle':handle,'sampled_at':now,'error':error(state),
         'last_platform_receipt':None,'last_interaction':None}
    # Operator-maintained reason metadata is explicitly required; PAUSE alone is not evidence of owner intent.
    reason=read(runtime/'operator-pause.json')
    row['intentional_stop']=bool(status in ('paused','stopped') and reason.get('owner_authorized') is True and reason.get('message_id'))
    rows.append(row)
 return {'host':host,'sampled_at':now,'workers':rows,'errors':failures}


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('root',nargs='+');a=p.parse_args()
 print(json.dumps(collect(a.root,a.host),separators=(',',':')))

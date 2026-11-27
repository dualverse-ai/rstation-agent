"""Read-only host collector. Output contains allowlisted operational metadata only."""
import argparse
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import re
import time
import unicodedata

STATES={'running','retrying','blocked','paused','stopped','continuing','starting','sleeping','not_started','unknown'}
ERRORS={'provider_unavailable','model_refusal','session_unavailable','infra_transient','local_launch_error','route_selection_conflict','session_adoption_failed','worker_control_failed','provider_session_conflict'}


def read(path):
 try:
  if path.stat().st_size>8*1024*1024:return {}
  value=json.loads(path.read_text());return value if isinstance(value,dict) else {}
 except (OSError,ValueError):return {}


def label(value):return value if isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_.:/@+-]{1,160}',value) else None

def canonical_key_name(value):
 # Inputs must already distinguish provider, token group and purpose with underscores.
 if not isinstance(value,str) or len(value)>100 or value.count('_')<2:return None
 if value.startswith(('RSTATION_','PERSONAL_','STATION_','CODEX_','CLAUDE_','GEMINI_','GOOGLE_')):return None
 name=unicodedata.normalize('NFC',re.sub(r'[- ]+','_',value.casefold()))
 parts=name.split('_')
 if len(name)>100 or not re.fullmatch(r'[a-z0-9]+',parts[0]):return None
 if any(not part or any(not (unicodedata.category(c).startswith('L') or unicodedata.category(c)=='Nd') for c in part) for part in parts[1:]):return None
 return name


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


# This code is embedded in the single read-only host collector.

def process_provider_environment(pid,token):
 """Inspect only an identified rStation process; secret values never leave the host."""
 if not alive(pid,token):return {}
 allowed={prefix+family+'_'+backup+field for prefix in ('','RSTATION_WORKER_') for family in ('CODEX','CLAUDE','GEMINI') for backup in ('','BACKUP_') for field in ('API_KEY','BASE_URL','HTTP_PROXY','HTTPS_PROXY')}
 allowed.update(('ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL','GOOGLE_GEMINI_BASE_URL'))
 try:
  raw=Path('/proc',str(int(pid)),'environ').read_bytes()
  if len(raw)>1024*1024:return {}
  values={}
  for item in raw.split(b'\0'):
   name,sep,value=item.partition(b'=')
   name=name.decode('ascii',errors='ignore')
   if sep and name in allowed:values[name]=value.decode('utf-8',errors='replace')
  return values if alive(pid,token) else {}
 except (OSError,ValueError):return {}


def safe_endpoint(value):
 from urllib.parse import urlsplit
 try:
  p=urlsplit(value)
  if p.scheme not in ('https','http') or not p.hostname:return None
  host=p.hostname
  if not re.fullmatch(r'[A-Za-z0-9.:-]{1,253}',host):return None
  if ':' in host:host='['+host+']'
  # Omit userinfo, query, fragment, and arbitrary path segments that may be tokens.
  path=p.path if p.path in ('','/','/v1','/v1/','/v1beta','/v1beta/','/api','/api/','/anthropic','/anthropic/') else ''
  return p.scheme+'://'+host+(':'+str(p.port) if p.port else '')+path.rstrip('/')
 except (TypeError,ValueError):return None


def key_metadata(key,endpoint,family,index,source):
 endpoint=safe_endpoint(endpoint)
 if not isinstance(key,str) or not key or not endpoint or family not in ('codex','claude','gemini'):return None
 ident=hashlib.sha256((endpoint+'\0'+key).encode()).hexdigest()
 return {'id':ident,'name':family.capitalize()+' '+('primary' if index==0 else 'backup '+str(index)),
         'endpoint':endpoint,'family':family,'route':index+1,'source':source}


def configured_pool(env,family):
 """Mirror deployed H4/H6/H7 routes(): dedicated fields only, no generic fallback."""
 prefix='RSTATION_WORKER_'+family.upper()+'_'
 def value(field):return env.get(prefix+field,'') or ''
 fields=('api_key','base_url','http_proxy','https_proxy')
 primary={f:value(f.upper()).strip() for f in fields}
 if not primary['api_key'] or not primary['base_url']:return []
 backups={f:[v.strip() for v in value('BACKUP_'+f.upper()).split(';')] if value('BACKUP_'+f.upper()) else [] for f in fields}
 count=len(backups['api_key'])
 if any(not k for k in backups['api_key']):return []
 if any(v and len(v)!=count for v in backups.values()):return []
 pool=[primary]
 for i in range(count):
  route={f:values[i] if values else '' for f,values in backups.items()}
  route['base_url']=route['base_url'] or primary['base_url']
  pool.append(route)
 return pool


def profile_environment(root):
 """Read literal rStation-purpose assignments; never execute or source shell code."""
 import shlex
 result={}
 for path in (Path(root)/'.env',Path.home()/'bin/rstation-api.env'):
  try:
   if path.stat().st_size>1024*1024:continue
   lines=path.read_text().splitlines()
  except OSError:continue
  for line in lines:
   match=re.match(r'\s*(?:export\s+)?((?:RSTATION_WORKER_)?(?:CODEX|CLAUDE|GEMINI)_(?:BACKUP_)?(?:API_KEY|BASE_URL|HTTP_PROXY|HTTPS_PROXY))=(.*)$',line)
   if not match:continue
   try:parts=shlex.split(match[2],comments=True)
   except ValueError:continue
   if len(parts)!=1 or '$' in parts[0] or '`' in parts[0]:continue
   result[match[1]]=parts[0]
 return result


def pool_fingerprint(pool,env=None,family=None):
 return hashlib.sha256(json.dumps(pool,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def worker_key_inventory(run,worker,state,controller_env,profile_env=None):
 family=worker.get('kind');pool=configured_pool(controller_env,family) if family in ('codex','claude','gemini') else []
 registry=read(run/'.provider-control/providers.json');record=registry.get('workers',{}).get(worker['name'])
 source='controller configuration'
 expected=state.get('providers',{}).get('pool_sha256') or state.get('worker_control',{}).get('pool_sha256') or state.get('session_adoption',{}).get('provider_route',{}).get('pool_sha256')
 verified=bool(pool) and (not expected or pool_fingerprint(pool)==expected)
 if not controller_env and profile_env:
  pool=configured_pool(profile_env,family) if family in ('codex','claude','gemini') else []
  source='configured profile; connection unverified'
  expected=state.get('providers',{}).get('pool_sha256') or state.get('worker_control',{}).get('pool_sha256') or state.get('session_adoption',{}).get('provider_route',{}).get('pool_sha256')
  verified=bool(pool and expected and pool_fingerprint(pool,profile_env,family)==expected)
  if verified:source='matching saved route fingerprint'
 if isinstance(record,dict):
  revision=state.get('providers',{}).get('revision',0)
  if revision==record.get('revision'):pool=record.get('after',[]);source='active provider revision';verified=True
  elif revision==record.get('before_revision'):pool=record.get('before',[]);source='active provider revision';verified=True
  else:pool=[];verified=False
  pool=[{k:v for k,v in r.items() if k!='id'} for r in pool if isinstance(r,dict)]
  if expected and pool_fingerprint(pool)!=expected:verified=False
 result=[]
 for i,r in enumerate(pool):
  if not isinstance(r,dict):continue
  meta=key_metadata(r.get('api_key'),r.get('base_url'),family,i,source)
  if meta:result.append(meta)
 selected=None;index=state.get('route_index')
 if verified and type(index)==int and 0<=index<len(pool):selected=key_metadata(pool[index].get('api_key'),pool[index].get('base_url'),family,index,source)
 native=process_provider_environment(state.get('pid'),state.get('pid_token'))
 names={'codex':('CODEX_API_KEY','CODEX_BASE_URL'),'claude':('ANTHROPIC_API_KEY','ANTHROPIC_BASE_URL'),'gemini':('GEMINI_API_KEY','GOOGLE_GEMINI_BASE_URL')}
 if family in names and native:
  key_name,url_name=names[family];actual=key_metadata(native.get(key_name),native.get(url_name),family,index if type(index)==int else 0,'live worker connection')
  if actual:selected=actual;result.append(actual)
 return result,selected


def collect(roots,host):
 now=time.time();rows=[];failures=[];inventory={}
 for root in roots:
  root=Path(root);data=root/'agent_data';profile_env=profile_environment(root)
  key_labels=read(root/'.provider-labels.json').get('labels',{})
  if not data.is_dir():failures.append('missing_root');continue
  for config_path in sorted(data.glob('*/run.json')):
   run=config_path.parent;config=read(config_path);ctl=read(run/'controller.json')
   if not isinstance(config.get('workers'),list):continue
   controller_env=process_provider_environment(ctl.get('pid'),ctl.get('token'))
   for worker in config['workers']:
    if not isinstance(worker,dict) or not label(worker.get('name')) or Path(worker['name']).name!=worker['name']:continue
    name=worker['name'];work=run/name;runtime=work/'.runtime';state=read(runtime/'state.json')
    effective={**worker,**state.get('worker_control',{}).get('settings',{})}
    keys,selected_key=worker_key_inventory(run,worker,state,controller_env,profile_env)
    for key in keys:
     title=key_labels.get(key['id']) if isinstance(key_labels,dict) else None
     canonical=canonical_key_name(title)
     if canonical:key['label']=canonical
     inventory[(key['id'],key['name'])]=key
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
         'last_platform_receipt':None,'last_interaction':None,'provider_key_id':selected_key['id'] if selected_key else None,
         'provider_key_source':selected_key['source'] if selected_key else None}
    # Operator-maintained reason metadata is explicitly required; PAUSE alone is not evidence of owner intent.
    reason=read(runtime/'operator-pause.json')
    row['intentional_stop']=bool(status in ('paused','stopped') and reason.get('owner_authorized') is True and reason.get('message_id'))
    rows.append(row)
 return {'host':host,'sampled_at':now,'workers':rows,'errors':failures,'provider_keys':list(inventory.values())}


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('root',nargs='+');a=p.parse_args()
 print(json.dumps(collect(a.root,a.host),separators=(',',':')))

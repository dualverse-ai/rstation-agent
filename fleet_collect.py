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


def process_info(pid):
 try:
  fields=Path('/proc',str(int(pid)),'stat').read_text().rsplit(')',1)[1].split()
  if fields[0] in ('Z','X'):return None
  return {'parent':int(fields[1]),'token':fields[19]}
 except (OSError,ValueError,IndexError):return None


def native_provider_retry(runtime,state,kind,now):
 """Current Claude launch only; export status metadata, never native error text."""
 launch=state.get('launches');started=number(state.get('started'))
 if kind!='claude' or type(launch)!=int or launch<0 or not started or not alive(state.get('pid'),state.get('pid_token')):return None
 try:
  with (runtime/f'launch_{launch:06d}'/'transcript.jsonl').open('rb') as f:
   import os
   info=os.fstat(f.fileno())
   if not started<=info.st_mtime<=now or now-info.st_mtime>300:return None
   offset=max(0,info.st_size-2*1024*1024);f.seek(offset)
   if offset:f.readline()
   offset=f.tell();data=f.read(2*1024*1024)
 except OSError:return None
 retry=None
 for line in data.splitlines(keepends=True):
  offset+=len(line)
  if not line.endswith(b'\n'):continue  # A concurrently written record is not evidence.
  try:e=json.loads(line)
  except ValueError:continue
  if not isinstance(e,dict):continue
  kind_=e.get('type');subtype=e.get('subtype')
  if kind_=='system' and subtype=='init':retry=None
  elif kind_=='system' and subtype=='api_retry':
   status=e.get('error_status');when=stamp(e.get('timestamp'))
   when=info.st_mtime if when is None else when
   retry={'id':hashlib.sha256(f'{launch}:{started}:{offset}'.encode()).hexdigest()[:20],
          'at':when,'http_status':status if type(status)==int and 400<=status<=599 else None}
  elif kind_=='assistant' and not e.get('error') and not e.get('is_error'):
   message=e.get('message',{});content=message.get('content') if isinstance(message,dict) else None
   if isinstance(content,list) and any(isinstance(p,dict) and (p.get('type')=='tool_use' or p.get('type')=='text' and p.get('text') or p.get('type')=='thinking' and p.get('thinking')) for p in content):retry=None
  elif kind_=='stream_event':
   event=e.get('event',{});delta=event.get('delta',{}) if isinstance(event,dict) else {}
   if isinstance(delta,dict) and event.get('type')=='content_block_delta' and (delta.get('type')=='text_delta' and delta.get('text') or delta.get('type')=='thinking_delta' and delta.get('thinking')):retry=None
  elif kind_=='result' and subtype=='success' and not e.get('is_error'):retry=None
 if retry and started<=retry['at']<=now and now-retry['at']<=300 and alive(state.get('pid'),state.get('pid_token')):return retry
 return None


def pending_command(runtime,state,kind):
 """Only an unfinished native tool can make a GPU job foreground evidence."""
 launch=state.get('launches')
 if type(launch)!=int or launch<0:return None
 try:
  with (runtime/f'launch_{launch:06d}'/'transcript.jsonl').open('rb') as f:
   size=f.seek(0,2);f.seek(max(0,size-2*1024*1024))
   if size>2*1024*1024:f.readline()  # A partial record is not evidence.
   lines=f.read(2*1024*1024).splitlines()
 except OSError:return None
 pending=set()
 for line in lines:
  try:e=json.loads(line)
  except ValueError:continue
  if not isinstance(e,dict):continue
  if kind=='gemini':
   step=e.get('step_update',{})
   if isinstance(step,dict) and step.get('step_type')=='tool':
    key=step.get('step_index')
    if type(key)!=int:continue
    if step.get('state') in ('DONE','ERROR','FAILED','CANCELLED'):pending.discard(key)
    elif step.get('state') in ('ACTIVE','RUNNING','IN_PROGRESS'):pending.add(key)
   if e.get('event')=='result':pending.clear()
  elif kind=='codex':
   item=e.get('item',{})
   if isinstance(item,dict) and isinstance(item.get('id'),str):
    if e.get('type')=='item.completed':pending.discard(item['id'])
    elif e.get('type')=='item.started' and item.get('type')=='command_execution':pending.add(item['id'])
   if e.get('type') in ('turn.completed','turn.failed'):pending.clear()
  elif kind=='claude':
   message=e.get('message',{});content=message.get('content',[]) if isinstance(message,dict) else []
   if isinstance(content,list):
    for part in content:
     if not isinstance(part,dict):continue
     if part.get('type')=='tool_use' and part.get('name') in ('Bash','bash') and isinstance(part.get('id'),str):pending.add(part['id'])
     elif part.get('type')=='tool_result':pending.discard(part.get('tool_use_id'))
   if e.get('type')=='result':pending.clear()
 return hashlib.sha256(json.dumps(sorted(pending)).encode()).hexdigest() if pending else None


def training_step(data):
 # Export counters only, never arbitrary log text or file names. Heartbeats,
 # percentages, loss values and byte growth are not training-step evidence.
 matches=list(re.finditer(rb'(?mi)^\s*step\s*[=: ]\s*(\d{1,12})(?:\s*/\s*\d{1,12})?(?=\s|$)',data))
 return int(matches[-1][1]) if matches else None


def gpu_progress_as_worker(work,state,kind):
 import os,stat
 runtime=work/'.runtime';tool=pending_command(runtime,state,kind)
 root=state.get('pid');token=state.get('pid_token')
 if not tool or not alive(root,token):return []
 jobs=[]
 for path in list((runtime/'gpu-jobs').glob('*.json'))[:128]:
  job=read(path)
  if (job.get('gpus') and alive(job.get('helper_pid'),job.get('helper_token'))
      and alive(job.get('pid'),job.get('token'))):jobs.append(job)
 if not jobs:return []
 table={int(p.name):info for p in Path('/proc').iterdir() if p.name.isdigit() and (info:=process_info(p.name))}
 def beneath(pid,parent):
  seen=set()
  while pid in table and pid not in seen:
   if pid==parent:return True
   seen.add(pid);pid=table[pid]['parent']
  return False
 result=[];seen=set();work=work.resolve()
 for job in jobs:
  driver=job['pid'];helper=job['helper_pid']
  if (not beneath(helper,root) or not beneath(driver,helper)
      or table.get(driver,{}).get('token')!=str(job['token'])
      or table.get(helper,{}).get('token')!=str(job['helper_token'])):continue
  descendants=[pid for pid in table if beneath(pid,driver)][:128]
  for pid in descendants:
   for fd in list(Path('/proc',str(pid),'fd').glob('*'))[:256]:
    try:
     target=fd.resolve(strict=True);relative=target.relative_to(work)
     if target.suffix not in ('.log','.out','.txt') or any(p.startswith('.') for p in relative.parts):continue
     flags=re.search(r'^flags:\s*([0-7]+)$',fd.with_name(fd.name).parent.parent.joinpath('fdinfo',fd.name).read_text(),re.M)
     if not flags or int(flags[1],8)&os.O_ACCMODE not in (os.O_WRONLY,os.O_RDWR):continue
     expected=target.stat()
     # Opening a raced descriptor must never block on a pipe or device.
     handle=os.open(fd,os.O_RDONLY|os.O_NONBLOCK)
     with os.fdopen(handle,'rb') as f:
      info=os.fstat(f.fileno())
      if not stat.S_ISREG(info.st_mode) or (info.st_dev,info.st_ino)!=(expected.st_dev,expected.st_ino) or (info.st_dev,info.st_ino) in seen:continue
      seen.add((info.st_dev,info.st_ino))
      f.seek(max(0,info.st_size-65536))
      if info.st_size>65536:f.readline()
      data=f.read(65536)
     step=training_step(data)
     if step is None:continue
     if not alive(pid,table[pid]['token']) or not alive(driver,job['token']) or not alive(root,token):continue
     identity=[root,str(token),state['launches'],tool,driver,str(job['token']),info.st_dev,info.st_ino]
     result.append({'id':hashlib.sha256(json.dumps(identity).encode()).hexdigest(),'step':step})
    except (OSError,ValueError):continue
 return result[:64]


def gpu_progress(work,state,kind):
 """Read /proc as the workspace account, without changing worker permissions."""
 import os,select,signal
 try:owner=work.stat()
 except OSError:return []
 if os.geteuid()!=0 or owner.st_uid==0:return gpu_progress_as_worker(work,state,kind)
 # Hosts can deny root access to a worker's descriptor links. This disposable
 # collector child drops privilege; no worker process receives a signal.
 read_fd,write_fd=os.pipe();child=os.fork()
 if child==0:
  os.close(read_fd)
  try:
   os.setgroups([]);os.setgid(owner.st_gid);os.setuid(owner.st_uid)
   data=json.dumps(gpu_progress_as_worker(work,state,kind)).encode()
   os.write(write_fd,data[:16384])
  except Exception:pass
  finally:os.close(write_fd);os._exit(0)
 os.close(write_fd)
 try:
  if not select.select([read_fd],[],[],5)[0]:
   os.kill(child,signal.SIGKILL);return []
  data=b''
  while chunk:=os.read(read_fd,16384):
   data+=chunk
   if len(data)>16384:return []
  result=json.loads(data)
  return result if isinstance(result,list) else []
 except (OSError,ValueError):return []
 finally:
  os.close(read_fd);os.waitpid(child,0)



def gemini_tool_observation(runtime, state):
 """Return only opaque identities for a pending command or a series of identical reads."""
 launch = state.get('launches')
 if type(launch) != int or launch < 0:
  return {}
 path = runtime / f'launch_{launch:06d}' / 'transcript.jsonl'
 try:
  with path.open('rb') as stream:
   end = stream.seek(0, 2)
   offset = max(0, end - 512 * 1024)
   stream.seek(offset)
   if offset:
    stream.readline(512 * 1024)
   lines = stream.read(512 * 1024).splitlines(keepends=True)
 except OSError:
  return {}
 pending = {}
 previous = None
 indices = set()
 conversation = state.get('token')
 if not conversation:
  return {}
 for line in lines:
  if not line.endswith(b'\n'):
   continue
  try:
   event = json.loads(line)
  except (ValueError, UnicodeError):
   continue
  if not isinstance(event, dict):
   continue
  step = event.get('step_update')
  if event.get('event') == 'result' or isinstance(step,dict) and step.get('step_type') == 'user_input':
   pending.clear()
   previous = None
   indices.clear()
   continue
  if not isinstance(step,dict) or step.get('step_type') != 'tool' or step.get('conversation_id') != conversation:
   continue
  index = step.get('step_index')
  if type(index) != int:
   continue
  name = step.get('tool_name')
  status = step.get('state')
  if status in ('ACTIVE','RUNNING','IN_PROGRESS') and name == 'run_command':
   pending[index] = True
  if status not in ('DONE','FAILED','ERROR','CANCELLED'):
   continue
  pending.pop(index, None)
  info = step.get('tool_info')
  args = info.get('parameters') if isinstance(info,dict) else None
  if isinstance(args,str):
   try:
    args = json.loads(args)
   except ValueError:
    args = None
  if name not in ('read_file','view_file') or not isinstance(args,dict) or not args:
   previous = None
   indices.clear()
   continue
  # Include offsets and limits: sequential ranges of one file are ordinary work.
  signature = json.dumps([name,args],sort_keys=True)
  if signature != previous:
   previous = signature
   indices.clear()
  indices.add(index)
 def identity(index):
  return hashlib.sha256(f'{conversation}/{launch}/{index}'.encode()).hexdigest()[:24]
 result = {}
 if len(indices) >= 5:
  result['identical_reads'] = identity(previous)
 if pending:
  result['pending_command'] = identity(min(pending))
 return result


def rotation_observation(rotation):
 """Validate the versioned controller evidence before exporting it."""
 enabled = rotation.get('enabled')
 if rotation.get('version') != 1 or not isinstance(enabled,list):
  return {}
 enabled = [k for k in enabled[:32] if isinstance(k,str) and re.fullmatch('[0-9a-f]{64}',k)]
 models = {}
 def add(model,key,item):
  if not label(model) or key not in enabled or not isinstance(item,dict):
   return
  at = number(item.get('at'))
  if not at or item.get('status') not in ('errors','working','interrupted'):
   return
  records = models.setdefault(model,{})
  if records.get(key,{}).get('at',0) >= at:
   return
  code = item.get('http_status')
  records[key] = dict(at=at, status=item['status'], attempted_at=number(item.get('attempted_at')),
                      http_status=code if type(code)==int and 400<=code<=599 else None)
 for model,items in rotation.get('model_observations',{}).items():
  if isinstance(items,dict):
   for key,item in items.items():
    add(model,key,item)
 for key,item in rotation.get('observations',{}).items():
  if isinstance(item,dict):
   add(item.get('model'),key,item)
 result = dict(gemini_enabled=enabled,gemini_model_observations=models,
               gemini_exhausted=rotation.get('exhausted') is True,
               gemini_exhausted_since=number(rotation.get('exhausted_since')))
 if rotation.get('key') in enabled:
  result.update(provider_key_id=rotation['key'],provider_key_source='Gemini launch record',
                route=enabled.index(rotation['key'])+1,gemini_attempt_started=number(rotation.get('started')))
 return result


def collect(roots,host):
 now=time.time();rows=[];failures=[];inventory={};host_enabled=set()
 for root in roots:
  root=Path(root);data=root/'agent_data';profile_env=profile_environment(root)
  allowed=read(root/'gemini-routing.json').get('enabled')
  if isinstance(allowed,list):host_enabled.update(k for k in allowed if isinstance(k,str) and re.fullmatch('[0-9a-f]{64}',k))
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
     if key['family']=='gemini' and isinstance(allowed,list):
      key['enabled']=key['id'] in allowed
     inventory[(key['id'],key['name'])]=key
    held=[];job_live=False
    for path in (runtime/'gpu-jobs').glob('*.json'):
     job=read(path)
     if alive(job.get('helper_pid'),job.get('helper_token')) or alive(job.get('pid'),job.get('token')):
      job_live=True;held.extend(g for g in job.get('gpus',[]) if type(g)==int and 0<=g<100)
    progress_samples=gpu_progress(work,state,worker.get('kind')) if job_live else []
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
         'state':status,'controller_alive':ctl_live,'process_alive':native_live,'foreground_job':bool(progress_samples),'owned_job':job_live,
         'job_progress_samples':progress_samples,
         'native_provider_retry':native_provider_retry(runtime,state,worker.get('kind'),now),
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
    if worker.get('kind')=='gemini':
     row.update(rotation_observation(state.get('gemini_rotation',{})))
     if state.get('blocked_reason') in ('gemini_context_limit','gemini_compaction_failed'):
      row['gemini_compaction_block']=state['blocked_reason']
     if native_live and not paused and not stopped:
      row['gemini_tool_observation']=gemini_tool_observation(runtime,state)
    rows.append(row)
 # Historical roots can contribute key labels even when all of their runs are retired.
 # Apply the current host policy to those identities without treating a retired configuration as enabled.
 for key in inventory.values():
  if host_enabled and key['family']=='gemini' and 'enabled' not in key:
   key['enabled']=key['id'] in host_enabled
 return {'host':host,'sampled_at':now,'workers':rows,'errors':failures,'provider_keys':list(inventory.values())}


if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--host',required=True);p.add_argument('root',nargs='+');a=p.parse_args()
 print(json.dumps(collect(a.root,a.host),separators=(',',':')))

"""Private read-only rStation researcher dashboard; no worker control endpoints."""
import argparse
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
from pathlib import Path
import shlex
import sqlite3
import subprocess
import threading
import time
import urllib.request
from urllib.parse import urlsplit,quote

from fleet_collect import label,number,stamp,canonical_key_name
ROOT=Path(__file__).resolve().parent
ATTENTION_SECONDS=6*3600
ORDER={'needs_attention':0,'transient_error':1,'working':2,'intentional':3}


def response_clock(row,now):
 """Only a real model response resets the durable six-hour clock."""
 def valid(value):return number(value) is not None and 0<value<=now
 times=[row.get(k) for k in ('last_response','last_real_response') if valid(row.get(k))]
 since=max(times) if times else row.get('first_observed_at')
 if not valid(since):since=None
 return since


def health(row,now):
 if row.get('intentional_stop'):return 'intentional','Operator-authorized pause or stop'
 since=response_clock(row,now)
 if since is not None and now-since>=ATTENTION_SECONDS:
  return 'needs_attention','No real model response for at least 6 hours'
 sample=row.get('sampled_at')
 if number(sample) is None or not 0<=now-sample<=300:return 'transient_error','Host evidence is missing or stale'
 retry=row.get('native_provider_retry')
 if isinstance(retry,dict) and number(retry.get('at')) is not None and 0<=now-retry['at']<=300:
  return 'transient_error','Native provider retry'+(' · HTTP '+str(retry['http_status']) if retry.get('http_status') else '')
 state=row.get('state')
 if state not in ('running','continuing'):
  return 'transient_error',{'blocked':'Controller reports a block','retrying':'Retrying after a failed request','paused':'Paused without a verified owner reason','stopped':'Stopped without a verified owner reason'}.get(state,'Worker is not currently working')
 job=row.get('last_job_progress')
 if row.get('foreground_job') and job and 0<=now-job<=600:return 'working','GPU experiment is advancing'
 progress=max((v for v in (row.get('last_response'),row.get('last_tool')) if number(v) is not None and 0<v<=now),default=None)
 if progress and now-progress<=1800:return 'working','Recent response or completed tool'
 return 'transient_error','No recent response or verified work'


def attention(row,now):
 """Hook fields are derived from the same status as the page and counts."""
 since=response_clock(row,now)
 status,_=health(row,now)
 return {'needs_attention':status=='needs_attention',
         'attention_known':since is not None,'attention_since':since,
         'attention_due_at':since+ATTENTION_SECONDS if since is not None else None,
         'attention_threshold_seconds':ATTENTION_SECONDS}


def with_job_progress(row,previous,now):
 """Require two fresh observations of the same live job, tool and output file."""
 def samples(values):
  return {v['id']:v for v in values if isinstance(v,dict) and isinstance(v.get('id'),str) and type(v.get('step'))==int and v['step']>=0}
 current=samples(row.get('job_progress_samples',[]));old=samples(previous.get('job_progress_samples',[]))
 fresh=0<now-previous.get('sampled_at',0)<=300
 entries=[]
 for key,value in current.items():
  last=None
  if row.get('foreground_job') and fresh and key in old:
   if value['step']>old[key]['step']:last=now
   elif value['step']==old[key]['step']:last=old[key].get('advanced_at')
  entries.append({'id':key,'step':value['step'],'advanced_at':last})
 return dict(row,job_progress_samples=entries,last_job_progress=max((e['advanced_at'] for e in entries if e['advanced_at']),default=None))



def family_summary(rows, now):
 """Provider evidence comes from fresh real responses, never PID or tool activity."""
 result=[]
 for family in ('gemini','claude','codex'):
  selected=[r for r in rows if r.get('family')==family]
  counts={key:sum(r.get('health')==key for r in selected) for key in ORDER}
  active=[r for r in selected if r.get('health')!='intentional']
  fresh=[r for r in active if 0<=now-r.get('sampled_at',0)<=300]
  responses=[r['last_response'] for r in fresh if r.get('last_response') and r['last_response']<=now]
  latest=max(responses,default=None)
  if latest and now-latest<=600:status='responding'
  elif not active and selected:status='inactive'
  elif not fresh or not responses:status='unknown'
  else:status='no_recent_response'
  result.append({'family':family,'status':status,'counts':counts,'total':len(selected),
                 'active_total':len(active),'need_attention':counts['needs_attention'],'last_response':latest})
 return result



def with_gemini_incidents(row, previous, now):
 """Track each long-tool wait by native launch and step, across collection restarts."""
 if row.get('family')!='gemini':
  return row
 tools=row.pop('gemini_tool_observation',{})
 signals={}
 if row.get('gemini_exhausted'):
  signals['routes_exhausted']=str(row.get('gemini_exhausted_since') or row.get('model'))
 if row.get('gemini_compaction_block'):
  signals['compaction_blocked']=row['gemini_compaction_block']
 if tools.get('identical_reads'):
  signals['identical_reads']=tools['identical_reads']
 if tools.get('pending_command'):
  signals['long_command']=tools['pending_command']
 tracking={}
 incidents={}
 for kind,identity in signals.items():
  old=previous.get('gemini_incident_tracking',{}).get(kind,{})
  since=old.get('since',now) if old.get('identity')==identity else now
  tracking[kind]=dict(identity=identity,since=since)
  if kind=='long_command':
   progress=max(since,row.get('last_job_progress') or 0)
   if now-progress<3600:
    continue
  incidents[kind]=since
 return dict(row,gemini_incident_tracking=tracking,gemini_incidents=incidents)


def gemini_key_evidence(key_id,rows,now):
 """The most recent terminal outcome for each model on this key survives switches."""
 records={}
 eligible=set()
 for row in rows:
  if row.get('intentional_stop') or not 0<=now-row.get('sampled_at',0)<=300:
   continue
  if key_id not in row.get('gemini_enabled',[]):
   continue
  if row.get('model'):
   eligible.add(row['model'])
  for model,items in row.get('gemini_model_observations',{}).items():
   item=items.get(key_id,{})
   if not 0<(item.get('at') or 0)<=now:
    continue
   if item['at']>records.get(model,{}).get('at',0):
    records[model]=item
 return records,eligible


def key_observation(key_id,rows,now,enabled=None):
 selected=[r for r in rows if r.get('provider_key_id')==key_id and not r.get('intentional_stop') and 0<=now-r.get('sampled_at',0)<=300]
 responses=[r['last_response'] for r in selected if r.get('started_at') and r.get('last_response') and r['started_at']<=r['last_response']<=now]
 latest=max(responses,default=None)
 failing=[r for r in selected if r.get('started_at') and (r.get('last_response') or 0)<r['started_at'] and r.get('state') in ('retrying','blocked','paused','stopped') and (r.get('error',{}).get('http_status') or r.get('error',{}).get('category')=='provider_unavailable')]
 recent=latest is not None and now-latest<=600
 status='mixed' if recent and failing else 'working' if recent else 'errors' if failing else 'unknown'
 errors=sorted({str(r.get('error',{}).get('http_status') or 'provider unavailable') for r in failing})
 reports,eligible=gemini_key_evidence(key_id,rows,now)
 if reports:
  failed=[(model,item) for model,item in reports.items() if item['status']=='errors']
  successes=[item['at'] for item in reports.values() if item['status']=='working']
  interrupted=any(item['status']=='interrupted' for item in reports.values())
  latest=max([latest or 0,*successes]) or None
  # A recorded failure remains the current outcome until a newer attempt supersedes it.
  # Old successes of unrelated models have dates in the breakdown but cannot dilute a red badge.
  responding=any(now-at<=600 for at in successes)
  if failed:
   status='mixed' if responding else 'errors'
  elif responding:
   status='working'
  elif interrupted:
   status='interrupted'
  else:
   status='last_success'
  errors=[model+' '+('HTTP '+str(item['http_status']) if item.get('http_status') else 'provider failure') for model,item in failed]
 elif status=='unknown' and (eligible or enabled is True):
  status='untried'
 if enabled is False and not selected and not eligible:
  status='disabled'
 model_status={model:dict(status='untried') for model in eligible}
 for row in selected:
  model=row.get('model')
  if model not in eligible or model in reports or not row.get('process_alive'):
   continue
  started=number(row.get('gemini_attempt_started'))
  if started and started<=now and now-row['sampled_at']<=300:
   model_status[model]=dict(status='attempting',at=started)
   if status=='untried':
    status='attempting'
 model_status.update(reports)
 models=set(model_status)|{r['model'] for r in selected if r.get('model')}
 return {'status':status,'last_response':latest,'workers':len(selected),'errors':errors,
         'models':sorted(models),'model_status':model_status}


class Store:
 def __init__(self,path):
  self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
  with self.connect() as con:
   con.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS samples(worker TEXT,at REAL,health TEXT,response REAL,tool REAL,gpus INTEGER,PRIMARY KEY(worker,at));
CREATE TABLE IF NOT EXISTS current(worker TEXT PRIMARY KEY,body TEXT);
CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY,at REAL,error TEXT);
CREATE TABLE IF NOT EXISTS profiles(handle TEXT PRIMARY KEY,at REAL,body TEXT);
CREATE TABLE IF NOT EXISTS key_inventory(host TEXT,key_id TEXT,name TEXT,at REAL,body TEXT,PRIMARY KEY(host,key_id,name));
CREATE TABLE IF NOT EXISTS key_samples(host TEXT,key_id TEXT,at REAL,status TEXT,response REAL,PRIMARY KEY(host,key_id,at));
CREATE INDEX IF NOT EXISTS key_samples_at ON key_samples(at);
CREATE INDEX IF NOT EXISTS samples_at ON samples(at);
''')
 @contextmanager
 def connect(self):
  con=sqlite3.connect(self.path,timeout=10)
  try:
   with con:yield con
  finally:con.close()
 def record(self,snapshot):
  now=snapshot['sampled_at']
  with self.connect() as con:
   con.execute('INSERT OR REPLACE INTO hosts VALUES(?,?,?)',(snapshot['host'],now,';'.join(snapshot.get('errors',[])) or None))
   for row in snapshot['workers']:
    previous=con.execute('SELECT body FROM current WHERE worker=?',(row['id'],)).fetchone()
    previous=json.loads(previous[0]) if previous else {}
    row=with_job_progress(row,previous,now)
    row=with_gemini_incidents(row,previous,now)
    retry=row.get('native_provider_retry');old_retry=previous.get('native_provider_retry')
    if isinstance(retry,dict) and isinstance(old_retry,dict) and retry.get('id')==old_retry.get('id'):
     # Unrelated transcript writes cannot refresh an already observed retry.
     retry['at']=min(retry['at'],old_retry['at'])
    first=previous.get('first_observed_at')
    if first is None:
     first=con.execute('SELECT MIN(at) FROM samples WHERE worker=?',(row['id'],)).fetchone()[0]
    row['first_observed_at']=first if first is not None else now
    responses=[previous.get('last_real_response'),previous.get('last_response'),row.get('last_response')]
    row['last_real_response']=max((v for v in responses if number(v) is not None and 0<v<=now),default=None)
    row['last_response']=row['last_real_response']
    state,reason=health(row,now);row=dict(row,health=state,health_reason=reason)
    con.execute('INSERT OR REPLACE INTO current VALUES(?,?)',(row['id'],json.dumps(row)))
    con.execute('INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?)',(row['id'],now,state,row.get('last_response'),row.get('last_tool'),len(row.get('gpu',[]))))
   if 'provider_keys' in snapshot and not snapshot.get('errors'):
    con.execute('DELETE FROM key_inventory WHERE host=?',(snapshot['host'],))
   for key in snapshot.get('provider_keys',[]):
    con.execute('INSERT OR REPLACE INTO key_inventory VALUES(?,?,?,?,?)',(snapshot['host'],key['id'],key['name'],now,json.dumps(key)))
   policies={}
   for key in snapshot.get('provider_keys',[]):
    policies.setdefault(key['id'],set()).add(key.get('enabled'))
   for ident,flags in policies.items():
    enabled=True if True in flags else False if False in flags else None
    observation=key_observation(ident,snapshot['workers'],now,enabled)
    con.execute('INSERT OR REPLACE INTO key_samples VALUES(?,?,?,?,?)',(snapshot['host'],ident,now,observation['status'],observation['last_response']))
   con.execute('DELETE FROM key_samples WHERE at<?',(now-8*86400,))
   con.execute('DELETE FROM samples WHERE at<?',(now-8*86400,))
 def failed(self,host):
  with self.connect() as con:con.execute('INSERT INTO hosts VALUES(?,?,?) ON CONFLICT(host) DO UPDATE SET error=excluded.error',(host,0,'collection_failed'))
 def profile(self,handle,data):
  with self.connect() as con:con.execute('INSERT OR REPLACE INTO profiles VALUES(?,?,?)',(handle,time.time(),json.dumps(data)))
 def view(self,now=None):
  now=now or time.time()
  with self.connect() as con:
   rows=[json.loads(x[0]) for x in con.execute('SELECT body FROM current')]
   hosts=[{'host':h,'sampled_at':at,'error':error,'stale':now-at>300} for h,at,error in con.execute('SELECT * FROM hosts')]
   profiles={h:(at,json.loads(body)) for h,at,body in con.execute('SELECT * FROM profiles')}
   inventory=list(con.execute('SELECT host,key_id,name,at,body FROM key_inventory'))
   key_responses=dict(con.execute('SELECT key_id,MAX(response) FROM key_samples GROUP BY key_id'))
   key_samples=list(con.execute('SELECT key_id,at,status FROM key_samples WHERE at>=? ORDER BY at',(now-5400,)))
   samples={}
   for worker,at,state,response,tool,gpus in con.execute('SELECT * FROM samples WHERE at>=? ORDER BY at',(now-7*86400,)):
    samples.setdefault(worker,[]).append((at,state,response,tool,gpus))
  counts={k:0 for k in ORDER};providers={}
  for row in rows:
   row.pop('job_progress_samples',None)
   row.pop('gemini_incident_tracking',None)
   if isinstance(row.get('native_provider_retry'),dict):row['native_provider_retry'].pop('id',None)
   row.update(attention(row,now))
   row['health'],row['health_reason']=health(row,now);counts[row['health']]+=1
   entries=samples.get(row['id'],[]);history=[None]*168;observed=lost=gpu_hours=0
   for i,(at,state,response,tool,gpus) in enumerate(entries):
    index=int(at//3600)-int(now//3600)+167
    if 0<=index<168:
     # Preserve sampled status; a completed tool cannot repaint overdue red.
     color={'transient_error':'degraded','needs_attention':'down'}.get(state,state)
     if history[index]!='working':history[index]=color
    if i and at>=now-86400:
     previous=entries[i-1];dt=at-previous[0]
     if 0<dt<=300:
      observed+=dt
      if previous[1] in ('down','degraded','transient_error','needs_attention'):lost+=dt
      gpu_hours+=previous[4]*dt/3600
   row.update(history=history,observed_hours=observed/3600,stalled_hours=lost/3600,gpu_hours=gpu_hours,
              registered_at=None,papers_total=None,papers_7d=None,last_public_activity=None,profile_sampled_at=None)
   if row.get('handle') in profiles:
    at,profile=profiles[row['handle']];row.update(profile);row['profile_sampled_at']=at
   key=(row['host'],row['family'],row.get('model'),row.get('route'))
   p=providers.setdefault(key,{'host':key[0],'family':key[1],'model':key[2],'route':key[3],'workers':0,'last_success':None,'last_error':None,'stale':False})
   p['workers']+=1
   work=max(row.get('last_response') or 0,row.get('last_tool') or 0)
   if row.get('started_at') and work>=row['started_at']:p['last_success']=max(p['last_success'] or 0,work)
   p['stale']|=now-row.get('sampled_at',0)>300
   if row.get('error',{}).get('category') and row['state'] in ('blocked','retrying'):
    p['last_error']=row['error']
  keys={}
  for host,ident,name,at,body in inventory:
   info=json.loads(body)
   k=keys.setdefault(ident,{'id':ident,'names':set(),'labels':set(),'endpoint':info['endpoint'],'hosts':set(),'families':set(),'policy':set(),'sampled_at':0})
   k['policy'].add(info.get('enabled'))
   k['names'].add(name)
   canonical=canonical_key_name(info.get('label'))
   if canonical:k['labels'].add(canonical)
   k['hosts'].add(host);k['families'].add(info['family']);k['sampled_at']=max(k['sampled_at'],at)
  # Installation profiles may supply an authorized backup missing from a worker's legacy key inventory.
  for row in rows:
   if row.get('intentional_stop') or not 0<=now-row.get('sampled_at',0)<=300:continue
   for ident in row.get('gemini_enabled',[]):
    if ident in keys:
     keys[ident]['hosts'].add(row['host'])
     keys[ident]['policy'].add(True)
  for ident,k in keys.items():
   flags=k.pop('policy')
   enabled=True if True in flags else False if False in flags else None
   k.update(key_observation(ident,rows,now,enabled));k['names']=sorted(k['names']);k['labels']=sorted(k['labels']);k['hosts']=sorted(k['hosts']);k['families']=sorted(k['families']);k['history']=[None]*18
   k['display_name']=k['labels'][0] if len(k['labels'])==1 else None
   k['name_status']='named' if len(k['labels'])==1 else 'conflict' if k['labels'] else 'missing'
   k['last_response']=max(k['last_response'] or 0,key_responses.get(ident) or 0) or None
   if now-k['sampled_at']>300:k['status']='unknown'
   elif k['status']=='unknown' and k['last_response'] and 0<=now-k['last_response']<=600:k['status']='working'
  priority={'unknown':0,'disabled':0,'untried':0,'attempting':1,'interrupted':1,'last_success':1,'working':2,'mixed':3,'errors':4}
  for ident,at,status in key_samples:
   if ident not in keys:continue
   index=int(at//300)-int(now//300)+17
   if 0<=index<18:
    old=keys[ident]['history'][index]
    if old is None or priority[status]>priority[old]:keys[ident]['history'][index]=status
  key_rows=sorted(keys.values(),key=lambda k:({'errors':0,'mixed':1,'interrupted':2,'working':3,'last_success':4,'attempting':4,'untried':5,'unknown':6,'disabled':7}[k['status']],k['endpoint'],k['names']))
  rows.sort(key=lambda r:(not r['needs_attention'],ORDER[r['health']],r.get('problem') or '',r['id']))
  return {'now':now,'active_total':len(rows)-counts['intentional'],'need_attention':counts['needs_attention'],'attention_threshold_seconds':ATTENTION_SECONDS,'counts':counts,'hosts':hosts,'workers':rows,'providers':list(providers.values()),'families':family_summary(rows,now),'provider_keys':key_rows,'key_identity_unknown':sum(not r.get('provider_key_id') and r['health']!='intentional' for r in rows),'history_start':min((v[0][0] for v in samples.values() if v),default=None)}


def fetch_public(handle):
 """Only public API, no researcher keys. Reduce responses immediately to metadata."""
 def get(suffix):
  request=urllib.request.Request('https://rstation.ai/api/v1/agents/'+quote(handle,safe='')+suffix,headers={'User-Agent':'rstation-agent-pool/0.1'})
  with urllib.request.urlopen(request,timeout=10) as response:return json.load(response)['data']
 profile=get('');result={'registered_at':stamp(profile.get('created_at')),'papers_total':number(profile.get('papers_count')),'last_public_activity':None,'papers_7d':None}
 latest=[]
 for suffix,kind in [('/posts?sort=new&limit=1','post'),('/comments?limit=1','comment'),('/papers?sort=new&limit=100','paper')]:
  data=get(suffix);items=data.get('items',[]) if isinstance(data,dict) else []
  times=[stamp(x.get('published_at') or x.get('created_at')) for x in items if isinstance(x,dict)]
  times=[v for v in times if v]
  if times:latest.append({'at':max(times),'kind':kind})
  if kind=='paper':
   # Only claim an exact seven-day count if the result covers that window.
   if not data.get('next_cursor') or (times and min(times)<time.time()-7*86400):result['papers_7d']=sum(v>=time.time()-7*86400 for v in times)
 if latest:result['last_public_activity']=max(latest,key=lambda x:x['at'])
 return result


def collect_host(host,roots,script):
 if host not in ('h4','h6','h7'):raise ValueError('Unsupported host')
 command=shlex.join(['python3','-','--host',host,*roots])
 proc=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=15',host,command],input=script,capture_output=True,timeout=55)
 if proc.returncode or len(proc.stdout)>8*1024*1024:raise ValueError('Host collection failed')
 value=json.loads(proc.stdout)
 if value.get('host')!=host or not isinstance(value.get('workers'),list):raise ValueError('Invalid collection')
 return value


class Collector:
 def __init__(self,store,config):self.store=store;self.config=config;self.stop=threading.Event();self.profile_checked={}
 def tick(self):
  script=(ROOT/'fleet_collect.py').read_bytes();hosts=self.config['hosts']
  with ThreadPoolExecutor(max_workers=len(hosts) or 1) as executor:
   futures={h:executor.submit(collect_host,h,roots,script) for h,roots in hosts.items()}
   for host,future in futures.items():
    try:
     data=future.result()
     for row in data['workers']:
      mapping=self.config.get('accounts',{}).get(host+'/'+row['run']+'/'+row['worker'],{})
      if label(mapping.get('handle')):row['handle']=mapping['handle']
      if mapping.get('intentional_stop') is True and mapping.get('owner_message') and row['state'] in ('stopped','paused'):row['intentional_stop']=True
     self.store.record(data)
    except Exception:self.store.failed(host)
 def profiles_tick(self):
  pending=[]
  for row in self.store.view()['workers']:
   handle=row.get('handle')
   if handle and time.time()-self.profile_checked.get(handle,0)>3600:
    self.profile_checked[handle]=time.time()
    pending.append(handle)
    if len(pending)>=8:break
  def fetch(handle):
   try:self.store.profile(handle,fetch_public(handle))
   except Exception:pass
  with ThreadPoolExecutor(max_workers=2) as executor:list(executor.map(fetch,pending))
 def profiles_run(self):
  while not self.stop.is_set():
   self.profiles_tick();self.stop.wait(60)
 def run(self):
  while not self.stop.is_set():
   self.tick();self.stop.wait(120)


def handler(store):
 class Handler(BaseHTTPRequestHandler):
  def log_message(self,*args):pass
  def do_GET(self):
   host=self.headers.get('Host','').split(':')[0]
   if host not in ('127.0.0.1','localhost'):self.send_error(403);return
   route=urlsplit(self.path).path
   if route=='/api/snapshot':body=json.dumps(store.view()).encode();mime='application/json'
   elif route in ('/','/app.js','/style.css','/icon.svg','/favicon.ico'):
    filename={'/':'index.html','/app.js':'app.js','/style.css':'style.css','/icon.svg':'icon.svg','/favicon.ico':'favicon.ico'}[route]
    body=(ROOT/'dashboard'/filename).read_bytes();mime={'/':'text/html','/app.js':'text/javascript','/style.css':'text/css','/icon.svg':'image/svg+xml','/favicon.ico':'image/x-icon'}[route]
   else:self.send_error(404);return
   self.send_response(200);self.send_header('Content-Type',mime+'; charset=utf-8');self.send_header('Content-Length',str(len(body)))
   self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
   self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
   self.end_headers();self.wfile.write(body)
 return Handler


def main():
 p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,required=True);p.add_argument('--data',type=Path,required=True);p.add_argument('--port',type=int,default=8911);p.add_argument('--once',action='store_true');a=p.parse_args()
 config=json.loads(a.config.read_text());store=Store(a.data/'history.sqlite3');collector=Collector(store,config)
 if a.once:collector.tick();print(json.dumps(store.view()));return
 server=ThreadingHTTPServer(('127.0.0.1',a.port),handler(store));thread=threading.Thread(target=collector.run,daemon=True);thread.start();threading.Thread(target=collector.profiles_run,daemon=True).start()
 try:server.serve_forever()
 finally:collector.stop.set();server.server_close()

if __name__=='__main__':main()

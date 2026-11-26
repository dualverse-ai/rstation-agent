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

from fleet_collect import label,number,stamp
ROOT=Path(__file__).resolve().parent
ORDER={'down':0,'degraded':1,'unknown':2,'working':3,'intentional':4}


def health(row,now):
 if now-row.get('sampled_at',0)>300:return 'unknown','Host evidence is stale'
 if row.get('intentional_stop'):return 'intentional','Operator-authorized pause or stop'
 state=row.get('state')
 if state=='blocked':return 'down','Controller reports a block'
 progress=max(row.get('last_response') or 0,row.get('last_tool') or 0)
 if not progress:return 'unknown','No response or completed-tool timestamp'
 if state in ('stopped','paused'):return 'down','Stopped without a verified owner reason'
 age=now-progress
 if age>10800:return 'down','No real response or completed tool for more than 3 hours'
 if state=='retrying':return 'degraded','Retrying after a failed request'
 if age<=1800:return 'working','Recent response or completed tool'
 if row.get('foreground_job') and now-(row.get('last_response') or 0)<=10800:return 'working','Owned foreground job and recent response'
 return 'degraded','No response or completed tool for 30 minutes'


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
  elif not fresh or all(r.get('health')=='unknown' for r in active):status='unknown'
  else:status='no_recent_response'
  result.append({'family':family,'status':status,'counts':counts,'total':len(selected),
                 'need_attention':counts['down']+counts['degraded'],'last_response':latest})
 return result


class Store:
 def __init__(self,path):
  self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
  with self.connect() as con:
   con.executescript('''PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS samples(worker TEXT,at REAL,health TEXT,response REAL,tool REAL,gpus INTEGER,PRIMARY KEY(worker,at));
CREATE TABLE IF NOT EXISTS current(worker TEXT PRIMARY KEY,body TEXT);
CREATE TABLE IF NOT EXISTS hosts(host TEXT PRIMARY KEY,at REAL,error TEXT);
CREATE TABLE IF NOT EXISTS profiles(handle TEXT PRIMARY KEY,at REAL,body TEXT);
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
    state,reason=health(row,now);row=dict(row,health=state,health_reason=reason)
    con.execute('INSERT OR REPLACE INTO current VALUES(?,?)',(row['id'],json.dumps(row)))
    con.execute('INSERT OR REPLACE INTO samples VALUES(?,?,?,?,?,?)',(row['id'],now,state,row.get('last_response'),row.get('last_tool'),len(row.get('gpu',[]))))
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
   samples={}
   for worker,at,state,response,tool,gpus in con.execute('SELECT * FROM samples WHERE at>=? ORDER BY at',(now-7*86400,)):
    samples.setdefault(worker,[]).append((at,state,response,tool,gpus))
  counts={k:0 for k in ORDER};providers={}
  for row in rows:
   row['health'],row['health_reason']=health(row,now);counts[row['health']]+=1
   entries=samples.get(row['id'],[]);history=[None]*168;observed=lost=gpu_hours=0
   for i,(at,state,response,tool,gpus) in enumerate(entries):
    index=int(at//3600)-int(now//3600)+167
    if 0<=index<168:
     # Green requires actual work in this hour, not merely an alive process.
     progress=max(response or 0,tool or 0)
     color='working' if int(progress//3600)==int(at//3600) else 'intentional' if state=='intentional' else 'unknown' if state=='unknown' else 'degraded'
     if history[index]!='working':history[index]=color
    if i and at>=now-86400:
     previous=entries[i-1];dt=at-previous[0]
     if 0<dt<=300:
      observed+=dt
      if previous[1] in ('down','degraded'):lost+=dt
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
  rows.sort(key=lambda r:(ORDER[r['health']],r.get('problem') or '',r['id']))
  return {'now':now,'counts':counts,'hosts':hosts,'workers':rows,'providers':list(providers.values()),'families':family_summary(rows,now),'history_start':min((v[0][0] for v in samples.values() if v),default=None)}


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

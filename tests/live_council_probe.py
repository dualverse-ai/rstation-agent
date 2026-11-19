"""Explicit, bounded real-provider council trial; no accounts, posts or GPU jobs.

Run manually with the dedicated worker profile loaded. This is not collected by pytest.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import save, read, safe_environment
from main import parser, build_config
from council import Store, configure
from council_service import Engine


def fallback_environment(env, address):
    result=dict(env)
    for family in ('CODEX','CLAUDE','GEMINI'):
        prefix='RSTATION_WORKER_'+family+'_'
        # Collapse all parallel backup fields together. A live profile may
        # have several backups and a matching semicolon-delimited proxy list.
        for field in ('API_KEY','BASE_URL','HTTP_PROXY','HTTPS_PROXY'):
            result[prefix+'BACKUP_'+field]=env.get(prefix+field,'')
        result[prefix+'API_KEY']='local-rejected-probe'
        result[prefix+'BASE_URL']=address
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--force-fallback',action='store_true',help='Reject local-primary requests, then use each real configured primary as backup.')
    p.add_argument('--timeout',type=int,default=600)
    p.add_argument('--platform-proxy')
    p.add_argument('--peer-files',action='store_true',help='Read same-group peer workspace files and reread them on follow-up.')
    p.add_argument('--read-files',action='store_true',help='Verify automatic local file discovery and fresh reads on a resumed conversation.')
    args=p.parse_args()
    if args.run_dir.exists():raise ValueError('Choose a fresh disposable run directory.')
    args.run_dir.mkdir(parents=True,mode=0o700)
    c=build_config(parser().parse_args(['start','rl_learn_to_explore','--name','council_live_probe',
        '--url','https://rstation.ai','--codex','1','--claude','1','--gemini','1']),{'category':'open_ended'})
    c['platform_proxy']=args.platform_proxy
    save(args.run_dir/'run.json',c)
    for w in c['workers']:(args.run_dir/w['name']).mkdir()
    env=safe_environment()
    rejector=None
    if args.force_fallback:
        class Reject(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def reject(self):
                body=b'{"error":{"code":401,"message":"Deliberate local council fallback trial","type":"authentication_error","status":"UNAUTHENTICATED"}}'
                self.send_response(401);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            do_GET=do_POST=reject
        rejector=ThreadingHTTPServer(('127.0.0.1',0),Reject)
        threading.Thread(target=rejector.serve_forever,daemon=True).start()
        env=fallback_environment(env,f'http://127.0.0.1:{rejector.server_port}')
    configure(args.run_dir,'all',True,env,notify=False,
              # The deliberate local 401 consumes two launches. Allow several
              # real-backup retries so a transient upstream TLS failure does
              # not turn this route-validation probe into a false negative.
              limits={'max_attempts':8,'attempt_seconds':180,'turn_seconds':args.timeout})
    if args.peer_files: args.read_files=True
    store=Store(args.run_dir)
    mapping={'codex':'claude_1','claude':'gemini_1','gemini':'codex_1'}
    nonce='ORCHID-'+str(int(time.time()))
    projects={worker:args.run_dir/(next(w['name'] for w in c['workers'] if w['name']!=worker) if args.peer_files else worker)/('current_method_'+family) for family,worker in mapping.items()}
    for family,worker in mapping.items():
        if args.read_files:
            project=projects[worker];project.mkdir()
            (project/'README.md').write_text('Research label: '+nonce+'\nRead method.py for the current observation history and measurements.csv for evaluation.\n')
            (project/'method.py').write_text('# The learned world model receives 64 factual observations.\nHISTORY_LENGTH = 64\n')
            (project/'measurements.csv').write_text('run,completion_count\nparent,0\ncandidate,0\n')
            question=('Read the files in '+str(project.resolve())+' directly using your file tools. '
                      'Do not ask me to attach or paste them. Report the research label, the implemented history length and the candidate completion count. '
                      'Then suggest one research hypothesis consistent with these facts. Under 130 words. Do not execute or edit anything.')
        else:
            question=f'Remember research label {nonce}. In under 130 words, suggest one task-general Thinker extension for sparse-reward exploration and one control that distinguishes its proposed mechanism from simply more search. Stay within Thinker. Include the label. No code, tools or experiment execution.'
        store.submit(worker,'probe',question,family=family)
    manager=Engine(args.run_dir,env);started=time.monotonic();followed=set();tokens={}
    try:
        while time.monotonic()-started<args.timeout:
            manager.tick()
            for family,worker in mapping.items():
                session=store.snapshot()['sessions'][worker+':probe'];last=session['turns'][-1]
                if last['status']=='failed':raise RuntimeError(family+': '+last['error'])
                if last['status']=='complete' and family not in followed:
                    assert nonce in last['answer'] and len(last['answer'])>50
                    if args.read_files:
                        assert '64' in last['answer'] and '0' in last['answer'], last['answer']
                        project=projects[worker]
                        assert (project/'method.py').read_text().endswith('HISTORY_LENGTH = 64\n')
                        (project/'README.md').write_text('Research label: '+nonce+'\nFollow-up marker: UPDATED-'+family+'\n')
                        (project/'measurements.csv').write_text('run,completion_count\nparent,0\ncandidate,3\n')
                    tokens[family]=read(store.root/worker/'probe/native.json')['token'];assert tokens[family]
                    followup=('The local README.md and measurements.csv in '+str(projects[worker])+' have changed. Read them again now. '
                              'Report the exact follow-up marker, research label and new candidate completion count, then explain what this changes and what remains uncertain. Under 100 words.'
                              if args.read_files else 'Recall the exact research label from my first question. Briefly explain one failure mode of the extension you just suggested. Do not introduce an unrelated approach. Under 100 words.')
                    store.submit(worker,'probe',followup)
                    followed.add(family)
                elif last['status']=='complete' and len(session['turns'])==2:
                    assert nonce in last['answer'] and len(last['answer'])>50
                    assert read(store.root/worker/'probe/native.json')['token']==tokens[family]
                    if args.read_files:
                        assert 'UPDATED-'+family in last['answer'] and '3' in last['answer'], last['answer']
                        assert (projects[worker]/'measurements.csv').read_text().endswith('candidate,3\n')
            if len(followed)==3 and all(len(s['turns'])==2 and s['turns'][-1]['status']=='complete' for s in store.snapshot()['sessions'].values()):
                result={'success':True,'seconds':round(time.monotonic()-started,1),'forced_fallback':args.force_fallback,'read_files':args.read_files,'peer_files':args.peer_files,
                        'families':{s['family']:{'same_session':True,'turns':[{'status':t['status'],'provider':t['provider'],'attempts':t['attempts'],'answer':t['answer']} for t in s['turns']]} for s in store.snapshot()['sessions'].values()}}
                save(args.run_dir/'result.json',result)
                summary={**result,'families':{k:{'same_session':v['same_session'],
                    'turns':[{a:b for a,b in t.items() if a!='answer'} for t in v['turns']]}
                    for k,v in result['families'].items()}}
                print(json.dumps(summary,indent=2))
                return
            time.sleep(.25)
        raise TimeoutError('Council live probe exceeded its bounded timeout.')
    finally:
        manager.close()
        if rejector:rejector.shutdown();rejector.server_close()


if __name__=='__main__':main()

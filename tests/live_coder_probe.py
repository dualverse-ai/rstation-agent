"""Bounded, opt-in coder and skill trial with dedicated rStation credentials."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import read,save,safe_environment
from main import parser,build_config
from coder import Store,configure,prepare_worker
from coder_service import Engine
from council import configure as council_configure,prepare_worker as council_prepare
from capabilities import install_skills,reminder
from recovery import ResearchManager


def until(engine,condition,seconds):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        engine.tick()
        if condition():return
        time.sleep(.25)
    raise TimeoutError('Bounded probe deadline reached.')


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--run-dir',type=Path,required=True)
    p.add_argument('--parent-route',type=int,default=0)
    p.add_argument('--parent-only-from',type=Path,help='Reuse completed isolated coder output for another native parent.')
    p.add_argument('--parent',choices=('codex','claude','gemini'),default='codex')
    args=p.parse_args();run=args.run_dir.resolve();run.mkdir(mode=0o700,parents=True,exist_ok=False)
    config=build_config(parser().parse_args(['start','probe','--name','coder_probe','--codex','1','--claude','1','--gemini','1']),{'category':'open_ended'})
    config['live_input']=False;save(run/'run.json',config)
    env=safe_environment();worker=args.parent+'_1'
    for w in config['workers']:
        work=run/w['name'];work.mkdir()
        (work/'project').mkdir();(work/'project/stats.py').write_text('def mean(values):\n    return sum(values) / len(values)\n')
        (work/'reference.txt').write_text('FILE_ONLY_NONCE_CODER_6993\n')
    # No platform lookup, registration or publication in this probe.
    council_configure(run,'all',True,env,statement='Isolated operational probe; no research or publication.',notify=False)
    configure(run,'all',True,env,notify=False,limits={'max_attempts':2,'attempt_seconds':150,'turn_seconds':360})
    store=Store(run);key=worker+':native'
    prompt=('Implement mean(values) that accepts any iterable, raises ValueError on empty input, and handles generators. '
            'Add test_stats.py using unittest and run it. Read the original researcher reference file at '+str(run/worker/'reference.txt')+'. '
            'Use the shell to fetch https://example.com with urllib.request, record its HTTP status and title in web-check.txt, '
            'and report the exact reference nonce. Do not invent results. Keep code changes small. No account, publication or GPU work.')
    result={'parent':args.parent,'model':'gpt-6.2-sol'}
    if args.parent_only_from:
        import shutil
        old=Store(args.parent_only_from)
        session=old.snapshot()['sessions']['codex_1:native']
        assert session['turns'][-1]['status']=='complete'
        session['worker']=worker
        root=store.root/worker/'native';root.mkdir(parents=True)
        shutil.copytree(old.root/'codex_1/native/workspace',root/'workspace')
        snapshot=read(old.root/'codex_1/native/files.json')
        # The completed source trial already applied its output. This parent
        # starts from the same original fixture and independently consumes it.
        original=(run/worker/'project/stats.py').read_bytes()
        import hashlib
        snapshot['source']=str(run/worker/'project')
        snapshot['base']={'stats.py':{'sha256':hashlib.sha256(original).hexdigest(),'mode':420}}
        save(root/'files.json',snapshot)
        save(store.root/'store.json',{'sessions':{key:session}})
        result['reused_coder_output']=str(args.parent_only_from)
    else:
        store.submit(worker,'native',prompt,path='project')
        engine=Engine(run,env);result={'parent':args.parent,'model':'gpt-6.2-sol'}
        try:
            until(engine,lambda:store.snapshot()['sessions'][key]['turns'][-1]['status'] in ('complete','failed'),370)
            turn=store.snapshot()['sessions'][key]['turns'][-1]
            assert turn['status']=='complete',turn
            root=store.root/worker/'native';scratch=run/worker/'project'
            assert 'FILE_ONLY_NONCE_CODER_6993' in turn['answer'],turn['answer']
            assert (scratch/'test_stats.py').exists()
            output=subprocess.run([sys.executable,'-m','unittest','-v'],cwd=scratch,capture_output=True,text=True,timeout=30)
            assert output.returncode==0,output.stderr
            web=(scratch/'web-check.txt').read_text();assert '200' in web and 'Example Domain' in web,web
            assert not (root/'workspace').exists() and not (root/'files.json').exists()
            assert store.snapshot()['sessions'][key]['workspace_mode']=='direct'
            token=read(root/'native.json')['token'];assert token
            result.update(coding=True,original_read=True,web=True,tests=output.stderr.strip(),token=token)
            store.submit(worker,'native','In the same task, add a test for negative values, run all tests, and recall the exact nonce from the earlier question. Do not fetch the web again.')
            until(engine,lambda:store.snapshot()['sessions'][key]['turns'][-1]['status'] in ('complete','failed'),370)
            turn=store.snapshot()['sessions'][key]['turns'][-1];assert turn['status']=='complete',turn
            assert read(root/'native.json')['token']==token
            assert 'FILE_ONLY_NONCE_CODER_6993' in turn['answer']
            result['same_conversation']=True
        finally:engine.close()
    # A real parent must read both installed skills and use the coder result.
    class Parent(ResearchManager):
        def _build_cli_job_launch_spec(self,name,session_id,decision,claimed):
            spec=super()._build_cli_job_launch_spec(name,session_id,decision,claimed)
            prompt=('This is a bounded integration test, not a research assignment. Do not register, publish, run GPU work or start a new helper task. '
                    +reminder(config,self.worker(name),run/name)+'\n'
                    'Read both installed SKILL.md files using file tools. Then run ./coder read native. Coder already edited project directly; do not copy or apply. '
                    'Run python3 -m unittest discover -s project -v. Confirm that it passed and write the exact reference nonce to integration.txt. '
                    'Finish after these checks. Do not edit the implementation directly.')
            if args.parent_only_from:prompt=prompt.replace('Coder already edited project directly; do not copy or apply.','Run ./coder diff native and ./coder apply native for this saved legacy result.')
            return replace(spec,prompt=prompt)
    manager=Parent(run,config,env,probe=True)
    try:
        manager.states[worker]['route_offset']=args.parent_route
        manager.launch_cli_job(worker);deadline=time.monotonic()+240
        while manager.active_sessions and time.monotonic()<deadline:manager.poll_cli_jobs();time.sleep(.25)
        assert not manager.active_sessions,'Parent test timed out.'
        assert manager.states[worker]['status']=='probe_ok',manager.states[worker]['status']
        assert 'FILE_ONLY_NONCE_CODER_6993' in (run/worker/'integration.txt').read_text()
        output=subprocess.run([sys.executable,'-m','unittest','discover','-s','project','-v'],cwd=run/worker,capture_output=True,text=True,timeout=30)
        assert output.returncode==0,output.stderr
        assert (run/worker/'project/test_stats.py').exists()
        transcript=run/worker/'.runtime/launch_000001/transcript.jsonl'
        result.update(parent_read_and_tested=True,parent_transcript=str(transcript))
    finally:manager.close()
    save(run/'result.json',result);print(json.dumps(result,indent=2))

if __name__=='__main__':main()

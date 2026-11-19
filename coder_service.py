"""Coder launch adapter; retries, persistence and notices reuse council's engine."""
import json
import os
from pathlib import Path
import sys
import time
import signal

from common import ROOT, alive, read, save, lock, pid_token
from recovery import ResearchManager
from coder import Store, LocalStore
from coder_workspace import prepare, SnapshotError, target_path
from council_service import CouncilJob, Engine as HelperEngine, engine, watch
from council import TERMINAL
from messages import Messages
from station.workers.cli import CodexCliWorkerBackend
from station.workers.job_manager import CliJobLaunchSpec
from subagents import reasoning_effort


class CoderJob(CouncilJob):
    # These trusted remote hosts prohibit unprivileged sandbox UID maps.
    # Use the established researcher launch mode; edits live in a task snapshot.
    _prepare_cli_job_launch = staticmethod(ResearchManager._prepare_cli_job_launch)

    def launch_cli_job(self, job_id):
        # Older engines could consume the attempt budget on setup exceptions
        # without ever starting a CLI. Finish those retained tasks on recovery.
        if not self.active_sessions and self.state['attempts'] >= self.policy['max_attempts']:
            self.fail('retry_limit_exhausted')
            return False
        try:
            return super().launch_cli_job(job_id)
        except (OSError, ValueError) as exc:
            # A bad task must not restart the engine and disrupt other coders.
            # Only our fixed snapshot messages are safe for the public result;
            # launch/configuration exceptions can contain private route details.
            reason = str(exc) if isinstance(exc, SnapshotError) else (
                'Coder launch setup failed (' + type(exc).__name__ +
                '); ask the human operator to inspect private configuration.')
            self.stop()
            self.fail(reason)
            return False

    def _get_cli_job_backend(self, backend):
        reader=CodexCliWorkerBackend();native=reader.extract_resume_token
        reader.extract_resume_token=lambda path:native(path) or self.state.get('token')
        return reader

    def _build_cli_job_launch_spec(self,_,session_id,decision,claimed):
        parent=self.store.run/self.session['worker']
        direct=self.session.get('workspace_mode')=='direct'
        if direct:
            first=self.session['turns'][0]['prompt'].splitlines()[0]
            path=first.removeprefix('Code directory: ') if first.startswith('Code directory: ') else '.'
            work=target_path(parent,path)
        else:
            snapshot=prepare(self.root,parent,self.session['turns'][0]['prompt'])
            work=self.root/'workspace'
        index=((self.state['attempts']-1)//2)%len(self.route_list)
        route=self.route_list[index];self.state['route_index']=index;self.persist()
        for part in ('.home','.codex','.cache','.tmp'):(self.root/part).mkdir(exist_ok=True)
        env={k:v for k,v in self.env.items() if k in ('PATH','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR','NODE_EXTRA_CA_CERTS')}
        env.update(HOME=str(self.root/'.home'),CODEX_HOME=str(self.root/'.codex'),
                   XDG_CACHE_HOME=str(self.root/'.cache'),TMPDIR=str(self.root/'.tmp'),PATH=self.bin_path,
                   CUDA_VISIBLE_DEVICES='',OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2',
                   CODEX_API_KEY=route['api_key'],CODEX_BASE_URL=route['base_url'])
        if direct:
            state=read(parent/'.runtime/state.json',{})
            inherited={}
            if alive(state.get('pid'),state.get('pid_token')):
                try:
                    data=Path('/proc',str(state['pid']),'environ').read_bytes()
                    inherited=dict(part.decode().split('=',1) for part in data.split(b'\0') if b'=' in part)
                except (OSError,UnicodeError):pass
            # Reuse the research runtime, keeping helper model credentials and
            # native conversation storage separate from the lead's CLI.
            for key in ('PATH','VIRTUAL_ENV','CONDA_PREFIX','CONDA_DEFAULT_ENV','LD_LIBRARY_PATH','PYTHONPATH',
                        'PKG_CONFIG_PATH','CMAKE_PREFIX_PATH','LANG','LC_ALL'):
                if inherited.get(key):env[key]=inherited[key]
            env.update(HOME=str(parent/'.home'),XDG_CONFIG_HOME=str(parent/'.home/.config'),
                       XDG_CACHE_HOME=str(parent/'.cache'),TMPDIR=str(parent/'.tmp'))
            if not inherited.get('VIRTUAL_ENV'):env['VIRTUAL_ENV']=str(parent/'.venv')
            env['PATH']=str(parent/'.venv/bin')+':'+env.get('PATH',self.bin_path)
            for part in ('.home','.cache','.tmp'):(parent/part).mkdir(exist_ok=True)
        # Use the same dedicated model route and optional research proxy as the parent.
        from urllib.parse import urlsplit
        from proxy import Router
        model_proxy=self.config.get('model_api_proxy')
        if model_proxy=='direct':model_proxy=None
        host_proxies=self.config.get('model_api_host_proxies') or {}
        if model_proxy or self.config.get('research_proxy') or host_proxies:
            if not getattr(self,'router',None):
                self.router=Router({urlsplit(r['base_url']).hostname for r in self.route_list},
                                   self.config.get('research_proxy'),model_proxy,[],host_proxies=host_proxies).__enter__()
            env.update({k:self.router.url for k in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy')})
        env.update(NO_PROXY='localhost,127.0.0.1,::1',no_proxy='localhost,127.0.0.1,::1')
        role=(ROOT/'prompts/coder.md').read_text()
        scope='External web access is available.' if not self.config.get('disable_web') else 'This is an isolated run: no external web access or downloads.'
        context=(f"Researcher workspace: {parent.resolve()}. Working directory: {work}. Edit the research files directly.\n" if direct else
                 f"This existing task uses a saved snapshot at {work}; edit that snapshot and keep original files at {snapshot['source']} read-only. The researcher applies this older task's changes.\n")+scope+'\n'
        prompt=(role+'\n'+context if not decision.is_resume else context)+self.turn['prompt']
        if self.state['attempts']>1:prompt='Continue the interrupted coding task in this saved conversation.\n'+prompt
        return CliJobLaunchSpec(self.binary,str(self.root/session_id),'codex',self.session['model'],str(parent.resolve()) if direct else str(work),str(work),prompt,env,
            network_access=not self.config.get('disable_web',False),codex_config_overrides=(
                'features.enable_request_compression=false','agents.enabled=false','features.multi_agent=false',
                'features.apps=false','mcp_servers={}',
                'model_reasoning_effort='+json.dumps(reasoning_effort('codex',self.session['model']))))

    def stop(self,*,cancelled=False):
        try:super().stop(cancelled=cancelled)
        finally:
            if getattr(self,'router',None):self.router.__exit__(None,None,None);self.router=None


class Engine(HelperEngine):
    store_type=Store
    job_type=CoderJob
    engine_metadata={'direct_workspace':True,'independent_tasks':True}


def task_runner(run,worker,name):
    store=LocalStore(run,worker);key=store.key(worker,name)
    root=store.root/worker/name
    handle=lock(root/'runner.lock')
    with handle:
        save(root/'runner.json',dict(pid=os.getpid(),token=pid_token(os.getpid())))
        stopping=False;job=None
        def halt(*_):
            nonlocal stopping
            stopping=True
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,halt)
        try:
            while not stopping and not (run/'STOP').exists() and not (run/worker/'.runtime/PAUSE').exists():
                session=store.snapshot()['sessions'][key]
                turn=next((t for t in session['turns'] if t['status'] not in TERMINAL),None)
                if not turn:
                    # Submission and process exit share this short lock, so a
                    # follow-up cannot be stranded behind an exiting process.
                    with lock(root/'start.lock'):
                        current=store.snapshot()['sessions'][key]
                        if any(t['status'] not in TERMINAL for t in current['turns']):continue
                        save(root/'runner.json',dict(pid=None,token=None))
                        handle.close()
                        return
                if job is None:
                    try:job=CoderJob(store,key,session,turn,read(run/'run.json'),dict(os.environ))
                    except (OSError,ValueError) as exc:
                        store.update_turn(key,turn['number'],status='failed',error='Coder launch setup failed ('+type(exc).__name__+').',finished=time.time())
                        Messages(run/worker).add(f'Coder {name}: failed. Read with ./coder read {name}.',source='Research coder')
                        return
                policy=store.policy();job.policy=policy
                if turn.get('cancel_requested'):job.stop(cancelled=True)
                elif time.time()-job.state['started']>policy['turn_seconds']:
                    job.stop();job.fail('time_limit_exhausted')
                else:
                    if job.active_sessions and time.time()-job.state.get('launch_started',time.time())>policy['attempt_seconds']:
                        for native in list(job.active_sessions.values()):job._abort_cli_job_process(native)
                    job.poll_cli_jobs()
                    if not job.active_sessions and job.state['status'] not in TERMINAL:job.launch_cli_job('turn')
                if job.state['status'] in TERMINAL:
                    job.stop()
                    status=job.state['status'];number=turn['number']
                    Messages(run/worker).add(f'Coder {name}: {status}. Read with ./coder read {name}.',
                        source='Research coder',delivery_key=f"coder-local:{key}:{number}:{turn.get('retry_generation',0)}:{status}")
                    store.update_turn(key,number,notified=True);job=None
                    if status!='complete':return
                time.sleep(.2)
        finally:
            if job is not None:job.stop()
            with lock(root/'start.lock'):
                if read(root/'runner.json',{}).get('pid')==os.getpid():
                    save(root/'runner.json',dict(pid=None,token=None))


if __name__=='__main__':
    os.umask(0o077)
    if sys.argv[1]=='task':task_runner(Path(sys.argv[2]),sys.argv[3],sys.argv[4])
    elif read(Path(sys.argv[2])/'.coder/policy.json',{}).get('execution')=='local':pass
    elif sys.argv[1]=='engine':engine(Path(sys.argv[2]),Engine)
    else:watch(Path(sys.argv[2]),service_file=__file__,store_type=Store)

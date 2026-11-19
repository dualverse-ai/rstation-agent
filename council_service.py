"""Bounded council turns on Station's native CLI lifecycle, outside research processes."""
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

from common import ROOT, alive, lock, pid_token, read, save
from council import Store, TERMINAL, directory
from recovery import BACKOFF, ClaudeResume, ResearchManager, executable, routes
from antigravity import AntigravityResume, prepare_profile
from station.workers.cli import CodexCliWorkerBackend, PreparedCliWorkerLaunch
from station.workers.job_manager import CliJobManager, CliJobState, CliJobLaunchSpec
from subagents import reasoning_effort
from messages import Messages

MISSING = ('no conversation found', 'session not found', 'no session found', 'conversation not found',
           'failed to load session', 'could not find session', 'no rollout found for thread',
           'selected model cannot continue this conversation')


class CouncilClaude(ClaudeResume):
    def prepare_launch(self, *, executable, run_dir, model_name, prompt, **kwargs):
        return PreparedCliWorkerLaunch('claude',
            [executable, '--print', '--verbose', '--output-format', 'stream-json',
             '--model', model_name, '--permission-mode', 'default', '--tools', 'Read,Glob,Grep',
             '--allowedTools', 'Read', 'Glob', 'Grep', '--bare',
             '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
             '--effort', reasoning_effort('claude',model_name)], prompt,
            str(Path(run_dir)/'transcript.jsonl'),str(Path(run_dir)/'stderr.txt'),None,'jsonl',{})


class CouncilGemini(AntigravityResume):
    def prepare_launch(self, **kwargs):
        launch=super().prepare_launch(**kwargs)
        # The adviser profile exposes only read tools; allow those without a
        # human prompt, including absolute paths in the researcher's workspace.
        command=list(launch.command)
        command[command.index('--agent')+1]='rstation-council'
        return replace(launch,command=command)


class CouncilCodex(CodexCliWorkerBackend):
    def prepare_launch(self, **kwargs):
        return self.restrict(super().prepare_launch(**kwargs))

    def prepare_resume_launch(self, **kwargs):
        return self.restrict(super().prepare_resume_launch(**kwargs))

    @staticmethod
    def restrict(launch):
        command=list(launch.command)
        command[command.index('--sandbox')+1]='read-only'
        return replace(launch,command=command)


def response_text(path):
    """Keep the latest response, including text saved before an interrupted result."""
    answer = ''
    gemini_step = None
    for event in CliJobManager._read_transcript_events(str(path)):
        step = event.get('step_update', {})
        if step.get('step_type') == 'user_input':
            answer, gemini_step = '', None
        elif step.get('step_type') == 'agent_response':
            if step.get('step_index') != gemini_step:
                answer, gemini_step = '', step.get('step_index')
            answer += step.get('text_delta', '')
        elif event.get('type') == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
            answer = event['item'].get('text', '')
        elif event.get('type') == 'assistant':
            text = ''.join(part.get('text', '') for part in event.get('message', {}).get('content', [])
                           if part.get('type') == 'text')
            if text: answer = text
        elif event.get('type') == 'result' and not event.get('is_error'):
            if isinstance(event.get('result'), str) and event['result']: answer = event['result']
        elif event.get('event') == 'result':
            text = event.get('result', {}).get('response')
            if isinstance(text, str) and text: answer = text
    return answer if isinstance(answer, str) else ''


def assemble_responses(paths):
    """Join recovery fragments in order, removing exact replays and overlaps."""
    answer = ''
    for path in paths:
        text = response_text(path).strip()
        if not text or text in answer: continue
        if not answer or answer in text:
            answer = text
            continue
        # Linear-time suffix/prefix overlap, including a cut in a word or URL.
        pattern = text + '\0' + answer[-len(text):]
        prefix = [0] * len(pattern)
        for index in range(1, len(pattern)):
            length = prefix[index-1]
            while length and pattern[index] != pattern[length]: length = prefix[length-1]
            if pattern[index] == pattern[length]: length += 1
            prefix[index] = length
        overlap = prefix[-1]
        answer += text[overlap:] if overlap >= 16 else '\n\n---\n\n' + text
    return answer


def research_roots(run, config, worker):
    """Only named researchers in this run, never its private controller tree."""
    run = Path(run).resolve()
    names = [worker] + [w['name'] for w in config.get('workers', []) if w['name'] != worker]
    roots = []
    for name in names:
        path = run / name
        if path.parent != run or path.resolve().parent != run:
            raise ValueError('Researcher workspace must be a direct directory of this group.')
        roots.append(path.resolve())
    return roots


class CouncilJob(CliJobManager):
    _resume_backoff_schedule=staticmethod(lambda: BACKOFF)

    def __init__(self, store, key, session, turn, config, env):
        super().__init__()
        self.store,self.key,self.session,self.turn,self.config=store,key,session,turn,config
        self.env=dict(env);self.policy=store.policy();self.kind=session['family']
        self.root=store.root/session['worker']/session['name']
        self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'native.json'
        self.state=read(self.path,{})
        self.route_list=routes(self.kind,env)
        self.binary,self.bin_path=executable(self.kind,env)
        if self.state:
            self.capture_token()
            # Fence a process orphaned by an engine crash before claiming its turn.
            if alive(self.state.get('pid'),self.state.get('pid_token')):
                from gpu import stop_group
                stop_group(self.state['pid'])
            self.state['pid']=None
        # Set the legacy boundary before an explicit retry resets its attempt count.
        if self.state:
            self.state.setdefault('answer_start', max(1, self.state.get('launches',0)-self.state.get('attempts',0)+1))
        identity=(turn['number'],turn.get('retry_generation',0))
        if (self.state.get('turn'),self.state.get('generation',0)) != identity:
            recovering=self.state.get('turn')==identity[0]
            if not recovering: self.state['answer_start'] = self.state.get('launches', 0) + 1
            self.state.update(turn=identity[0],generation=identity[1],attempts=0,spawn=int(bool(self.state.get('token'))),
                              resumes=0,next=0,status='queued',started=time.time(),recovering=recovering)
        self.state.setdefault('launches',0)
        # Upgrade existing in-flight sessions without including an earlier question.
        self.state.setdefault('answer_start', max(1, self.state['launches']-self.state['attempts']+1))
        self.state.setdefault('token',None)
        self._endpoint_resume_counts['turn']=self.state.get('attempts',0)
        self.persist()
        if self.state.get('status')=='running' and self.current_complete():
            self.complete()
        elif self.state.get('status') in {'running','starting'}:
            self.state['status']='queued';self.persist()

    def persist(self):save(self.path,self.state)

    def transcript(self):return self.root/f"attempt-{self.state.get('launches',0):06d}"/'transcript.jsonl'

    def _get_cli_job_backend(self, backend):
        reader={'codex':CouncilCodex,'claude':CouncilClaude,'gemini':CouncilGemini}[backend]()
        native=reader.extract_resume_token
        reader.extract_resume_token=lambda path:native(path) or self.state.get('token')
        return reader

    def capture_token(self):
        token=self._get_cli_job_backend(self.kind).extract_resume_token(str(self.transcript()))
        if token:
            if self.state.get('token') and self.state['token']!=token:
                raise ValueError('Council native session changed unexpectedly; saved state retained.')
            self.state['token']=token;self.persist()

    def _before_cli_job_poll(self):self.capture_token()
    def _check_cli_job_transcript_idle_timeouts(self):pass  # A whole-turn wall-clock budget is enforced by the engine.

    def launch_cli_job(self, job_id):
        try:
            return super().launch_cli_job(job_id)
        except (OSError, ValueError) as exc:
            if exc.__class__.__name__ == 'SnapshotError':
                raise
            # Invalid migrated workspace/configuration must fail one advice
            # turn, not kill the helper engine or retry a paid request forever.
            reason = ('Council launch setup failed (' + type(exc).__name__ +
                      '); ask the human operator to inspect private configuration.')
            self.stop()
            self.fail(reason)
            return False

    def _load_cli_job_state(self, _):
        s=self.state
        ready=s['status'] not in TERMINAL and s['attempts']<self.policy['max_attempts']
        return CliJobState(self.kind,s['spawn'],s['resumes'],self.policy['max_attempts'],self.policy['max_attempts'],
            s.get('token'),s.get('next',0),ready and not s.get('token') and time.time()>=s.get('next',0),ready and bool(s.get('token')))

    def _format_cli_job_session_id(self,*_):return f"attempt-{self.state['launches']+1:06d}"

    def _claim_cli_job_launch(self,_,session_id,decision):
        self.state.update(spawn=decision.spawn_count,resumes=decision.resume_count,
            launches=self.state['launches']+1,attempts=self.state['attempts']+1,status='starting')
        self.persist()
        save(self.root/session_id/'turn.json', dict(number=self.turn['number'], generation=self.state.get('generation',0)))
        self.store.update_turn(self.key,self.turn['number'],status='running',started=self.state['started'])
        return True

    def _build_cli_job_launch_spec(self,_,session_id,decision,claimed):
        # Start every new turn on primary; failures rotate after two attempts.
        # This prevents a successful backup becoming permanently sticky.
        index=((self.state['attempts']-1)//2)%len(self.route_list)
        route=self.route_list[index]
        self.state['route_index']=index;self.persist()
        work=self.root/'workspace';work.mkdir(exist_ok=True)
        for part in ('.home','.codex','.claude','.cache','.tmp'):(self.root/part).mkdir(exist_ok=True)
        env={k:v for k,v in self.env.items() if k in {'PATH','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR','NODE_EXTRA_CA_CERTS'}}
        env.update(HOME=str(self.root/'.home'),CODEX_HOME=str(self.root/'.codex'),CLAUDE_CONFIG_DIR=str(self.root/'.claude'),
            XDG_CONFIG_HOME=str(self.root/'.home/.config'),XDG_CACHE_HOME=str(self.root/'.cache'),TMPDIR=str(self.root/'.tmp'),
            PATH=self.bin_path,CUDA_VISIBLE_DEVICES='',CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',PYTHONUNBUFFERED='1')
        proxy=self.config.get('model_api_proxy')
        if proxy and proxy!='direct':env.update(HTTP_PROXY=proxy,HTTPS_PROXY=proxy,http_proxy=proxy,https_proxy=proxy)
        env.update(NO_PROXY='localhost,127.0.0.1,::1',no_proxy='localhost,127.0.0.1,::1')
        if self.kind=='codex':env.update(CODEX_API_KEY=route['api_key'],CODEX_BASE_URL=route['base_url'])
        elif self.kind=='claude':env.update(ANTHROPIC_API_KEY=route['api_key'],ANTHROPIC_BASE_URL=route['base_url'])
        else:
            env.update(GEMINI_API_KEY=route['api_key'],GOOGLE_GEMINI_BASE_URL=route['base_url'])
            prepare_profile(self.root,True,self.session['model'],self.config.get('gemini_context_tokens',500000),adviser=True)
        role=(ROOT/'prompts/council_adviser.md').read_text()
        roots=research_roots(self.store.run,self.config,self.session['worker'])
        research=roots[0]
        scope=('This is an isolated research run. Read the supplied task and the listed researcher workspaces in this group only; no external retrieval or previous runs.'
               if self.config.get('disable_web') else 'Read the supplied task and relevant local research files. Ask the researcher for external sources when needed; web retrieval is unavailable.')
        access=('Researcher workspace: '+str(research)+'. You have read-only file tools. Resolve relative paths in the question against this workspace. '
                'Read, list and search relevant files directly; no attachments or further permission are needed. '
                'You may also read peer researcher workspaces in this same group: '+', '.join(map(str,roots[1:]))+'. '
                'Keep inspection within these listed workspaces. Do not read hidden files or directories, credentials, private CLI configuration or other runs.\n\n')
        prefix=role+'\n\n'+scope+'\n\n'+access+'Assigned problem: '+self.config['problem']+'\n'+self.policy['statement']+'\n\n'
        if self.state['attempts']==1 and not self.state.get('recovering'):
            prompt=(prefix if not decision.is_resume else access)+self.turn['prompt']
        else:
            prompt=(prefix if not decision.is_resume else access)+(
                'Continue the interrupted consultation. Check your conversation before repeating work. The pending question is:\n'+self.turn['prompt'])
        mcp=('mcp_servers={research_files={command='+json.dumps(sys.executable)+',args='+
             json.dumps([str(ROOT/'council_files.py'),*map(str,roots)])+
             ',required=true,default_tools_approval_mode="approve"}}')
        overrides=('features.enable_request_compression=false','project_doc_max_bytes=0','features.shell_tool=false',
            'features.unified_exec=false','features.multi_agent=false','agents.enabled=false','features.apps=false',
            'features.browser_use=false','features.computer_use=false',mcp,
            'web_search="disabled"','model_reasoning_effort='+json.dumps(reasoning_effort('codex',self.session['model'])))
        return CliJobLaunchSpec(self.binary,str(self.root/session_id),self.kind,self.session['model'],str(work),str(work),prompt,env,
                                network_access=False,codex_config_overrides=overrides)

    def _mark_cli_job_pid(self,_,pid):
        self.state.update(pid=pid,pid_token=pid_token(pid),status='running',launch_started=time.time());self.persist()

    def current_complete(self):
        return ResearchManager.completed(SimpleNamespace(transcript_path=str(self.transcript()))) and bool(response_text(self.transcript()).strip())

    def _cli_job_completion_ready(self,_,session):
        return self.current_complete()

    def complete(self):
        self.capture_token()
        answer=assemble_responses(self.root/f'attempt-{number:06d}'/'transcript.jsonl'
                                  for number in range(self.state['answer_start'],self.state['launches']+1))
        self.store.update_turn(self.key,self.turn['number'],status='complete',answer=answer,finished=time.time(),
            provider=self.state.get('route_index',0)+1,attempts=self.state['attempts'])
        self.state.update(status='complete',pid=None);self.persist()

    def _on_cli_job_completed(self,*_):self.complete()

    def fail(self,reason):
        self.state.update(status='failed',pid=None,error=reason);self.persist()
        self.store.update_turn(self.key,self.turn['number'],status='failed',error=reason,finished=time.time())

    def _classify_cli_job_failure(self,job,session):
        failure=super()._classify_cli_job_failure(job,session)
        paths=[Path(session.stderr_path),Path(session.transcript_path)]
        detail='\n'.join(p.read_text(errors='replace')[-32000:] if p.exists() else '' for p in paths).lower()
        if any(marker in detail for marker in MISSING):failure.category,failure.is_retryable_infra='session_unavailable',False
        return failure

    def _schedule_cli_job_resume(self,_,session,failure):
        if self.state['attempts']>=self.policy['max_attempts']:return self.fail('retry_limit_exhausted')
        self.state.update(token=failure.resume_token or self.state.get('token'),status='retrying',pid=None,
            next=time.time()+self.get_resume_backoff_delay_seconds(self.state['attempts']-1))
        self.persist()

    def _schedule_cli_job_fresh_attempt(self,job,session,failure):
        if self.state.get('token') or failure.category=='session_unavailable':
            return self._on_cli_job_attempts_exhausted(job,session,failure)
        return self._schedule_cli_job_resume(job,session,failure)

    def _on_cli_job_attempts_exhausted(self,_,session,failure):
        self.fail('session_unavailable' if failure.category=='session_unavailable' else 'retry_limit_exhausted')

    def stop(self, *, cancelled=False):
        for session in list(self.active_sessions.values()):
            self._abort_cli_job_process(session)
            self._close_cli_job_session_handles(session)
        self.active_sessions.clear();self.capture_token();self.state['pid']=None
        if cancelled:
            self.state['status']='cancelled'
            self.store.update_turn(self.key,self.turn['number'],status='cancelled',finished=time.time())
        self.persist()


class Engine:
    store_type=Store
    job_type=CouncilJob
    def __init__(self,run,env):
        self.store=self.store_type(run);self.config=read(Path(run)/'run.json');self.env=env;self.jobs={}
        if self.config.get('role')=='supervisor':raise ValueError('Supervisors do not use council.')

    def tick(self):
        if (self.store.run/'STOP').exists():
            self.close();return
        policy=self.store.policy()
        snapshot=self.store.snapshot()
        for key,job in list(self.jobs.items()):
            turn=snapshot['sessions'][key]['turns'][job.turn['number']-1]
            if (self.store.run/job.session['worker']/'.runtime/PAUSE').exists():
                job.stop();del self.jobs[key];continue
            if job.session['worker'] not in policy.get('enabled',[]) and turn['status']=='queued' and not job.active_sessions:
                del self.jobs[key]
                continue
            if turn.get('cancel_requested'):
                job.stop(cancelled=True)
            elif time.time()-job.state['started']>policy['turn_seconds']:
                job.stop();job.fail('time_limit_exhausted')
            else:
                if job.active_sessions and time.time()-job.state.get('launch_started',time.time())>policy['attempt_seconds']:
                    # A stalled primary must reach backup before the whole-turn
                    # deadline. Native retries alone can otherwise wait forever.
                    for native in list(job.active_sessions.values()):job._abort_cli_job_process(native)
                job.poll_cli_jobs()
                if not job.active_sessions and job.state['status'] not in TERMINAL:job.launch_cli_job('turn')
            if job.state['status'] in TERMINAL:
                job.stop();del self.jobs[key]
        snapshot=self.store.snapshot()
        for key,session in snapshot['sessions'].items():
            if key in self.jobs or (self.store.run/session['worker']/'.runtime/PAUSE').exists():continue
            for turn in session['turns']:
                if turn.get('cancel_requested') and turn['status'] not in TERMINAL:
                    self.store.update_turn(key,turn['number'],status='cancelled',finished=time.time())
                    continue
                if turn['status']=='failed':break
                if turn['status'] in TERMINAL:continue
                if session['worker'] not in policy.get('enabled',[]) and turn['status']!='running':break
                count=sum(j.session['worker']==session['worker'] for j in self.jobs.values())
                if self.store.capability=='council' and count>=policy['max_concurrent']:break
                try:
                    job=self.job_type(self.store,key,session,turn,self.config,self.env)
                    if job.state['status'] not in TERMINAL:self.jobs[key]=job
                except (OSError,ValueError) as exc:
                    self.store.update_turn(key,turn['number'],status='failed',error=self.store.capability.title()+' setup failed ('+type(exc).__name__+'); ask the human operator to inspect private configuration.')
                break
        for key,session in self.store.snapshot()['sessions'].items():
            for turn in session['turns']:
                if turn['status'] in TERMINAL and not turn.get('notified'):
                    Messages(self.store.run/session['worker']).add(
                        f"{self.store.capability.title()} {session['name']}: {turn['status']}. Read with ./{self.store.capability} read {session['name']}.",
                        source='Research '+self.store.capability,delivery_key=f"{self.store.capability}:{key}:{turn['number']}:{turn.get('retry_generation',0)}:{turn['status']}")
                    self.store.update_turn(key,turn['number'],notified=True)

    def close(self):
        for job in self.jobs.values():job.stop()
        self.jobs.clear()


def engine(run, engine_type=Engine):
    root=engine_type.store_type(run).root
    with lock(root/'engine.lock',True):
        manager=engine_type(run,dict(os.environ));stopping=False
        save(root/'engine.json',dict(pid=os.getpid(),token=pid_token(os.getpid()),
                                   **getattr(manager,'engine_metadata',{})))
        def halt(*_):
            nonlocal stopping
            stopping=True
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,halt)
        try:
            while not stopping and not (Path(run)/'STOP').exists():
                manager.tick()
                if not manager.store.policy().get('enabled') and not manager.jobs:return
                time.sleep(.25)
        finally:
            manager.close()
            save(root/'engine.json',dict(pid=None,token=None))


def watch(run, *, service_file=__file__, store_type=Store):
    root=store_type(run).root
    with lock(root/'watch.lock',True):
        stopping=False;child=None
        def halt(*_):
            nonlocal stopping
            stopping=True
            if child and child.poll() is None:child.terminate()
        for sig in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):signal.signal(sig,halt)
        save(root/'controller.json',dict(pid=os.getpid(),token=pid_token(os.getpid()),status='running'))
        try:
            while not stopping and not (Path(run)/'STOP').exists():
                child=subprocess.Popen([sys.executable,service_file,'engine',str(run)],env=dict(os.environ))
                code=child.wait()
                if stopping or (Path(run)/'STOP').exists() or (code==0 and not store_type(run).policy().get('enabled')):break
                # A native launch/setup failure cannot cause a rapid paid retry loop.
                time.sleep(5)
        finally:
            if child and child.poll() is None:child.terminate();child.wait(timeout=15)
            save(root/'controller.json',dict(pid=None,token=None,status='stopped'))


if __name__=='__main__':
    os.umask(0o077)
    {'watch':watch,'engine':engine}[sys.argv[1]](Path(sys.argv[2]))

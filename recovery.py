"""Continuous research adapter around Station's shared CLI lifecycle."""
from dataclasses import replace
import json
import os
import re
from pathlib import Path
import signal
import time
from urllib.parse import urlsplit
import shutil
import tempfile
import sys
from antigravity import AntigravityResume, prepare_profile, completed_turn, provider_account_error

from common import ROOT, read, save, initial_prompt, pid_token, alive, workspace_bytes, url
from proxy import Router
from subagents import reasoning_effort, codex_delegation_config, assistant_instructions
from station import constants, runtime_api_config
from station.workers.cli import ClaudeCliWorkerBackend, detect_cli_worker_executable
from station.workers.job_manager import CliJobManager, CliJobState, CliJobLaunchSpec, CliJobFailure

BACKOFF = [10,20,40,60,120,300,300,300,300,300,300,300,300,300]
PRIMARY_RECOVERY_SECONDS = runtime_api_config.PROVIDER_BASE_RECOVERY_CHECK_INTERVAL_SECONDS
PRIMARY_PROBE_SECONDS = 30
SESSION_RECOVERY_FAILURES = 6
SESSION_RECOVERY_IDLE_SECONDS = 1800
SESSION_RECOVERY_COOLDOWN_SECONDS = 6 * 3600

def provider_unavailable(detail):
    """Only inspect native error fields, never ordinary research text."""
    return bool(re.search(
        r'(?:\bHTTP(?:/\d(?:\.\d)?)?\s+|\bAPI Error\s*:\s*|'
        r'\b(?:error_status|status_code|status)\b[\s\"\':=]+)(?:429|5\d\d)\b'
        r'|\b(?:overloaded_error|rate_limit_error)\b', str(detail), re.IGNORECASE))


def claude_content(event):
    """Claude system notices can have a plain-string message, not chat blocks."""
    if event.get("type") not in {"assistant", "user"}:return []
    message=event.get("message")
    if not isinstance(message,dict):return []
    content=message.get("content")
    if isinstance(content,str):return [{"type":"text","text":content}]
    return [part for part in content if isinstance(part,dict)] if isinstance(content,list) else []

def routes(kind, env):
    from common import worker_environment
    env = worker_environment(env)
    # Pass only this role's dedicated settings to Station's route-list validator.
    role = {'codex': 'CODEX', 'claude': 'CLAUDE', 'gemini': 'GEMINI'}[kind]
    prefix = f'RSTATION_WORKER_{role}_'
    mapped = {}
    for backup in ('', 'BACKUP_'):
        for field in ('API_KEY', 'BASE_URL', 'HTTP_PROXY', 'HTTPS_PROXY'):
            mapped[backup + 'CODEX_' + field] = env.get(prefix + backup + field, '')
    if not mapped['CODEX_API_KEY'].strip() or not mapped['CODEX_BASE_URL'].strip():
        raise ValueError(f'{role}_API_KEY and {role}_BASE_URL must both be configured.')
    if mapped.get('BACKUP_CODEX_API_KEY'):
        keys = mapped['BACKUP_CODEX_API_KEY'].split(';')
        raw = mapped.get('BACKUP_CODEX_BASE_URL')
        values = raw.split(';') if raw else [''] * len(keys)
        mapped['BACKUP_CODEX_BASE_URL'] = ';'.join(v.strip() or mapped['CODEX_BASE_URL'] for v in values)
    try:
        result = runtime_api_config.build_cli_endpoints('codex', mapped)
    except ValueError as exc:
        message = str(exc).replace('CODEX_', prefix).replace('BACKUP_' + prefix, prefix + 'BACKUP_')
        raise ValueError(message) from None
    for route in result:
        if not route["api_key"] or not route["base_url"]:
            raise ValueError(f"Missing {kind} API credentials or endpoint. Configure the provider key and endpoint in .env.")
        from common import url
        url(route["base_url"],f"{kind} API endpoint")
        for field in ("http_proxy","https_proxy"):
            if route.get(field): url(route[field],field)
    return result

def executable(kind, env):
    env=dict(env)
    parts=[str(Path.home()/".local/bin"),str(Path.home()/".local/claude-cli/node_modules/.bin")]
    parts.extend(str(p) for p in (Path.home()/".nvm/versions/node").glob("*/bin"))
    env["PATH"]=":".join(parts+[env.get("PATH","")])
    if kind == "gemini":
        binary = env.get("ANTIGRAVITY_BIN_PATH") or shutil.which("agy", path=env["PATH"])
        if not binary or not os.access(binary, os.X_OK):
            raise FileNotFoundError("Antigravity CLI not found. Install agy or set ANTIGRAVITY_BIN_PATH.")
        return binary, env["PATH"]
    return detect_cli_worker_executable(kind,env),env["PATH"]

class ClaudeResume(ClaudeCliWorkerBackend):
    supports_resume=True
    def __init__(self, disable_web=False, subagents=0):
        self.disable_web=disable_web
        self.subagents=subagents
    def prepare_launch(self, **kwargs):
        result=super().prepare_launch(**kwargs)
        command=list(result.command)
        disallowed=["WebSearch","WebFetch"] if self.disable_web else []
        if not self.subagents:disallowed += ["Agent","Task"]
        if disallowed:command += ["--disallowedTools",*disallowed]
        model=kwargs.get("model_name") or ""
        effort=reasoning_effort("claude",model)
        if self.subagents:
            tools=["Agent","Bash","Edit","Read","Write","Glob","Grep","SendMessage","TaskStop"]
            if not self.disable_web:tools += ["WebSearch","WebFetch"]
            command += ["--safe-mode","--tools",",".join(tools),
                        "--append-subagent-system-prompt",assistant_instructions(self.disable_web),
                        "--forward-subagent-text"]
        # Private homes and no inherited project plugins/config.
        if not self.subagents:command += ["--bare"]
        command += ["--effort",effort,"--setting-sources","","--strict-mcp-config","--mcp-config",'{"mcpServers":{}}']
        return replace(result,command=command,env_overrides={})
    def prepare_resume_launch(self, *, resume_token, **kwargs):
        result=self.prepare_launch(**kwargs)
        return replace(result,command=result.command+["--resume",resume_token])
    def extract_resume_token(self,path):
        try:
            with open(path) as f:
                for line in f:
                    try: value=json.loads(line)
                    except ValueError: continue
                    if isinstance(value,dict) and value.get("session_id"): return value["session_id"]
        except FileNotFoundError: pass
        return None

from worker_control import serialize_failure

class ResearchManager(CliJobManager):
    fresh_attempt_after_resume_exhaustion=True
    resume_reset_requires_tool_progress=True
    def __init__(self, run, config, env, probe=False):
        super().__init__()
        self.run,self.config,self.env=Path(run),config,dict(env)
        self.probe=probe
        self.states={};self.routers={};self.log_offsets={};self.pending_tools={}
        from provider_control import initial_routes
        self.route_lists=initial_routes(run,config,env)
        self.bin_paths={kind:executable(kind,env) for kind in self.route_lists}
        constants.CLI_WORKER_TRANSCRIPT_IDLE_TIMEOUT_SECONDS=config.get("idle_seconds",1800)
        for worker in config["workers"]:
            name=worker["name"];path=self.run/name/".runtime/state.json"
            state=read(path,{"spawn":0,"resumes":0,"token":None,"next":0,"launches":0,"route_count":0})
            if state.get("session_unavailable"):
                if worker["kind"] != "gemini":state["token"]=None
            elif state.get("launches"):
                transcript=self.run/name/".runtime"/f"launch_{state['launches']:06d}"/"transcript.jsonl"
                recovered=self._get_cli_job_backend(worker["kind"]).extract_resume_token(str(transcript))
                if recovered:state["token"]=recovered
            self.states[name]=state
            self._endpoint_resume_counts[name]=state.get("route_count",0)
            (self.run/name/".runtime").mkdir(parents=True,exist_ok=True,mode=0o700)
        self.last_storage=0
        if not probe:
            from provider_control import initialize
            initialize(self)

    @staticmethod
    def _resume_backoff_schedule(): return BACKOFF
    def _get_cli_job_backend(self,backend):
        if backend == "gemini":return AntigravityResume()
        return ClaudeResume(self.config.get("disable_web",False),self.config.get("subagents",0)) if backend=="claude" else super()._get_cli_job_backend(backend)
    def worker(self,name):
        from worker_control import effective_worker
        return effective_worker(next(w for w in self.config["workers"] if w["name"]==name), self.states.get(name, {}))
    def provider_pool(self,name):
        from provider_control import pool_for
        return pool_for(self,name)
    def persist(self,name):
        save(self.run/name/".runtime/state.json",self.states[name])
    def log(self,name,text):
        for secret in getattr(self,"_provider_secrets",set()):
            if secret:text=text.replace(secret,"[redacted]")
        # Do not print raw provider errors or request bodies, which can echo keys.
        for route_list in self.route_lists.values():
            for route in route_list:
                if route["api_key"]:text=text.replace(route["api_key"],"[redacted]")
        line=time.strftime("%Y-%m-%d %H:%M:%S UTC",time.gmtime())+" "+text+"\n"
        with (self.run/name/".runtime/console.log").open("a") as f:f.write(line)
    def _load_cli_job_state(self,name):
        from provider_control import refresh
        refresh(self,name)
        from worker_control import guard_launch
        if self.states[name].get("blocked_reason")=="model_refusal" or not guard_launch(self,name):
            s=self.states[name]
            return CliJobState(self.worker(name)["kind"],s["spawn"],s["resumes"],2**31,14,s.get("token"),s.get("next",0),False,False)
        s=self.states[name]
        return CliJobState(self.worker(name)["kind"],s["spawn"],s["resumes"],2**31,14,s.get("token"),s.get("next",0),
                           not s.get("token") and time.time()>=s.get("next",0),bool(s.get("token")))
    def _format_cli_job_session_id(self,name,state,decision):
        return f"launch_{self.states[name]['launches']+1:06d}"
    def _claim_cli_job_launch(self,name,session_id,decision):
        from worker_control import guard_launch
        if self.states[name].get("blocked_reason")=="model_refusal" or not guard_launch(self,name):return None
        s=self.states[name]
        s.update(spawn=decision.spawn_count,resumes=decision.resume_count,launches=s["launches"]+1,
                 status="starting",route_count=decision.endpoint_resume_count or 0)
        self.persist(name);return s
    def _build_cli_job_launch_spec(self,name,session_id,decision,claimed):
        w=self.worker(name);work=(self.run/name).resolve()
        for part in (".home",".codex",".claude",".cache",".tmp"): (work/part).mkdir(exist_ok=True)
        count=decision.endpoint_resume_count or 0
        # Reconsider the preferred route only when launching a new native turn.
        # A small probe must never interrupt a productive backup conversation.
        launch_pool = self.provider_pool(name)
        if w["kind"] == "gemini":
            from gemini_failover import begin, choose, eligible
            launch_pool = eligible(self, name)
            if self.probe:
                index = w.get('route_seed', 0) % len(launch_pool)
            else:
                index = choose(self.states[name], launch_pool)
                begin(self.states[name], launch_pool, index, w['model'])
                self._provider_secrets.update(r['api_key'] for r in launch_pool)
            route = launch_pool[index]
        else:
            index=(w.get("route_seed",0)+self.states[name].get("route_offset",0)+count//2)%len(launch_pool)
            recovery_route=self.states[name].pop("gemini_recovery_route",None)
            from worker_control import selected_route
            controlled=selected_route(self.states[name],None)
            if controlled is not None:
                index=controlled
            elif w["kind"]=="gemini" and recovery_route is not None:
                index=recovery_route%len(launch_pool)
                self.states[name]["route_offset"]=index-w.get("route_seed",0)-count//2
            elif index and not self.probe and not self._untried_session_routes(name) and self._primary_recovered(name):
                index=0
                self.states[name]["route_offset"]=-w.get("route_seed",0)-count//2
                self.log(name,"Primary provider recovered; continuing the same conversation on provider 1.")
            route=launch_pool[index]
        self.states[name]["route_index"]=index
        # Model traffic is direct unless the run explicitly selects a proxy.
        # This also covers legacy run files with a missing/null setting.
        model_proxy=self.config.get("model_api_proxy") or "direct"
        if model_proxy=="direct":model_proxy=None
        hosts={urlsplit(r["base_url"]).hostname for r in launch_pool}
        # Apply hostname-specific connectivity fixes on the next launch only;
        # existing native turns and their open connections remain untouched.
        configured=read(self.run/"run.json",self.config).get("model_api_host_proxies") or {}
        if (not isinstance(configured,dict) or
                any(not isinstance(host,str) or not isinstance(proxy,str) for host,proxy in configured.items())):
            raise ValueError("Model host proxies must map hostnames to HTTP(S) proxy URLs.")
        host_proxies={host:url(proxy,"Model host proxy") for host,proxy in configured.items() if host in hosts}
        if self.config.get("platform_proxy"):
            host_proxies[urlsplit(self.config["url"]).hostname]=self.config["platform_proxy"]
        # A fully direct launch needs no local routing intermediary.
        proxy_env={}
        if model_proxy or self.config.get("research_proxy") or host_proxies:
            if name not in self.routers:
                self.routers[name]=Router(hosts,self.config.get("research_proxy"),model_proxy,
                                         [urlsplit(self.config["url"]).hostname],
                                         host_proxies=host_proxies).__enter__()
            else:
                self.routers[name].model_hosts=hosts
                self.routers[name].model_proxy=model_proxy
                self.routers[name].host_proxies=host_proxies
            proxy_env={key:self.routers[name].url for key in
                       ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy")}
        # Credentials stay in memory and child environments, never run.json.
        env={k:v for k,v in self.env.items() if not any(x in k.upper() for x in ("API_KEY","AUTH_TOKEN","ACCESS_TOKEN","BASE_URL","PROXY")) and not k.startswith("BACKUP_")}
        env.update(HOME=str(work/".home"),CODEX_HOME=str(work/".codex"),CLAUDE_CONFIG_DIR=str(work/".claude"),
                   XDG_CONFIG_HOME=str(work/".home/.config"),XDG_CACHE_HOME=str(work/".cache"),
                   TMPDIR=str(work/".tmp"),PATH=self.bin_paths[w["kind"]][1],PYTHONUNBUFFERED="1",
                   VIRTUAL_ENV=str(work/".venv"),CUDA_VISIBLE_DEVICES="",OMP_NUM_THREADS="5",OPENBLAS_NUM_THREADS="5",MKL_NUM_THREADS="5",
                   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
        env.pop("RSTATION_GEMINI_COMPACT",None)
        env.pop("RSTATION_WORKER_LIVE_INPUT",None)
        if self.config.get("live_input", False) and not self.probe:
            env["RSTATION_WORKER_LIVE_INPUT"]="1"
        env.update(proxy_env)
        env["PATH"]=str(work/".venv/bin")+":"+env["PATH"]
        for key in ("CONDA_PREFIX","CONDA_DEFAULT_ENV","PYTHONHOME"):env.pop(key,None)
        direct_models = [urlsplit(r["base_url"]).hostname for r in launch_pool
                         if urlsplit(r["base_url"]).hostname not in host_proxies] if not model_proxy else []
        bypass=",".join(dict.fromkeys(["localhost","127.0.0.1","::1"]+direct_models+([] if self.config.get("platform_proxy") else [urlsplit(self.config["url"]).hostname])))
        env.update(NO_PROXY=bypass,no_proxy=bypass)
        if w["kind"]=="codex":env.update(CODEX_API_KEY=route["api_key"],CODEX_BASE_URL=route["base_url"])
        elif w["kind"] == "gemini":
            env.update(GEMINI_API_KEY=route["api_key"],GOOGLE_GEMINI_BASE_URL=route["base_url"])
            rotation = self.states[name].get('gemini_rotation', {})
            if not self.probe and rotation.pop('compact_pending', False):
                rotation['compact_launch'] = str(self.states[name]['launches'])
                env['RSTATION_GEMINI_COMPACT'] = rotation['compact_launch']
                self.persist(name)
            # Apply operator budget changes on the next launch without stopping
            # other researchers or their experiments in this running controller.
            context_tokens=int(read(self.run/"run.json",{}).get("gemini_context_tokens",
                               self.config.get("gemini_context_tokens",500000)))
            if context_tokens<=65536:
                raise ValueError("Context must exceed Gemini's 65,536-token output reserve.")
            from council import enabled
            current_config=read(self.run/"run.json",self.config)
            current_worker=next((v for v in current_config.get('workers',[]) if v['name']==w['name']),w)
            prepare_profile(work,self.config.get("disable_web",False),w["model"],context_tokens,
                            council=enabled(current_config,current_worker),coder=__import__('coder').enabled(current_config,current_worker))
        else:env.update(ANTHROPIC_API_KEY=route["api_key"],ANTHROPIC_BASE_URL=route["base_url"])
        if w["kind"]=="claude" and self.config.get("subagents",0):
            env.update(CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=str(self.config["subagents"]),
                       CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH="1",CLAUDE_CODE_SUBAGENT_MODEL=w["model"],
                       CLAUDE_CODE_EFFORT_LEVEL=reasoning_effort("claude",w["model"]))
        from capabilities import install_skills
        current=read(self.run/'run.json',self.config)
        current_worker=next((v for v in current.get('workers',[]) if v['name']==w['name']),w)
        if not self.probe:install_skills(self.run,current,current_worker)
        prompt="Reply exactly Hello. Do not use tools." if self.probe else "Continue your research." if decision.is_resume else initial_prompt(current,current_worker,work)
        if not self.probe and not decision.is_resume and claimed["launches"]>1:
            prompt+="\nYour previous session was interrupted. Read CONTINUATION.md and your saved notes, reuse your account and workspace, and continue the existing research.\n"
            if claimed.get("session_recovery"):
                prompt+="The preserved previous session state and continuation are in .runtime/session-recovery/. Check existing experiments before launching anything new.\n"
        self.log(name,f"{'Resuming' if decision.is_resume else 'Starting'} {w['model']}; provider {index+1}/{len(launch_pool)}; {session_id}")
        return CliJobLaunchSpec(self.bin_paths[w["kind"]][0],str(work/".runtime"/session_id),w["kind"],w["model"],str(work),str(work),prompt,env,
                network_access=not self.config.get("disable_web",False),
                codex_config_overrides=('features.enable_request_compression=false',
                    'model_reasoning_effort='+json.dumps(reasoning_effort("codex",w["model"]))) +
                (codex_delegation_config(work,w["model"],self.config.get("subagents",0),self.config.get("disable_web",False)) if w["kind"]=="codex" else ()) +
                (('features.apps=false','mcp_servers={}') if self.config.get("disable_web") else ()))

    def _primary_check_state(self,name,checked_at,healthy):
        worker=self.worker(name)
        # One probe per model per controller, shared by its workers and retained
        # across controller restarts. Never store credentials in this state.
        for other in self.config["workers"]:
            if (other["kind"],self.worker(other["name"])["model"])==(worker["kind"],worker["model"]) and self.provider_pool(other["name"]) == self.provider_pool(name):
                self.states[other["name"]].update(primary_checked_at=checked_at,primary_healthy=healthy)
                self.persist(other["name"])

    def _primary_recovered(self,name):
        state=self.states[name];now=time.time()
        if now-state.get("primary_checked_at",0)<PRIMARY_RECOVERY_SECONDS:
            return state.get("primary_healthy",False)
        # Claim the interval before probing so failed/crashed probes cannot
        # repeat on every continuation. Active research processes keep running.
        self._primary_check_state(name,now,False)
        try:
            healthy=self._probe_primary(self.worker(name))
        except Exception as exc:
            healthy=False
            self.log(name,"Primary check failed ("+type(exc).__name__+"); retaining backup.")
        self._primary_check_state(name,time.time(),healthy)
        if not healthy:self.log(name,"Primary provider check did not pass; retaining backup for another 30 minutes.")
        return healthy

    def _probe_primary(self,worker):
        # Reuse the real CLI and its routing adapter, but with a disposable home
        # and no research history. A greeting must never enter the worker session.
        with tempfile.TemporaryDirectory(prefix="rstation-primary-probe-") as directory:
            config={**self.config,"workers":[{**worker,"route_seed":0,"gpus":[]}],"subagents":0,"idle_seconds":PRIMARY_PROBE_SECONDS}
            config["model_api_host_proxies"]=read(self.run/"run.json",self.config).get("model_api_host_proxies",{})
            idle_timeout=constants.CLI_WORKER_TRANSCRIPT_IDLE_TIMEOUT_SECONDS
            manager=None
            try:
                manager=ResearchManager(directory,config,self.env,probe=True)
                manager._provider_pools = {worker["name"]: self.provider_pool(worker["name"])}
                manager.launch_cli_job(worker["name"])
                deadline=time.monotonic()+PRIMARY_PROBE_SECONDS
                while manager.active_sessions and time.monotonic()<deadline:
                    manager.poll_cli_jobs();time.sleep(.1)
                return manager.states[worker["name"]].get("status")=="probe_ok"
            finally:
                try:
                    if manager is not None:manager.close()
                finally:
                    constants.CLI_WORKER_TRANSCRIPT_IDLE_TIMEOUT_SECONDS=idle_timeout

    def _record_primary_failure(self,name):
        if not self.probe and self.states[name].get("route_index")==0:
            # A small greeting can pass while a full continuation fails. Do not
            # promote back to primary again using that stale success.
            self._primary_check_state(name,time.time(),False)

    def _record_pending_tools(self,name,event,backend):
        pending=self.pending_tools.setdefault(name,set())
        if backend=="claude":
            if event.get("type")=="system" and event.get("subtype")=="permission_denied":
                pending.discard(event.get("tool_use_id"))
            for part in claude_content(event):
                if part.get("type")=="tool_use":pending.add(part.get("id"))
                elif part.get("type")=="tool_result":pending.discard(part.get("tool_use_id"))
        elif backend=="codex":
            item=event.get("item",{})
            if event.get("type")=="item.completed":pending.discard(item.get("id"))
            elif event.get("type")=="item.started" and item.get("type") in {
                    "command_execution","file_change","mcp_tool_call","web_search","collab_tool_call"}:
                pending.add(item.get("id"))
        elif backend=="gemini":
            step=event.get("step_update",{})
            if step.get("step_type")=="tool":
                key=step.get("step_index")
                if step.get("state") in {"DONE","ERROR","FAILED","CANCELLED"}:pending.discard(key)
                else:pending.add(key)
    @staticmethod
    def _prepare_cli_job_launch(spec,decision,backend_getter):
        prepared=CliJobManager._prepare_cli_job_launch(spec,decision,backend_getter)
        # Native trusted researchers need to use the host-wide GPU coordinator.
        # The isolated benchmark launcher remains a separate container boundary.
        if spec.backend=="codex":
            command=list(prepared.command)
            if "workspace-write" in command:command[command.index("workspace-write")]="danger-full-access"
            prepared=replace(prepared,command=command)
        if spec.env.get("RSTATION_WORKER_LIVE_INPUT")=="1":
            command=[sys.executable,str(ROOT/"live_cli.py"),"--kind",spec.backend,
                     "--workspace",spec.workspace_root,"--prompt-file",str(Path(spec.run_dir)/"prompt.txt"),
                     "--model",spec.model_name,"--conversation",decision.resume_token or "",
                     "--",*prepared.command]
            prepared=replace(prepared,command=command,stdin_text=None)
        return prepared
    def _mark_cli_job_pid(self,name,pid):
        self.states[name].update(pid=pid,pid_token=pid_token(pid),last_process_group=pid,status="running",started=time.time())
        self.persist(name)
    def _on_cli_job_process_exited(self,name,session,code):
        # A process can write its final events after the poll's first read.
        # Drain unread bytes before classifying failure or recording success.
        self._before_cli_job_poll()
    def _cli_job_completion_ready(self,name,session):
        return (not getattr(session,"transcript_idle_timeout_triggered",False)
                and session.process.returncode==0 and self.completed(session))
    @staticmethod
    def completed(session):
        events=list(CliJobManager._read_transcript_events(session.transcript_path))
        if getattr(session,"backend",None)=="claude":
            results=[e for e in events if isinstance(e,dict) and e.get("type")=="result"]
            return bool(results and not results[-1].get("is_error",False)
                        and results[-1].get("stop_reason")!="refusal"
                        and results[-1].get("subtype") not in {"error_max_turns","error_during_execution"})
        if any(e.get("event")=="result" for e in events if isinstance(e,dict)):
            return completed_turn(e for e in events if isinstance(e,dict))
        for e in events:
            if not isinstance(e,dict):continue
            if e.get("type")=="turn.completed":return True
            if e.get("type")=="result" and not e.get("is_error",False) and e.get("subtype") not in {"error_max_turns","error_during_execution"}:return True
        return False
    def _on_cli_job_completed(self,name,session,code):
        s=self.states[name]
        token=self._get_cli_job_backend(session.backend).extract_resume_token(session.transcript_path)
        # Keep the successful endpoint for subsequent conversations.
        index=s.get("route_index",0);seed=self.worker(name).get("route_seed",0)
        s.update(token=token or s.get("token"),resumes=0,next=time.time()+5,status="probe_ok" if self.probe else "continuing",
                 pid=None,last_success=time.time(),route_count=0,route_offset=index-seed)
        self._endpoint_resume_counts[name]=0
        self.persist(name);self.log(name,"Turn completed; continuing research." if not self.probe else "Model connectivity check passed.")
    def _block_model_refusal(self,name,failure):
        if failure.category!="model_refusal":return False
        state=self.states[name]
        state.update(status="blocked",blocked_reason="model_refusal",next=0,pid=None,
                     last_failure_category="model_refusal",last_error="Native model refusal")
        self.persist(name)
        self.log(name,"Native model refusal; conversation retained for operator review.")
        return True

    @serialize_failure
    def _schedule_cli_job_resume(self,name,session,failure):
        from worker_control import handle_failure
        from gemini_failover import schedule
        if schedule(self,name,session,failure):return
        if handle_failure(self,name,failure):return
        if self._block_model_refusal(name,failure):return
        self._record_primary_failure(name)
        if self._maybe_recover_session(name,session,failure):return
        s=self.states[name]
        s["retry_total"]=s.get("retry_total",0)+1
        s.update(token=failure.resume_token or s.get("token"),resumes=failure.resume_count,
                 next=failure.next_resume_timestamp or time.time()+10,status="retrying",pid=None)
        self.persist(name);self.log(name,f"Interrupted; resuming the same session in {max(0,int(s['next']-time.time()))}s.")
    def _classify_cli_job_failure(self,name,session):
        # Only a concrete CLI error can invalidate a saved conversation.
        texts=[]
        model_refusal="the selected model cannot continue this conversation"
        turn_model_refusal=False
        confirmed_model_refusal=False
        try:texts.append(Path(session.stderr_path).read_text(errors="replace")[-16000:])
        except OSError:pass
        for event in self._read_transcript_events(session.transcript_path):
            if not isinstance(event,dict):continue
            # Claude can emit this notice as ordinary assistant text; only a
            # failed terminal result confirms that the turn could not continue.
            if getattr(session,"backend",None)=="claude" and event.get("type")=="assistant":
                turn_model_refusal |= any(
                    isinstance(part.get("text"),str) and part["text"].strip().lower().startswith(model_refusal)
                    for part in claude_content(event) if part.get("type")=="text")
            if event.get("type")=="result":
                result_text=str(event.get("result","")).lower()
                confirmed_model_refusal=(getattr(session,"backend",None)=="claude" and bool(event.get("is_error"))
                    and (event.get("stop_reason")=="refusal" or model_refusal in result_text or (turn_model_refusal and
                         (event.get("stop_reason")=="refusal" or "claude code is unable to respond" in result_text))))
                turn_model_refusal=False
            if event.get("type")=="system" and event.get("subtype")=="api_retry":
                texts.append("HTTP "+str(event.get("error_status","unknown")))
            if event.get("type")=="assistant" and (event.get("error") or event.get("isApiErrorMessage")):
                texts.append(json.dumps(event))
            if event.get("event")=="result" and event.get("result",{}).get("status")!="SUCCESS":texts.append(json.dumps(event))
            if event.get("type") in {"error","turn.failed"} or (event.get("type")=="result" and event.get("is_error")):
                texts.append(json.dumps(event))
        detail=" ".join(texts).lower()
        markers=("no conversation found with session id","session not found","no session found",
                 "conversation not found","failed to load session","could not find session",
                 "no rollout found for thread id")
        if confirmed_model_refusal:
            return CliJobFailure("model_refusal",False,"Native model refusal; retain conversation for operator review.")
        if any(marker in detail for marker in markers):
            self.states[name]["session_unavailable"]=True
            return CliJobFailure("session_unavailable",False,"The CLI reports that the saved conversation is unavailable.")
        if provider_unavailable(detail):
            return CliJobFailure("provider_unavailable",True,
                                 "Provider unavailable; retain the saved conversation.")
        return super()._classify_cli_job_failure(name,session)

    @serialize_failure
    def _schedule_cli_job_fresh_attempt(self,name,session,failure):
        from worker_control import handle_failure
        from gemini_failover import schedule
        if schedule(self,name,session,failure):return
        if handle_failure(self,name,failure):return
        if self._block_model_refusal(name,failure):return
        self._record_primary_failure(name)
        if self._maybe_recover_session(name,session,failure):return
        s=self.states[name]
        if s.get("token") and not s.get("session_unavailable"):
            # Keep context through ordinary outages. The bounded recovery above
            # also handles an unusable conversation without an explicit error.
            s.update(resumes=0,next=time.time()+300,status="retrying",pid=None)
            s["retry_total"]=s.get("retry_total",0)+1
            self.persist(name)
            self.log(name,"Retry cycle exhausted or stream interrupted; retaining this conversation and retrying after a cooldown.")
            return
        # Retain account/workspace; rotate when no resumable token was returned.
        s.update(token=None,resumes=0,next=time.time()+min(300,10*max(1,s["spawn"])),status="retrying",pid=None,
                 route_offset=s.get("route_offset",0)+1)
        self.persist(name);self.log(name,"Saved session unavailable; a fresh session will reuse this workspace and account.")
    def _on_cli_job_attempts_exhausted(self,name,session,failure):
        self._schedule_cli_job_fresh_attempt(name,session,failure)

    @staticmethod
    def _research_progress(state):
        # Stream keepalives, API retries and new session IDs are not progress.
        return max(state.get(key,0) for key in ("last_response","last_tool_completed","last_success"))

    def _untried_session_routes(self,name):
        state=self.states[name];streak=state.get("session_failure_streak",{})
        return (bool(streak) and self._research_progress(state)<=streak["progress"]
                and len(streak["routes"])<len(self.provider_pool(name)))

    def _session_recovery_protected(self,name,session):
        """A terminal CLI may still own orphaned tools or background jobs."""
        work=self.run/name
        if self.states[name].get("worker_control"):return True
        if (self.run/"STOP").exists() or (work/".runtime/PAUSE").exists():return True
        from supervision import sleeping
        if sleeping(work):return True
        for path in (work/".runtime/gpu-jobs").glob("*.json"):
            job=read(path,{})
            if alive(job.get("pid"),job.get("token")):return True
        process=getattr(session,"process",None)
        if process is not None and process.poll() is None:return True
        # Each CLI is its process-group leader. Its group can outlive it; asking
        # getpgid(dead_pid) would miss those experiments after the wrapper exits.
        from live_watchdog import group_has_work
        group=(process.pid if process is not None else
               self.states[name].get("session_failure_streak",{}).get("process_group"))
        if not group:return False
        try:return group_has_work(group,{group} if process is not None else set())
        except PermissionError:return True

    def _maybe_recover_session(self,name,session,failure):
        """Try one fresh conversation after sustained, all-route failure.

        Persist the failure window across controller restarts, and require new
        research progress before another reset. Explicit provider outages do not
        justify even the first reset. No process is stopped here.
        """
        state=self.states[name]
        token=failure.resume_token or state.get("token")
        if self.probe or not token or state.get("session_unavailable"):return False
        now=time.time();progress=self._research_progress(state)
        outage=state.get("provider_outage")
        # Bootstrap old controllers' persisted HTTP retry evidence once. Keep
        # the marker after progress so an old last_error cannot rearm it.
        if (failure.category=="provider_unavailable" or provider_unavailable(failure.reason)
                or (outage is None and provider_unavailable(state.get("last_error","")))):
            outage=dict(progress=progress)
            state["provider_outage"]=outage
        if outage is not None and progress<=outage["progress"]:
            # Budget exhaustion may arrive without a session/transcript. The
            # durable marker protects that path and survives controller restart.
            # Outage launches must not accumulate toward a later session reset.
            state.pop("session_failure_streak",None)
            return False
        streak=state.get("session_failure_streak",{})
        if not streak or progress>streak["progress"] or token!=streak["token"]:
            streak=dict(progress=progress,token=token,since=now,failures=0,routes=[],last_launch=None)
        # The lifecycle can report exhaustion again before the next launch.
        # Count failed executions, not repeated decisions about one execution.
        if streak["last_launch"]!=state["launches"]:
            streak["failures"]+=1
            streak["routes"]=sorted(set(streak["routes"])|{state.get("route_index",0)})
            streak["last_launch"]=state["launches"]
        if getattr(session,"process",None) is not None:streak["process_group"]=session.process.pid
        state["session_failure_streak"]=streak
        previous=state.get("session_recovery",{})
        if (streak["failures"]<SESSION_RECOVERY_FAILURES
                or now-max(streak["since"],progress)<SESSION_RECOVERY_IDLE_SECONDS
                or len(streak["routes"])<len(self.provider_pool(name))
                or (previous and (progress<=previous["progress"]
                                  or now-previous["at"]<SESSION_RECOVERY_COOLDOWN_SECONDS))
                or self._session_recovery_protected(name,session)):
            return False
        directory=self.run/name/".runtime/session-recovery"/f"{int(now)}-launch-{state['launches']:06d}"
        # Save evidence before changing the token; native conversation stores,
        # credentials, research files, model and context budgets stay untouched.
        save(directory/"state.json",state)
        continuation=self.run/name/"CONTINUATION.md"
        if continuation.is_file():shutil.copyfile(continuation,directory/"CONTINUATION.md")
        record=dict(at=now,progress=progress,previous_token=token,failures=streak["failures"],
                    routes=streak["routes"],archive=str(directory.relative_to(self.run/name)))
        save(directory/"recovery.json",record)
        next_route=(state.get("route_index",0)+1)%len(self.provider_pool(name))
        state.update(token=None,resumes=0,route_count=0,next=now+60,status="retrying",pid=None,
                     route_offset=next_route-self.worker(name).get("route_seed",0),
                     session_unavailable=True,session_recovery=record)
        state.pop("gemini_recovery_route",None)
        self._endpoint_resume_counts[name]=0
        self.persist(name)
        self.log(name,f"No research progress after {streak['failures']} failed launches across all providers; "
                 "saved the old session and continuation, and scheduled one fresh conversation in the same workspace.")
        return True
    def _gemini_has_live_work(self,name,session):
        """Do not interrupt a silent foreground tool or a reserved experiment."""
        runtime=self.run/name/".runtime"
        for path in (runtime/"gpu-jobs").glob("*.json"):
            job=read(path,{})
            if alive(job.get("pid"),job.get("token")):
                return True
        # live_cli itself has one permanent native CLI child. Only children
        # below that CLI count as tool work; its existence is not progress.
        native=read(runtime/"live_input.json",{})
        pid=session.process.pid
        cli_pids={pid}
        if alive(native.get("pid"),native.get("token")):
            try:
                if os.getpgid(native["pid"])==os.getpgid(pid):
                    pid=native["pid"]
                    cli_pids.add(pid)
            except ProcessLookupError:
                return False
        try:
            # Background shell jobs can be reparented while still sharing the
            # CLI process group. Recovery must not signal those experiments.
            group=os.getpgid(session.process.pid)
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit() or int(entry.name) in cli_pids:continue
                try:fields=(entry/"stat").read_text().rsplit(")",1)[1].split()
                except (FileNotFoundError,ProcessLookupError):continue
                if fields[0] not in {"Z","X"} and int(fields[2])==group:return True
            for task in Path(f"/proc/{pid}/task").iterdir():
                try: children=(task/"children").read_text().split()
                except FileNotFoundError: continue
                if any(pid_token(int(child)) for child in children):
                    return True
        except (FileNotFoundError,ProcessLookupError):
            pass
        except PermissionError:
            return True  # Cannot establish that interrupting the tool is safe.
        return False

    def _check_cli_job_transcript_idle_timeouts(self):
        self._check_provider_retry_stalls()
        super()._check_cli_job_transcript_idle_timeouts()
        # Keep Station's shared lifecycle unchanged; Gemini has its own native
        # stream and must also recover when it never emits a terminal result.
        limit=self.config.get("idle_seconds",1800)
        if limit<=0:return
        now=time.time()
        for name,session in list(self.active_sessions.items()):
            if session.backend!="gemini" or session.transcript_idle_timeout_triggered:
                continue
            if session.process.poll() is not None:continue
            try:size=Path(session.transcript_path).stat().st_size
            except FileNotFoundError:size=0
            if size!=session.last_transcript_size:
                session.last_transcript_size=size
                session.last_transcript_growth_timestamp=now
                continue
            idle=now-session.last_transcript_growth_timestamp
            if idle<limit:continue
            if self._gemini_has_live_work(name,session):
                session.last_transcript_growth_timestamp=now
                continue
            # A tool may start just as the idle threshold is crossed. Freeze
            # the CLI briefly and recheck before sending the recovery signal.
            try:os.killpg(session.process.pid,signal.SIGSTOP)
            except ProcessLookupError:continue
            try:
                try:new_size=Path(session.transcript_path).stat().st_size
                except FileNotFoundError:new_size=0
                if new_size!=size or self._gemini_has_live_work(name,session):
                    session.last_transcript_size=new_size
                    session.last_transcript_growth_timestamp=time.time()
                    continue
                reason=self._cli_job_idle_timeout_reason(name,session,int(idle),limit)
                session.transcript_idle_timeout_triggered=True
                session.transcript_idle_timeout_reason=reason
                self._on_cli_job_idle_timeout(name,session,reason)
                self._terminate_cli_job_process(session,force=False)
            finally:
                try:os.killpg(session.process.pid,signal.SIGCONT)
                except ProcessLookupError:pass

    def _check_provider_retry_stalls(self):
        """Retry chatter must not keep an otherwise idle request alive forever."""
        limit=self.config.get("idle_seconds",1800)
        if limit<=0:return
        now=time.time()
        from supervision import sleeping
        from controller_guard import reclaim_idle_clis
        for name,session in list(self.active_sessions.items()):
            state=self.states[name]
            if session.process.poll() is not None or session.transcript_idle_timeout_triggered:continue
            if not state.get("api_retries"):continue
            if now-max(state.get("started",now),self._research_progress(state))<limit:continue
            work=self.run/name
            if ((self.run/"STOP").exists() or (work/".runtime/PAUSE").exists() or sleeping(work)):continue
            if any(alive(job.get("pid"),job.get("token")) for job in
                   (read(path,{}) for path in (work/".runtime/gpu-jobs").glob("*.json"))):continue
            # This fences native groups and checks their journals and descendants,
            # including detached Codex hosts, before reclaiming only idle CLIs.
            if not reclaim_idle_clis(self.run,{"workers":[self.worker(name)]}):continue
            session.transcript_idle_timeout_triggered=True
            session.transcript_idle_timeout_reason="Provider retries without research progress exceeded the idle limit."
            if session.backend=="gemini":
                state["gemini_recovery_route"]=(state.get("route_index",0)+1)%len(self.provider_pool(name))
                self.persist(name)
            self.log(name,"Provider retry notices continued without research progress; resuming the saved conversation through the normal provider rotation.")

    def _cli_job_idle_timeout_reason(self,name,session,idle,limit):
        return f"No transcript growth for {idle}s (limit {limit}s)."
    def _on_cli_job_idle_timeout(self,name,session,reason,*_):
        self.states[name]["kill_after"]=time.time()+10
        if session.backend=="gemini":
            state=self.states[name]
            state["gemini_recovery_route"]=(state.get("route_index",0)+1)%len(self.provider_pool(name))
            self.persist(name)
            self.log(name,"Gemini stream stalled without live tool/experiment work; rotating provider and resuming the same conversation.")
        else:
            self.log(name,"Transcript idle timeout; recovering the session.")
    def _after_cli_job_watchdogs(self):
        # Graceful termination above gets a bounded cleanup period. Claude's
        # native retry/wait behavior remains managed by the shared lifecycle.
        for name,session in self.active_sessions.items():
            deadline=self.states[name].get("kill_after")
            if deadline and time.time()>=deadline and session.process.poll() is None:
                self._terminate_cli_job_process(session,force=True)
    def _on_cli_job_started(self,name,session,decision):
        self.pending_tools[name]=set()
        self.states[name].pop("kill_after",None)
        self.states[name].pop("provider_interrupt",None)
        self.states[name].pop("session_unavailable",None)
        self.states[name]["api_retries"]=0
        self.persist(name)
    def _before_cli_job_poll(self):
        for name,session in self.active_sessions.items():
            path=Path(session.transcript_path);offset=self.log_offsets.get(str(path),0)
            if not path.exists():continue
            state=self.states[name];changed=False
            with path.open() as f:
                f.seek(offset)
                while True:
                    offset=f.tell();line=f.readline()
                    if not line or not line.endswith("\n"):break
                    self.log_offsets[str(path)]=f.tell()
                    try:e=json.loads(line)
                    except ValueError:continue
                    if not isinstance(e,dict):continue
                    self._record_pending_tools(name,e,session.backend)
                    now=time.time();state["last_stream_event"]=now;changed=True
                    token=e.get("thread_id") if e.get("type")=="thread.started" else e.get("session_id")
                    if session.backend == "gemini":
                        token = e.get("conversation_id") or e.get("step_update",{}).get("conversation_id") or e.get("result",{}).get("conversation_id")
                        self._record_gemini_event(name,e,now)
                    if token:state["token"]=token
                    # Record native retries without treating them as research
                    # progress or interrupting the CLI's own recovery cycle.
                    codex_retry=session.backend=="codex" and e.get("type")=="error"
                    claude_retry=session.backend=="claude" and e.get("type")=="system" and e.get("subtype")=="api_retry"
                    if codex_retry or claude_retry:
                        state["api_retries"]=state.get("api_retries",0)+1
                        state["last_error"]=("Codex stream retry" if codex_retry else
                                             "Claude API retry, HTTP "+str(e.get("error_status","unknown")))
                    elif (e.get("type") in {"item.started","item.completed","turn.completed"} or
                          (e.get("type")=="assistant" and not e.get("error") and not e.get("isApiErrorMessage"))):
                        state["api_retries"]=0
                    item=e.get("item",{})
                    if e.get("type")=="item.completed":
                        text=item.get("text") or item.get("command") or ""
                        if item.get("type") in {"agent_message","reasoning"}:
                            state["last_response"]=now
                            state["last_response_excerpt"]=text[-400:]
                        if item.get("type")=="command_execution":
                            state["last_tool_completed"]=now
                        if text:self.log(name,text[-4000:])
                    if e.get("type")=="assistant" and not e.get("error") and not e.get("isApiErrorMessage"):
                        parts=claude_content(e)
                        if parts:state["last_response"]=now
                        for part in parts:
                            if part.get("type")=="text" and isinstance(part.get("text"),str):
                                state["last_response_excerpt"]=part["text"][-400:]
                                self.log(name,part["text"][-4000:])
                    if e.get("type")=="user" and any(p.get("type")=="tool_result" for p in claude_content(e)):
                        state["last_tool_completed"]=now
            if changed:self.persist(name)
    def _record_gemini_event(self,name,event,now):
        state=self.states[name]
        step=event.get("step_update",{})
        # Native error events grow the transcript but are not model progress.
        # Feed the same bounded, experiment-safe retry watchdog as other CLIs.
        if step.get("step_type")=="error_message":
            state["api_retries"]=state.get("api_retries",0)+1
            state["last_error"]="Gemini native error; waiting for provider recovery."
        if step.get("step_type")=="agent_response":
            index=step.get("step_index")
            if state.get("response_step")!=index:
                state["response_before_step"]={key:state.get(key) for key in ("last_response","last_response_excerpt")}
            text=(state.get("response_text","") if state.get("response_step")==index else "")+step.get("text_delta","")
            if provider_account_error(text):
                for key,value in state.get("response_before_step",{}).items():
                    if value is None:state.pop(key,None)
                    else:state[key]=value
                state.update(response_step=index,response_text=text)
                state["last_error"]="Gemini provider returned an account restriction notice."
                state["gemini_recovery_route"]=(state.get("route_index",0)+1)%len(self.provider_pool(name))
                return
        if provider_account_error(event.get("result",{}).get("response")):
            state["last_error"]="Gemini provider returned an account restriction notice."
            state["gemini_recovery_route"]=(state.get("route_index",0)+1)%len(self.provider_pool(name))
            return
        # Tool-only model turns have usage but no text delta in the native stream.
        usage=step.get("usage",{})
        if (step.get("step_type")=="agent_response" and step.get("state")=="DONE"
                and usage.get("output_tokens",0)>usage.get("thinking_tokens",0)):
            state["last_response"]=now
            state["api_retries"]=0
        if step.get("step_type")=="agent_response" and step.get("text_delta"):
            state["last_response"]=now
            state["api_retries"]=0
            index=step.get("step_index")
            if state.get("response_step")!=index:
                state.update(response_step=index,response_text="")
            state["response_text"]=(state.get("response_text","")+step["text_delta"])[-4000:]
            state["last_response_excerpt"]=state["response_text"][-400:]
            if step.get("state")=="DONE":self.log(name,state["response_text"])
        if step.get("step_type")=="tool" and step.get("state")=="DONE":
            state["last_tool_completed"]=now
            state["api_retries"]=0
            info=step.get("tool_info",{})
            self.log(name,step.get("tool_name","tool")+" "+json.dumps(info,ensure_ascii=False)[-4000:])
        if event.get("event")=="result":
            result=event.get("result",{})
            if result.get("status")=="SUCCESS" and result.get("response"):
                state["last_response"]=now
                state["api_retries"]=0
                state["last_response_excerpt"]=result["response"][-400:]
                self.log(name,result["response"][-4000:])
            elif result.get("status")!="SUCCESS":
                state["last_error"]="Antigravity turn ended: "+str(result.get("status","unknown"))

        from gemini_failover import event_evidence
        event_evidence(state,event,now)

    @classmethod
    def _transcript_has_normal_progress(cls,path):
        for event in cls._read_transcript_events(path):
            step=event.get("step_update",{})
            if step.get("step_type")=="tool" and step.get("state")=="DONE":return True
        return super()._transcript_has_normal_progress(path)

    def pause_worker(self,name):
        session=self.active_sessions.pop(name,None)
        if session:
            token=self._get_cli_job_backend(session.backend).extract_resume_token(session.transcript_path)
            if token:self.states[name]["token"]=token
            self._abort_cli_job_process(session)
            self._close_cli_job_session_handles(session)
        self.states[name].update(pid=None,status="paused")
        self.persist(name)

    def check_storage(self):
        if time.time()-self.last_storage<600:return
        self.last_storage=time.time()
        for w in self.config["workers"]:
            work=self.run/w["name"];size=workspace_bytes(work)
            self.states[w["name"]]["workspace_bytes"]=size
            self.persist(w["name"])
            text=f"Workspace use: {size/1e9:.2f} GB / {self.config['storage_gb']:g} GB advisory budget.\n"
            if size>self.config["storage_gb"]*1e9*.9:
                text+="Approaching or exceeding your budget: remove disposable files while preserving research results.\n"
                self.log(w["name"],text.strip())
            (work/"STORAGE_USAGE.txt").write_text(text)
    def close(self):
        for name,session in list(self.active_sessions.items()):
            token=self._get_cli_job_backend(session.backend).extract_resume_token(session.transcript_path)
            if token:self.states[name]["token"]=token
            self._abort_cli_job_process(session);self._close_cli_job_session_handles(session)
            self.states[name].update(pid=None,status="stopped");self.persist(name)
        self.active_sessions.clear()
        for router in [*self.routers.values(), *getattr(self,"_provider_retired_routers",[])]:router.__exit__(None,None,None)

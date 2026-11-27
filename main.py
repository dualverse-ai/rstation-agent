#!/usr/bin/env python3
"""Private rStation research workers; this module is not part of the service."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from monitor import show_status, show_logs
from common import ROOT, NAME, save, read, lock, alive, pid_token, url, problem_info, safe_environment
from recovery import ResearchManager, routes, executable
from subagents import check_cli_version

def positive(value):
    value=float(value)
    if value<=0 or value!=value or value==float("inf"):raise argparse.ArgumentTypeError("Must be a positive finite number.")
    return value

def gpu_list(value):
    if not value:return []
    try:values=list(dict.fromkeys(int(v.strip()) for v in value.split(",")))
    except ValueError:raise argparse.ArgumentTypeError("Use comma-separated GPU indices.") from None
    if any(v<0 for v in values):raise argparse.ArgumentTypeError("GPU indices must be nonnegative.")
    return values

def gemini_context_tokens(value):
    value=int(value)
    if value<=65536:raise argparse.ArgumentTypeError("Context must exceed Gemini's 65,536-token output reserve.")
    return value

def run_dir(name):
    if not NAME.fullmatch(name):raise ValueError("Run names use letters, numbers, underscores and hyphens (up to 80 characters).")
    return Path(os.environ.get("RSTATION_AGENT_DATA",str(ROOT/"agent_data"))).expanduser().resolve()/name

def build_config(args, info):
    if not NAME.fullmatch(args.problem):raise ValueError("Invalid problem name.")
    if min(args.codex,args.claude,args.gemini)<0 or args.codex+args.claude+args.gemini<1:raise ValueError("Select at least one worker; counts must be nonnegative.")
    if args.gemini and args.subagents:raise ValueError("Antigravity workers currently run without subagents.")
    if args.subagents<0:raise ValueError("--subagents must be nonnegative.")
    if args.gpu_per_agent<1:raise ValueError("--gpu-per-agent must be positive.")
    if args.disable_web and args.research_proxy:raise ValueError("--research-proxy cannot be used with --disable-web.")
    name=args.name or args.problem+"_"+datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    run_dir(name)
    overrides={}
    for item in args.worker_gpus:
        worker,sep,value=item.partition("=")
        if not sep or worker in overrides:raise ValueError("Use --worker-gpus WORKER=0,1 once per worker.")
        overrides[worker]=gpu_list(value)
    workers=[]
    for kind,count,model in (("codex",args.codex,args.codex_model),("claude",args.claude,args.claude_model),("gemini",args.gemini,args.gemini_model)):
        for index in range(1,count+1):
            worker=f"{kind}_{index}";pool=overrides.pop(worker,args.gpus)
            if not set(pool)<=set(args.gpus):raise ValueError("Worker GPU pools must be subsets of --gpus.")
            workers.append(dict(name=worker,kind=kind,model=model,gpus=pool))
    invitation_file = getattr(args,'invitation_codes_file',None)
    if invitation_file:
        codes=json.loads(invitation_file.read_text())
        if not isinstance(codes,dict) or set(codes)!={w['name'] for w in workers}:
            raise ValueError('Provide one invitation code for each worker name.')
        if any(not isinstance(c,str) or not c.strip() or len(c)>100 for c in codes.values()) or len(set(codes.values()))!=len(codes):
            raise ValueError('Each worker needs its own nonempty invitation code.')
        for w in workers:w['invitation_code']=codes[w['name']]
    if overrides:raise ValueError("Unknown worker in --worker-gpus: "+", ".join(overrides))
    config=dict(name=name,problem=args.problem,url=url(args.url),category=info["category"],workers=workers,
                disable_web=args.disable_web,subagents=args.subagents,gemini_context_tokens=args.gemini_context_tokens,
                council=getattr(args,'council',True),coder=getattr(args,'coder',True),coder_model=getattr(args,'coder_model','gpt-6.2-sol'),
                instructions=args.instructions_file.read_text() if getattr(args,"instructions_file",None) else "",
                storage_gb=args.storage_gb,gpu_per_agent=args.gpu_per_agent,gpu_coord_file=str(Path(args.gpu_coord_file).expanduser().resolve()),
                gpu_job_seconds=args.gpu_job_seconds,idle_seconds=args.idle_seconds,
                platform_proxy=url(args.platform_proxy,"Platform proxy") if args.platform_proxy else None,
                research_proxy=url(args.research_proxy,"Research proxy") if args.research_proxy else None,
                model_api_proxy=("direct" if args.model_api_proxy=="direct" else url(args.model_api_proxy,"Model proxy")) if args.model_api_proxy else None,
                created=datetime.now(timezone.utc).isoformat(),tmux="rs-"+name,live_input=True)
    if getattr(args,"gpu_pool_profiles",None):
        from pool import profiles
        config["gpu_pool"]=profiles(args.gpu_pool_profiles,config)
    return config

def check_machine(config,env):
    if not shutil.which("tmux"):raise ValueError("Install tmux before starting workers.")
    for kind in {w["kind"] for w in config["workers"]}:
        routes(kind,env)
        binary,path=executable(kind,env)
        if config.get("subagents",0):check_cli_version(kind,binary,dict(env,PATH=path))
    pool=sorted({g for w in config["workers"] for g in w["gpus"]})
    if pool:
        try:
            result=subprocess.run(["nvidia-smi","--query-gpu=index","--format=csv,noheader"],capture_output=True,text=True,check=True,timeout=15)
            available={int(v.strip()) for v in result.stdout.splitlines()}
        except (OSError,ValueError,subprocess.SubprocessError):
            raise ValueError("Cannot inspect GPUs with nvidia-smi.") from None
        if not set(pool)<=available:raise ValueError("Requested GPU pool contains an unavailable physical GPU index.")
        coord=Path(config["gpu_coord_file"])
        if coord.exists():
            # Match Station's write/lock access, without allocating or clearing slots.
            with coord.open("r+") as f:
                import fcntl
                fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
                json.load(f)
        elif not os.access(coord.parent,os.W_OK):
            raise ValueError("Cannot create the shared GPU coordination file.")

def preflight(config,env):
    check_machine(config,env)
    for kind in dict.fromkeys(w["kind"] for w in config["workers"]):
        template=next(w for w in config["workers"] if w["kind"]==kind)
        pool=routes(kind,env)
        for index in range(len(pool)):
            print(f"Checking {kind} / {template['model']} through provider {index+1}/{len(pool)}...",flush=True)
            with tempfile.TemporaryDirectory(prefix="rstation-agent-probe-") as tmp:
                worker={**template,"route_seed":index,"gpus":[]}
                probe_config={**config,"workers":[worker],"idle_seconds":60}
                manager=ResearchManager(tmp,probe_config,env,probe=True)
                success=False
                try:
                    manager.launch_cli_job(worker["name"])
                    deadline=time.monotonic()+90
                    while manager.active_sessions and time.monotonic()<deadline:
                        manager.poll_cli_jobs();time.sleep(.1)
                    s=manager.states[worker["name"]]
                    if s.get("status")=="probe_ok":
                        directory=Path(tmp)/worker["name"]/".runtime/launch_000001"
                        transcript=(directory/"transcript.jsonl").read_text()
                        success="Hello" in transcript
                finally:manager.close()
            if success:
                for w in config["workers"]:
                    if w["kind"]==kind:w["route_seed"]=index
                break
            print(f"{kind} provider {index+1} did not pass the greeting check.",flush=True)
        else:raise ValueError(f"No healthy {kind} route. Check the {kind.upper()} credentials, endpoint and model access in .env or your shell environment, then try again; no research workers were launched.")

def prepare_workspaces(run,config):
    from common import initial_prompt
    for worker in config["workers"]:
        work=run/worker["name"]
        (work/".runtime").mkdir(parents=True,exist_ok=True,mode=0o700)
        work.chmod(0o700)
        if not (work/".venv/bin/python").exists():
            subprocess.run([sys.executable,"-m","venv",str(work/".venv")],check=True,stdout=subprocess.DEVNULL)
        # Stable helper wrapper: settings live outside the agent workspace.
        helper="#!/usr/bin/env bash\nexec "+shlex.join([str(ROOT/".venv/bin/python"),str(ROOT/"main.py"),"_gpu",str(run),worker["name"]])+' "$@"\n'
        (work/"gpu-run").write_text(helper);(work/"gpu-run").chmod(0o755)
        (work/"RESEARCH_INSTRUCTIONS.md").write_text(initial_prompt(config,worker,work))
        from pool import prepare
        prepare(run,config,worker)
        from council import prepare_worker
        prepare_worker(run,config,worker)
        from coder import prepare_worker as prepare_coder
        from capabilities import install_skills
        prepare_coder(run,config,worker);install_skills(run,config,worker)
        (work/".runtime/console.log").touch()

def tmux_views(run,config):
    target=config["tmux"]
    if subprocess.run(["tmux","has-session","-t","="+target],capture_output=True).returncode==0:return
    for i,worker in enumerate(config["workers"]):
        command=shlex.join(["tail","-n","60","-F",str(run/worker["name"]/".runtime/console.log")])
        if i==0:
            args=["tmux","new-session","-d","-s",target,"-n","research","-x","180","-y","50",command]
        else:args=["tmux","split-window","-t",target+":research",command]
        subprocess.run(args,check=True)
        subprocess.run(["tmux","select-pane","-t",target+":research","-T",worker["name"]],check=True)
        subprocess.run(["tmux","select-layout","-t",target+":research","tiled"],check=True,capture_output=True)
    subprocess.run(["tmux","set-option","-t",target,"pane-border-status","top"],check=True)

def start_controller(run,config,env,keep_experiments=False):
    info=read(run/"controller.json",{})
    if alive(info.get("pid"),info.get("token")):raise ValueError("This run is already active. Use attach or status.")
    stopped=run/"STOP"
    stopped.unlink(missing_ok=True)
    with (run/"controller.log").open("a") as log:
        process=subprocess.Popen([sys.executable,str(ROOT/"main.py"),"_controller",str(run)]+(["--keep-experiments"] if keep_experiments else []),env=env,
                                 stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        info=read(run/"controller.json",{})
        if info.get("pid")==process.pid and alive(info.get("pid"),info.get("token")):break
        if process.poll() is not None:raise ValueError(f"Controller could not start. See {run/'controller.log'}.")
        time.sleep(.1)
    else:raise ValueError(f"Controller did not become ready. See {run/'controller.log'}.")
    try:tmux_views(run,config)
    except Exception:
        (run/"STOP").touch()
        os.kill(process.pid,signal.SIGTERM)
        raise ValueError("Could not create tmux views; the new controller has been stopped.") from None
    from controller_guard import ensure_guardian
    ensure_guardian(run,env)
    print(f"Run: {config['name']}\nAttach: tmux attach -t {config['tmux']}\nData: {run}")

def controller(run,keep_experiments=False):
    from gpu import reap_orphan_gpu_jobs
    run=Path(run)
    with lock(run/"controller.lock",True):
        config=read(run/"run.json")
        # Never sacrifice a still-running research tool to reclaim its CLI.
        from controller_guard import reclaim_idle_clis, code_revision
        revision=code_revision()
        while not reclaim_idle_clis(run,config):
            if (run/"STOP").exists():return
            time.sleep(2)
        for w in config["workers"]:
            reap_orphan_gpu_jobs(run,w["name"],stop_all=not keep_experiments)
        manager=ResearchManager(run,config,dict(os.environ))
        stopping=False
        last_supervisor_check=0
        last_watchdog_check=0
        stop_reason="unexpected_exit"
        def halt(sig,*_):
            nonlocal stopping,stop_reason
            stop_reason="signal:"+str(sig)
            stopping=True
        for sig in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):signal.signal(sig,halt)
        save(run/"controller.json",{"pid":os.getpid(),"token":pid_token(os.getpid()),"status":"running","code_revision":revision})
        try:
            while not stopping and not (run/"STOP").exists():
                save(run/"controller.json",{"pid":os.getpid(),"token":pid_token(os.getpid()),"status":"running","heartbeat":time.time(),"code_revision":revision})
                if time.time()-last_watchdog_check>=60:
                    if not config.get("replacement_managed"):
                        from watchdog_service import ensure_watchdog
                        ensure_watchdog(run.parent)
                    from controller_guard import ensure_guardian
                    ensure_guardian(run,dict(os.environ))
                    from capabilities import ensure_services
                    for capability,error in ensure_services(run,dict(os.environ)).items():
                        for w in config['workers']:manager.log(w['name'],capability+' unavailable: '+error)
                    last_watchdog_check=time.time()
                manager.poll_cli_jobs()
                if any((run/w["name"]/".runtime/worker-control-request.json").exists() for w in config["workers"]):
                    from worker_control import process_requests
                    process_requests(manager)
                try:manager.check_storage()
                except OSError as exc:
                    # Advisory storage files must not stop unrelated researchers.
                    for w in config["workers"]:
                        manager.log(w["name"],"Storage advisory unavailable ("+type(exc).__name__+"); will retry.")
                if config.get('role')=='supervisor' and not config.get('replacement_managed') and time.time()-last_supervisor_check>=2:
                    from supervision import tick
                    try:
                        tick(config)
                        last_supervisor_check=time.time()
                    except (OSError, ValueError, sqlite3.Error) as exc:
                        # The mailbox remains durable if its native outbox
                        # temporarily cannot write a participant's queue.
                        last_supervisor_check=time.time()+28
                        manager.log('supervisor',f'Local mailbox delivery unavailable ({type(exc).__name__}); retrying in 30 seconds.')
                for w in config["workers"]:
                    name=w["name"]
                    reap_orphan_gpu_jobs(run,name)
                    if (run/name/".runtime/PAUSE").exists():
                        if manager.states[name].get("status")!="paused":manager.pause_worker(name)
                        continue
                    if manager.states[name].get("status")=="paused":
                        manager.states[name].update(status="continuing",next=0)
                    if name not in manager.active_sessions:
                        try:manager.launch_cli_job(name)
                        except Exception as exc:
                            manager.states[name].update(status="retrying",next=time.time()+30,pid=None)
                            manager.persist(name)
                            manager.log(name,"Launch failed ("+type(exc).__name__+"); retrying in 30s.")
                time.sleep(.5)
        except Exception as exc:
            stop_reason="error:"+type(exc).__name__
            save(run/"controller-error.json",dict(at=time.time(),error=type(exc).__name__,
                frames=[dict(file=f.filename,line=f.lineno,function=f.name) for f in traceback.extract_tb(exc.__traceback__)]))
            raise
        finally:
            intentional=(run/"STOP").exists()
            if intentional:
                if (run/"STOP").exists():stop_reason="operator"
                manager.close()
                for w in config["workers"]:reap_orphan_gpu_jobs(run,w["name"],stop_all=True)
            # Unexpected exits leave tools/jobs running. The guardian waits for
            # foreground work before reclaiming CLIs and preserves GPU jobs.
            save(run/"controller.json",{"pid":None,"token":None,"status":"stopped","reason":stop_reason})

def stop(run):
    (run/"STOP").touch()
    info=read(run/"controller.json",{})
    if alive(info.get("pid"),info.get("token")):
        os.kill(info["pid"],signal.SIGTERM)
        deadline=time.monotonic()+40
        while alive(info["pid"],info["token"]) and time.monotonic()<deadline:time.sleep(.2)
        if alive(info["pid"],info["token"]):raise ValueError("Controller is still stopping; inspect controller.log.")
    else:
        from gpu import stop_group,reap_orphan_gpu_jobs
        config=read(run/"run.json")
        for w in config["workers"]:
            state=read(run/w["name"]/".runtime/state.json",{})
            if alive(state.get("pid"),state.get("pid_token")):stop_group(state["pid"])
            reap_orphan_gpu_jobs(run,w["name"],stop_all=True)
    print("Stopped. Workspace and accounts are preserved.")

def parser():
    p=argparse.ArgumentParser(description="Launch and manage rStation researchers.")
    sub=p.add_subparsers(dest="command",required=True)
    s=sub.add_parser("start",help="Start a new research group.")
    s.add_argument("problem");s.add_argument("--url",default="https://rstation.ai")
    s.add_argument("--name");s.add_argument("--codex",type=int,default=1);s.add_argument("--claude",type=int,default=1)
    s.add_argument("--codex-model",default="gpt-6.2-astra");s.add_argument("--claude-model",default="claude-opus-5-5")
    s.add_argument("--gemini",type=int,default=1)
    s.add_argument("--gemini-model",default="gemini-4-carbon")
    s.add_argument("--gemini-context-tokens",type=gemini_context_tokens,default=500000)
    s.add_argument("--subagents",type=int,default=0,help="Concurrent assistant slots per researcher (default: 0).")
    s.add_argument('--council',action=argparse.BooleanOptionalAction,default=True,help='Research council (default: on).')
    s.add_argument('--coder',action=argparse.BooleanOptionalAction,default=True,help='Fixed Codex coder (default: on).')
    s.add_argument('--coder-model',default='gpt-6.2-sol')
    s.add_argument("--invitation-codes-file",type=Path,help="Private JSON mapping worker names to problem invitation codes.")
    s.add_argument("--instructions-file",type=Path,help="Optional local resource notes appended to the startup prompt.")
    s.add_argument("--storage-gb",type=positive,default=50)
    s.add_argument("--gpus",type=gpu_list,default=[])
    s.add_argument("--gpu-per-agent",type=int,default=1)
    s.add_argument("--worker-gpus",action="append",default=[])
    s.add_argument("--gpu-pool-profiles",type=Path,help="Opt-in JSON worker-to-prepared-client mapping; default local reservations only.")
    s.add_argument("--gpu-coord-file",default="/tmp/station_gpu_used.json")
    s.add_argument("--gpu-job-seconds",type=positive,default=3600)
    s.add_argument("--idle-seconds",type=positive,default=1800)
    s.add_argument("--platform-proxy",help="Proxy for rStation access when this host cannot reach it directly.")
    s.add_argument("--research-proxy");s.add_argument("--model-api-proxy",default="direct",help="Proxy URL for model calls; defaults to direct.")
    s.add_argument("--disable-web",action="store_true",help="Disable research web tools and use isolated research instructions.")
    s.add_argument("--check",action="store_true",help="Validate service, resources and model connectivity without starting research.")
    s.add_argument('--supervisor',action='store_true',help='Explicitly add a GPT-6 Astra supervisor to this roster (default: no supervisor).')
    s.add_argument('--supervisor-invitations-file',type=Path,help='Private JSON mapping invite-only problem names to separate supervisor invitation codes.')
    supervise=sub.add_parser('supervise',help='Assign an optional supervisor to existing researchers.')
    supervise.add_argument('name')
    supervise.add_argument('--member',action='append',required=True,help='RUN:WORKER; repeat for each researcher.')
    supervise.add_argument('--supervisor-invitations-file',type=Path,help='Private JSON mapping invite-only problem names to separate supervisor invitation codes.')
    end=sub.add_parser('stop-supervision',help='End supervision and release proposal requirements, preserving researchers.')
    end.add_argument('name')
    for cmd in ("resume","attach","stop","status","logs","pause"):
        sp=sub.add_parser(cmd)
        sp.add_argument("name",nargs="?" if cmd=="status" else None)
        if cmd=="pause":sp.add_argument("worker")
        if cmd=="resume":
            sp.add_argument("--worker",help="Resume one paused worker in an active group.")
            sp.add_argument("--keep-experiments",action="store_true",help="Keep live GPU experiments when recovering a stopped controller.")
        if cmd=="status":
            sp.add_argument("--json",action="store_true")
            sp.add_argument("--watch",type=positive,help="Refresh every N seconds.")
        if cmd=="logs":
            sp.add_argument("worker")
            sp.add_argument("--lines",type=int,default=40)
    send=sub.add_parser("send",help="Send a message directly to a worker CLI.")
    send.add_argument("name");send.add_argument("worker",help="Worker name, or all.")
    send.add_argument("text",nargs="?")
    send.add_argument("--file",type=Path,help="Read the message from a text file.")
    send.add_argument('--owner',action='store_true',help='Explicitly identify an original or verified relayed owner instruction.')
    send.add_argument('--owner-reference',help='Owner instruction reference, such as desk:6:13328; requires --owner.')
    messages=sub.add_parser("messages",help="Show operator message delivery status.")
    messages.add_argument("name");messages.add_argument("worker",nargs="?",default="all")
    council=sub.add_parser('council',help='Configure optional research council consultations.')
    council.add_argument('action',choices=['enable','disable','status','resume','restore'])
    council.add_argument('--from-run',type=Path,help='Stopped source run or migration snapshot for restoring missing council state.')
    council.add_argument('name');council.add_argument('worker',nargs='?',default='all')
    council.add_argument('--models-file',type=Path,help='JSON family-to-model-list mapping; contains no credentials.')
    council.add_argument('--max-concurrent',type=int,help='Concurrent adviser turns per researcher (default 2).')
    council.add_argument('--max-pending',type=int,help='Unfinished turns per researcher (default 12).')
    council.add_argument('--turn-seconds',type=int,help='Wall-clock limit for one consultation turn including retries (default 1800).')
    council.add_argument('--attempt-seconds',type=int,help='Wall-clock limit per native attempt before retry/fallback (default 300).')
    council.add_argument('--max-attempts',type=int,help='Native launches per turn (default 16).')
    coder=sub.add_parser('coder',help='Configure the fixed Codex coder.')
    coder.add_argument('action',choices=['enable','disable','status','resume'])
    coder.add_argument('name');coder.add_argument('worker',nargs='?',default='all')
    coder.add_argument('--model')
    coder.add_argument('--turn-seconds',type=int)
    coder.add_argument('--attempt-seconds',type=int)
    coder.add_argument('--max-attempts',type=int)

    pool=sub.add_parser("gpu-pool",help="Attach prepared cross-host clients without restarting workers.")
    pool.add_argument("name")
    choice=pool.add_mutually_exclusive_group(required=True)
    choice.add_argument("--profiles",type=Path);choice.add_argument("--disable",action="store_true")
    c=sub.add_parser("_controller");c.add_argument("run");c.add_argument("--keep-experiments",action="store_true")
    g=sub.add_parser("_gpu")
    g.add_argument("run");g.add_argument("worker");g.add_argument("--count",type=int,default=1)
    g.add_argument("--timeout",type=positive)
    return p

def main(argv=None):
    os.umask(0o077)
    argv=list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0]=='providers':
        from provider_control import cli
        try: cli(argv[1:])
        except (ValueError, OSError, KeyError, TypeError):
            raise SystemExit('Provider command failed; no credentials are shown. Check the private input, controller account and current status.') from None
        return
    if argv and argv[0]=='worker':
        from worker_control import cli
        try: cli(argv[1:])
        except (ValueError, OSError) as exc: raise SystemExit(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        return
    if argv and argv[0]=='restart-timer':
        from restart_timer import cli
        try: cli(argv[1:])
        except (ValueError, OSError) as exc: raise SystemExit(str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        return
    if argv and argv[0]=='_coder':
        from coder import worker_cli
        try:worker_cli(Path(argv[1]),argv[2],argv[3:])
        except (ValueError,OSError) as exc:print(str(exc),file=sys.stderr);raise SystemExit(1)
        return
    if argv and argv[0]=='_council':
        from council import worker_cli
        try:worker_cli(argv[1],argv[2],argv[3:])
        except (ValueError,OSError) as exc:
            print(str(exc) if isinstance(exc,ValueError) else 'Council storage unavailable; retry later.',file=sys.stderr)
            raise SystemExit(1)
        return
    if argv and argv[0]=="_pool":
        from pool import worker_cli
        try:raise SystemExit(worker_cli(argv[1],argv[2],argv[3:]))
        except (ValueError,OSError) as exc:raise SystemExit(str(exc))
    experiment=[]
    if argv and argv[0]=="_gpu" and "--" in argv:
        split=argv.index("--");experiment=argv[split+1:];argv=argv[:split]
    args=parser().parse_args(argv)
    try:
        if args.command=='gpu-pool':
            from pool import configure
            print(json.dumps({'enabled_workers':configure(run_dir(args.name),args.profiles)}));return
        if args.command=='coder':
            from coder import Store,configure,start_service
            run=run_dir(args.name);env=safe_environment()
            if args.action in {'enable','disable'}:
                configure(run,args.worker,args.action=='enable',env,model=args.model,
                          limits={k:getattr(args,k) for k in ('turn_seconds','attempt_seconds','max_attempts')})
            if args.action in {'enable','resume'}:
                if not Store(run).policy().get('enabled'):raise ValueError('Coder is disabled.')
                start_service(run,env)
            from capabilities import health
            print(json.dumps(health(run,'coder'),indent=2));return
        if args.command=='council':
            from council import Store, configure, start_service
            run=run_dir(args.name);store=Store(run)
            if read(run/'run.json',{}).get('role')=='supervisor':raise ValueError('Supervisors do not use council.')
            if args.action=='status':
                from capabilities import health
                print(json.dumps({**{k:v for k,v in store.policy().items() if k!='statement'},**health(run,'council')},indent=2));return
            if args.action=='restore':
                from capabilities import restore
                if not args.from_run:raise ValueError('Provide --from-run with the saved source run.')
                restore(run,'council',args.from_run)
            env=safe_environment()
            if args.action in {'enable','disable'}:
                configure(run,args.worker,args.action=='enable',env,
                    models=json.loads(args.models_file.read_text()) if args.models_file else None,
                    limits={k:getattr(args,k) for k in ('max_concurrent','max_pending','turn_seconds','attempt_seconds','max_attempts')})
            if args.action in {'enable','resume','restore'}:
                if not store.policy().get('enabled'):raise ValueError('Council is disabled.')
                start_service(run,env)
            print('Council '+args.action+' complete. Existing research processes were not restarted.');return
        if args.command=='supervise':
            from supervision import create_group
            create_group(args.name,args.member,safe_environment(),invitation_file=args.supervisor_invitations_file);return
        if args.command=='stop-supervision':
            from supervision import end_group
            end_group(args.name);return
        if args.command=="_controller":controller(args.run,args.keep_experiments);return
        if args.command=="_gpu":
            from gpu import run_gpu
            command=experiment
            raise SystemExit(run_gpu(args.run,args.worker,args.count,command,args.timeout))
        if args.command=="start":
            info=problem_info(url(args.url),args.problem,args.disable_web,args.platform_proxy) if NAME.fullmatch(args.problem) else {}
            config=build_config(args,info);run=run_dir(config["name"])
            if run.exists():raise ValueError("Run name already exists. Use resume or choose --name.")
            if args.supervisor_invitations_file and not args.supervisor:
                raise ValueError('--supervisor-invitations-file requires --supervisor.')
            invitations = None
            if args.supervisor:
                from supervision import supervisor_invitations
                invitations = supervisor_invitations([config], args.supervisor_invitations_file,
                                                     problem_details={config['problem']:info})
            env=safe_environment();preflight(config,env)
            if args.supervisor and not any(w['kind']=='codex' and w['model']=='gpt-6-astra' for w in config['workers']):
                preflight({**config,'workers':[dict(name='supervisor',kind='codex',model='gpt-6-astra',gpus=[])]},env)
            if args.check:print("All startup checks passed. No research agents registered or launched.");return
            run.mkdir(parents=True,mode=0o700,exist_ok=False)
            save(run/"run.json",config);prepare_workspaces(run,config)
            if config.get('council'):
                from council import configure, start_service
                configure(run,'all',True,env,notify=False);start_service(run,env)
            if config.get('coder'):
                from coder import configure,start_service
                configure(run,'all',True,env,notify=False);start_service(run,env)
            start_controller(run,config,env)
            if args.supervisor:
                from supervision import create_group
                create_group(config['name'],[config['name']+':'+w['name'] for w in config['workers']],env,check=False,invitations=invitations)
            return
        if args.command=="status":
            root=Path(os.environ.get("RSTATION_AGENT_DATA",str(ROOT/"agent_data")))
            while True:
                selected=[run_dir(args.name)] if args.name else sorted(root.glob("*"))
                show_status(selected,as_json=args.json)
                if not args.watch:return
                time.sleep(args.watch)
        if args.command=="logs":
            show_logs(run_dir(args.name),args.worker,args.lines);return
        run=run_dir(args.name)
        config=read(run/"run.json")
        if not config:raise ValueError("Run not found.")
        if args.command in {"send","messages"}:
            from messages import Messages
            workers=[w for w in config["workers"] if args.worker in {"all",w["name"]}]
            if not workers:raise ValueError("Worker not found.")
            if args.command=="send":
                if bool(args.text is not None)==bool(args.file):
                    raise ValueError("Provide message text or --file, once.")
                text=args.file.read_text() if args.file else args.text
                if not text.strip():raise ValueError("The message is empty.")
                if not config.get("live_input",False):
                    raise ValueError("Resume this run with the updated toolkit to enable CLI messages.")
                if args.owner != bool(args.owner_reference):
                    raise ValueError('Use --owner and --owner-reference together.')
                for w in workers:
                    number=Messages(run/w["name"]).add(text, owner_reference=args.owner_reference)
                    print(f"{w['name']}: message {number} queued. Check with agents messages {args.name} {w['name']}.")
            else:
                for w in workers:
                    rows=Messages(run/w["name"]).list()
                    print(w["name"])
                    for row in rows:
                        print(f"  {row['id']:>4}  {row['status']:10}  {row['queued_at']}  {' '.join(row['text'].split())[:100]}")
                    if not rows:print("  No operator messages.")
            return
        if args.command=="pause":
            if args.worker not in {w["name"] for w in config["workers"]}:raise ValueError("Worker not found.")
            (run/args.worker/".runtime/PAUSE").touch()
            print("Pause requested; the workspace and conversation are preserved.")
        elif args.command=="stop":stop(run)
        elif args.command=="attach":
            tmux_views(run,config)
            os.execvp("tmux",["tmux","attach-session","-t",config["tmux"]])
        elif args.command=="resume":
            if args.worker:
                ctl=read(run/"controller.json",{})
                if not alive(ctl.get("pid"),ctl.get("token")):raise ValueError("Resume the group first, then resume this worker.")
                if args.worker not in {w["name"] for w in config["workers"]}:raise ValueError("Worker not found.")
                (run/args.worker/".runtime/PAUSE").unlink(missing_ok=True)
                print("Worker resume requested.")
                return
            ctl=read(run/"controller.json",{})
            if alive(ctl.get("pid"),ctl.get("token")):raise ValueError("This run is already active.")
            info=problem_info(config["url"],config["problem"],config.get("disable_web",False),config.get("platform_proxy"))
            config["category"]=info["category"]
            config["live_input"]=True
            from worker_control import preflight_resume
            env=safe_environment();preflight_resume(run,config,env,preflight)
            save(run/"run.json",config);prepare_workspaces(run,config);start_controller(run,config,env,args.keep_experiments)
            from capabilities import ensure_services
            ensure_services(run,env)
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        # Specific configuration errors are written without credential values.
        print(str(exc) if isinstance(exc,ValueError) else f"Operation failed ({type(exc).__name__}); inspect private logs.",file=sys.stderr)
        raise SystemExit(1)

if __name__=="__main__":main()

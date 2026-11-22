"""Shared private launcher configuration and durable local state."""
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "vendor"))
NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")

def save(path, value, *, owner=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            if owner is not None: os.fchown(out.fileno(), *owner)
            json.dump(value, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def read(path, default=None):
    try: return json.loads(Path(path).read_text())
    except FileNotFoundError: return default

def lock(path, nonblocking=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    f = path.open("a+")
    try: fcntl.flock(f, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
    except BaseException:
        f.close()
        raise
    return f

def pid_token(pid):
    try:
        # Field 22, after the parenthesized process name (which may contain spaces).
        fields = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")",1)[1].split()
        # An exited child can retain its PID until its parent reaps it.
        if fields[0] in {"Z", "X"}: return None
        return fields[19]
    except (OSError, ValueError, IndexError): return None

def alive(pid, token):
    return bool(pid and token and pid_token(pid) == token)

def url(value, label="URL"):
    try:
        p = urlsplit(value)
        if p.scheme not in {"http","https"} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError()
        p.port
    except ValueError:
        raise ValueError(f"{label} must be an HTTP(S) URL without credentials, query or fragment.") from None
    return value.rstrip("/")

def fetch(base, path, proxy=None):
    # Platform traffic is direct unless the operator configures its proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http":proxy,"https":proxy} if proxy else {}))
    with opener.open(base + path, timeout=20) as response:
        data = json.load(response)
    if data.get("success") is not True or not isinstance(data.get("data"), dict):
        raise ValueError("Unexpected rStation response.")
    return data["data"]

def fetch_guide(base, proxy=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http":proxy,"https":proxy} if proxy else {}))
    with opener.open(base + "/skill.md", timeout=20) as response:
        guide = response.read().decode("utf-8")
    if "# rStation\n" not in guide:
        raise ValueError("Unexpected rStation guide response.")
    return guide


def problem_info(base, problem, disable_web=False, proxy=None):
    guide = fetch_guide(base,proxy) if proxy else fetch_guide(base)
    isolated = any(line.startswith("**Isolated research setting:") for line in guide.splitlines())
    if isolated is not disable_web:
        raise ValueError("Worker web mode must match the instance: use --disable-web for an isolated instance.")
    info = fetch(base, "/api/v1/problems/" + problem,proxy) if proxy else fetch(base, "/api/v1/problems/" + problem)
    if info.get("category") not in {"open_ended", "closed_ended"}:
        raise ValueError("Unknown problem category.")
    if info.get("archived"): raise ValueError("This problem is archived.")
    return info

def workspace_bytes(path):
    total = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base)/d).is_symlink()]
        for name in files:
            try:
                st = (Path(base)/name).lstat()
                if not (Path(base)/name).is_symlink(): total += st.st_size
            except FileNotFoundError: pass
    return total

def initial_prompt(config, worker, work):
    if config.get('replacement_first_instruction'):
        ordinary = dict(config)
        first = ordinary.pop('replacement_first_instruction')
        return first + '\n\n' + initial_prompt(ordinary, worker, work)
    if config.get('role') == 'supervisor':
        from supervision import supervisor_prompt
        return supervisor_prompt(config, Path(work))
    prompt = (f"You are researching r/{config['problem']} on {config['url']}. "
              "The problem statement defines what counts as a result. "
              f"First read {Path(work)/'AGENTS.md'}, then obtain access and read the full problem statement.")
    if worker.get('invitation_code'):
        payload=json.dumps({'invitation_code':worker['invitation_code']})
        prompt += (f"\nYour operator has invited you to r/{config['problem']}. Include invitation_code in registration, "
                   f"or use your existing account to POST /api/v1/agents/me/invitations with {payload}. Keep this code private.")
    return prompt


def researcher_instructions(config, worker, work):
    """Persistent workspace guidance; initial and resumed turns stay brief."""
    ending = (
        "Continue researching until the operator stops you. Completing one research direction does not end your assignment."
        if config["category"] == "open_ended" else
        "Continue until the problem is solved or the operator stops you. If you believe it is solved, submit the result for review and address any remaining verification issues."
    )
    prompt = f"""# Research assignment

You are a researcher working on r/{config['problem']} on {config['url']}. The problem statement defines the scientific requirements and your scientific goal.

## Worker context policy

**Every rStation worker must have a context window of at least 300,000 tokens. Only the human user can explicitly approve a reduction, including a temporary reduction or a change from a larger window to 300,000. Agents cannot grant this approval.**

During an API outage, keep the configured context window and wait for service to recover. Never reduce context or token usage as an outage workaround. This rule applies to launches, retries, provider switches, and recovery overrides.

## Account and access

1. Read {config['url']}/skill.md. Reuse the recorded account for this workspace; create one only if none exists or the operator has explicitly assigned a new account to this successor. Complete registration before reading discussions or papers. Keep invitation codes private.
2. Store credentials at {work}/.rstation/credentials.json and record the handle in IDENTITY.md. Tell the human operator if registration or access is blocked.
3. Confirm access to r/{config['problem']} and read its full task statement before starting research.

## Working continuously

After a context refresh, reread the rStation Research Guide, IDENTITY.md and PLAN.md before continuing. Follow all research practices specified by rStation.

Work toward the main research goal, participate in discussions and publish substantial findings. While a paper is under review or advice is pending, continue useful work. Follow rStation's check-in, holiday and meta-reflection guidance. {ending}
Run long jobs in the background, return from the current turn and check them later; avoid long foreground sleeps that delay incoming messages. Operator messages arrive through your private CLI inbox.

## Workspace

Your persistent workspace is {work}. Keep notes, code, results and a concise CONTINUATION.md here for recovery. Keep total storage within {config['storage_gb']:g} GB, including temporary files and caches. Remove disposable outputs while preserving important findings; STORAGE_USAGE.txt tracks usage.
"""
    if config.get("subagents",0):
        from subagents import researcher_guidance
        prompt += "\n"+researcher_guidance(config["subagents"],worker["kind"])+"\n"
    from capabilities import reminder
    skills=reminder(config,worker,work)
    if skills:prompt += '\n## Available skills\n\n'+skills+'\n'
    prompt += "\n## Resources and access\n"
    if worker["gpus"] and worker["name"] not in config.get("gpu_pool",{}):
        prompt += f"""
GPU access: you may reserve at most {min(config['gpu_per_agent'],len(worker['gpus']))} GPU(s) at a time from your shared pool {worker['gpus']}. All GPUs in this pool are shared with other researchers; none is assigned to you unless the operator explicitly specifies otherwise. Use the full configured pool and do not pin GPUs or create a wrapper that narrows it. Run every GPU experiment with `./gpu-run -- python your_experiment.py`. {("(use --count N for multiple GPUs)." if min(config["gpu_per_agent"],len(worker["gpus"])) > 1 else "")} The helper waits for available GPUs and releases them when the experiment exits. Reserve GPUs only when ready to compute, and stop the experiment to release them as soon as they are no longer needed. Do not hold GPUs while planning, reading or discussing. The per-experiment time limit is {config['gpu_job_seconds']} seconds; your overall research session has no time limit. Run long experiments in the background with output saved to a file so you can keep checking the community.
"""
    elif not worker["gpus"]:
        prompt += "\nGPU access is disabled for this worker. Use CPU experiments.\n"
    from pool import guidance as pool_guidance
    prompt += pool_guidance(config,worker)
    if config.get("disable_web"):
        prompt += "\nThis is an isolated research run. Do not use external websites, search, downloads, or previous runs and their artifacts. Use this rStation instance, your current workspace and preinstalled libraries.\n"
    if config.get("research_proxy"):
        prompt += f"\nFor external web access and downloads use {config['research_proxy']}. Your shell proxy environment is configured.\n"
    if config.get("platform_proxy"):
        prompt += f"\nUse the configured shell proxy for requests to {config['url']} as well.\n"
    if config.get("instructions"):
        prompt += "\n## Existing task notes\n\n"+config["instructions"].strip()+"\n"
    return prompt

def worker_environment(values):
    """Normalize public provider settings without persisting credentials."""
    result = dict(values)
    for family in ('CODEX', 'CLAUDE', 'GEMINI'):
        for backup in ('', 'BACKUP_'):
            for field in ('API_KEY', 'BASE_URL', 'HTTP_PROXY', 'HTTPS_PROXY'):
                short = family + '_' + backup + field
                legacy = 'RSTATION_WORKER_' + short
                if short in values:
                    result[legacy] = result.pop(short)
    return result


def safe_environment():
    from dotenv import dotenv_values
    defaults = dotenv_values(ROOT/".env") if (ROOT/".env").exists() and not os.environ.get("RSTATION_MANAGED_ENVIRONMENT") else {}
    env = worker_environment({k: v for k, v in defaults.items() if v is not None})
    env.update(worker_environment(os.environ))
    return env

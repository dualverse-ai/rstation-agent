"""Restart enrolled research controllers; credentials stay in process memory.

Only a running/explicitly launched run is enrolled. Explicit STOP exits
are terminal. Paused workers, saved conversations and live experiments survive.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from common import ROOT, alive, lock, pid_token, read, save
from live_watchdog import descendants, group_has_work, process


def code_revision():
    digest = hashlib.sha256()
    for name in ('main.py', 'recovery.py', 'worker_control.py', 'live_cli.py', 'controller_guard.py', 'capabilities.py', 'coder.py', 'coder_service.py', 'coder_workspace.py', 'council.py', 'council_service.py', 'antigravity.py'):
        digest.update((ROOT/name).read_bytes())
    return digest.hexdigest()[:16]


def pending_tools(run, worker, state):
    """Check the frozen CLI's journal, including tools with no OS child."""
    from types import SimpleNamespace
    from recovery import ResearchManager
    observer = SimpleNamespace(pending_tools={})
    path = run/worker['name']/'.runtime'/f"launch_{state.get('launches', 0):06d}"/'transcript.jsonl'
    if not path.exists():
        return True  # Cannot establish a safe checkpoint for a live CLI.
    large = path.stat().st_size > 32*1024*1024
    with path.open('rb') as stream:
        if large:
            stream.seek(-4*1024*1024, 2)
            stream.readline()  # Drop the possibly incomplete first event.
        lines = stream.readlines()
    anchored = not large
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            return True
        if isinstance(event, dict):
            # A completed turn bounds the history relevant to pending tools.
            # Descendant and process-group checks still protect background work.
            if event.get('type') == 'turn.completed':
                observer.pending_tools = {}
                anchored = True
            if anchored:
                ResearchManager._record_pending_tools(observer, worker['name'], event, worker['kind'])
    return not anchored or bool(observer.pending_tools.get(worker['name']))


def native_child(info):
    args = info['args']
    if not args:
        return False
    name = Path(args[0]).name
    if name == 'node':
        paths = [str(Path(a).resolve()) for a in args[1:3] if a.startswith('/')]
        return any('/@openai/codex/' in a or '/@anthropic-ai/claude-code/' in a for a in paths)
    return name in {'codex', 'codex-code-mode-host', 'agy', 'claude'}


def stopped(pid, token):
    deadline = time.monotonic()+1
    while alive(pid, token) and time.monotonic() < deadline:
        if process(pid)['state'] in {'T', 't'}:
            return True
        time.sleep(.01)
    return False


def reclaim_idle_clis(run, config):
    """Fence orphan CLIs together, and never signal a live research tool."""
    frozen = []
    try:
        for worker in config['workers']:
            state = read(run/worker['name']/'.runtime/state.json', {})
            pid = state.get('pid')
            if not alive(pid, state.get('pid_token')):
                continue
            if os.getpgid(pid) != pid:
                return False
            os.killpg(pid, signal.SIGSTOP)
            frozen.append((pid, state['pid_token']))
            if not stopped(pid, state['pid_token']):
                return False
            children = descendants(pid)
            if any(not native_child(p) for p in children.values()):
                return False
            groups = {pid}
            # Codex starts its idle code-mode host in a separate process group.
            # Fence that host too, then inspect again for tools it could spawn.
            for p in children.values():
                if p['group'] in groups:
                    continue
                if Path(p['args'][0]).name != 'codex-code-mode-host' or p['group'] != p['pid']:
                    return False
                os.killpg(p['pid'], signal.SIGSTOP)
                frozen.append((p['pid'], p['start']))
                groups.add(p['pid'])
            if any(not stopped(p['pid'], p['start']) for p in children.values()):
                return False
            current = descendants(pid)
            if set(current) != set(children) or any(current[p]['start'] != children[p]['start'] for p in children):
                return False
            if any(group_has_work(group, {pid, *children}) for group in groups):
                return False
            if pending_tools(run, worker, state):
                return False
        from gpu import stop_group
        for pid, token in frozen:
            if alive(pid, token):
                stop_group(pid)
        return True
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    finally:
        for pid, token in frozen:
            try:
                # Group members may outlive their leader; a reused PID must
                # never receive the signal.
                if alive(pid, token):
                    os.killpg(pid, signal.SIGCONT)
            except ProcessLookupError:
                pass


def reload_when_idle(run, ctl):
    """Replace old Python code without running its destructive shutdown path."""
    pid, token = ctl.get('pid'), ctl.get('token')
    if not alive(pid, token):
        return False
    os.kill(pid, signal.SIGSTOP)
    try:
        if not stopped(pid, token) or (run/'STOP').exists():
            return False
        if not reclaim_idle_clis(run, read(run/'run.json', {})):
            return False
        os.kill(pid, signal.SIGKILL)
        return True
    finally:
        if alive(pid, token):
            os.kill(pid, signal.SIGCONT)


class ControllerGuard:
    def __init__(self, run, env, command=None):
        self.run = Path(run)
        self.env = dict(env)
        self.command = command or [sys.executable, str(ROOT/'main.py'), '_controller', str(self.run), '--keep-experiments']
        self.child = None
        self.next_start = 0
        self.failures = 0
        self.healthy_since = None
        self.next_reload_check = 0
        self.revision = code_revision()

    def tick(self):
        now = time.time()
        ctl = read(self.run/'controller.json', {})
        if (self.run/'STOP').exists():
            return False
        if self.child is not None and self.child.poll() is not None:
            self.child = None  # Reap an exited child, never a different process.
        running = alive(ctl.get('pid'), ctl.get('token'))
        upgrade = read(self.run/'controller-upgrade.json', {})
        if upgrade and running:
            # Recovery launches the installed code, which may supersede an older
            # queued upgrade. Never keep killing that newer controller to chase
            # a revision that is no longer present on disk.
            target_revision = code_revision()
            if ctl.get('code_revision') == target_revision:
                (self.run/'controller-upgrade.json').unlink(missing_ok=True)
            elif now >= self.next_reload_check:
                self.next_reload_check = now+30
                if reload_when_idle(self.run, ctl):
                    running = False
        state = dict(pid=os.getpid(), token=pid_token(os.getpid()), heartbeat=now,
                     status='watching' if running else 'waiting', controller_pid=ctl.get('pid'),
                     attempts=self.failures, code_revision=self.revision)
        if running:
            if self.healthy_since is None:
                self.healthy_since = now
            if now-self.healthy_since > 120:
                self.failures = 0
            receipt = read(self.run/'controller-recovery.json', {})
            if receipt.get('status') == 'awaiting_response':
                config = read(self.run/'run.json', {})
                states = [read(self.run/w['name']/'.runtime/state.json', {})
                          for w in config.get('workers', [])
                          if not (self.run/w['name']/'.runtime/PAUSE').exists()]
                if states and all(min(s.get('last_response') or 0, s.get('last_tool_completed') or 0) > receipt['at'] for s in states):
                    receipt.update(status='recovered', verified_at=now)
                    save(self.run/'controller-recovery.json', receipt)
        elif self.child is None and now >= self.next_start:
            self.healthy_since = None
            # One controller.lock also arbitrates explicit resume/adoption races.
            try:
                handle = lock(self.run/'controller.lock', True)
            except BlockingIOError:
                save(self.run/'guardian.json', state)
                return True
            handle.close()
            if (self.run/'STOP').exists():
                return False
            self.failures += 1
            self.next_start = now+min(300, 5*2**min(self.failures-1, 6))
            with (self.run/'controller.log').open('ab') as log:
                self.child = subprocess.Popen(self.command, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log, start_new_session=True, close_fds=True)
            save(self.run/'controller-recovery.json', dict(at=now, status='awaiting_response',
                 previous_controller=ctl.get('pid'), previous_reason=ctl.get('reason'), pid=self.child.pid))
            state.update(status='restarting', attempts=self.failures, next_start=self.next_start)
        save(self.run/'guardian.json', state)
        return True


def ensure_guardian(run, env):
    """Enrol only an already active run, without restarting any researcher."""
    run = Path(run).resolve()
    with lock(run/'guardian-start.lock'):
        ctl = read(run/'controller.json', {})
        if (run/'STOP').exists() or not alive(ctl.get('pid'), ctl.get('token')):
            return None
        guard = read(run/'guardian.json', {})
        if alive(guard.get('pid'), guard.get('token')):
            return None
        with (run/'guardian.log').open('ab') as log:
            child = subprocess.Popen([sys.executable, str(ROOT/'controller_guard.py'), str(run)],
                env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                start_new_session=True, close_fds=True)
        deadline = time.monotonic()+10
        while time.monotonic() < deadline:
            guard = read(run/'guardian.json', {})
            if guard.get('pid') == child.pid and alive(child.pid, guard.get('token')):
                return child
            if child.poll() is not None:
                raise RuntimeError('Controller guardian failed to start; inspect guardian.log')
            time.sleep(.05)
        raise RuntimeError('Controller guardian startup timed out')


def main():
    run = Path(sys.argv[1]).resolve()
    stopping = False
    def halt(*_):
        nonlocal stopping
        stopping = True
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, halt)
    with lock(run/'guardian.lock', True):
        service = ControllerGuard(run, os.environ)
        try:
            while not stopping:
                try:
                    if not service.tick():
                        break
                except (OSError, ValueError) as exc:
                    # No exception text/locals: they can contain API credentials.
                    print('Guardian state unavailable: '+type(exc).__name__, flush=True)
                time.sleep(2)
        finally:
            save(run/'guardian.json', dict(pid=None, token=None, status='stopped'))


if __name__ == '__main__':
    main()

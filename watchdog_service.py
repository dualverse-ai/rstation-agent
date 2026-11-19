"""Host watchdog for existing workers, including CLIs started before an upgrade.

Run as the research account: python watchdog_service.py /path/to/agent_data
Only native CLIs or positively identified blocked tools are signalled. Controllers,
conversations, workspaces, registered GPU jobs and intentional pauses are retained.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from common import alive, lock, read, save
from live_watchdog import NativeWatchdog, descendants, group_has_work, process


class PoolWatchdog:
    def __init__(self, root):
        self.root = Path(root)
        self.monitors = {}

    def tick(self):
        active = set()
        actions = []
        for state_path in self.root.glob('*/*/.runtime/state.json'):
            work = state_path.parent.parent
            run = work.parent
            if (run/'STOP').exists() or (state_path.parent/'PAUSE').exists():
                continue
            state = read(state_path, {})
            receipt_path = state_path.parent/'watchdog-recovery.json'
            receipt = read(receipt_path, {})
            if receipt.get('status') == 'awaiting_response' and (state.get('last_response') or 0) > receipt['at']:
                receipt.update(status='recovered', response_at=state['last_response'])
                save(receipt_path, receipt)
            config = read(run/'run.json', {})
            controller = read(run/'controller.json', {})
            if state.get('status') != 'running' or not alive(controller.get('pid'), controller.get('token')):
                continue
            worker = next((w for w in config.get('workers', []) if w['name'] == work.name), {})
            if worker.get('kind') not in {'claude', 'gemini'}:
                continue
            native = read(state_path.parent/'live_input.json', {})
            wrapper = state.get('pid')
            if not alive(wrapper, state.get('pid_token')):
                continue
            try:
                if native:
                    pid = native.get('pid')
                    if not alive(pid, native.get('token')):
                        continue
                    info = process(pid)
                    if info['parent'] != wrapper or os.getpgid(pid) != os.getpgid(wrapper):
                        continue
                    started = (state_path.parent/'live_input.json').stat().st_mtime
                else:
                    # Legacy runs launch the native CLI directly.
                    pid = wrapper
                    info = process(pid)
                    if (info['parent'] != controller['pid'] or not info['args']
                            or Path(info['args'][0]).name not in {'claude', 'agy'}):
                        continue
                    started = state.get('started') or state_path.stat().st_mtime
                key = (str(work.resolve()), pid, info['start'])
                active.add(key)
                progress = max(state.get('last_response') or 0, state.get('last_tool_completed') or 0,
                               started)
                if key not in self.monitors:
                    monitor = NativeWatchdog(config.get('idle_seconds', 1800))
                    monitor.last_activity -= max(0, time.time() - progress)
                    self.monitors[key] = (monitor, progress)
                monitor, old_progress = self.monitors[key]
                if progress > old_progress:
                    monitor.activity()
                    self.monitors[key] = (monitor, progress)
                waiting = False
                if config.get('role') == 'supervisor':
                    from supervision import sleeping
                    waiting = sleeping(work)
                result = monitor.check(pid, wrapper_pid=wrapper, waiting=waiting)
                if not result:
                    continue
                if result['action'] == 'resume_native':
                    # Stop just the native process, check again for newly spawned
                    # or reparented work, then let its launcher resume normally.
                    os.kill(pid, signal.SIGSTOP)
                    try:
                        current = process(pid)
                        if (current['start'] != info['start'] or descendants(pid)
                                or group_has_work(info['group'], {wrapper, pid})):
                            continue
                        os.kill(pid, signal.SIGTERM)
                    finally:
                        try: os.kill(pid, signal.SIGCONT)
                        except ProcessLookupError: pass
                    monitor.activity()
                actions.append(dict(worker=str(work), at=time.time(), status='awaiting_response', **result))
                save(state_path.parent/'watchdog-recovery.json', actions[-1])
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
        self.monitors = {k: v for k, v in self.monitors.items() if k in active}
        return actions


def ensure_watchdog(root):
    """Controllers keep one guard alive; the service lock arbitrates races."""
    root = Path(root).resolve()
    try:
        handle = lock(root/'.maintenance-watchdog.lock', True)
    except BlockingIOError:
        return None
    handle.close()
    with (root/'.maintenance-watchdog.log').open('ab') as log:
        return subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(root)],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True,
                                env={k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ})


def main():
    root = Path(sys.argv[1]).resolve()
    if not root.is_dir():
        raise ValueError('Agent data root does not exist')
    with lock(root/'.maintenance-watchdog.lock', True):
        service = PoolWatchdog(root)
        while True:
            actions = service.tick()
            for action in actions:
                print(json.dumps(action), flush=True)
            save(root/'.maintenance-watchdog.json', dict(pid=os.getpid(), heartbeat=time.time(),
                 watched=len(service.monitors), actions=actions))
            time.sleep(10)


if __name__ == '__main__':
    main()

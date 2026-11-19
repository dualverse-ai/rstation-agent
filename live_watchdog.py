"""Conservative recovery for unattended native CLIs and known blocked shell tools."""
import os
from pathlib import Path
import re
import shlex
import signal
import time


def process(pid):
    root = Path('/proc') / str(pid)
    fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
    args = (root / 'cmdline').read_bytes().split(b'\0')
    return dict(pid=pid, parent=int(fields[1]), group=int(fields[2]), state=fields[0],
                cpu=int(fields[11]) + int(fields[12]), start=fields[19],
                args=[a.decode(errors='replace') for a in args if a])


def descendants(pid):
    result = {}
    pending = [pid]
    while pending:
        parent = pending.pop()
        try:
            for task in (Path('/proc') / str(parent) / 'task').iterdir():
                for child in (task / 'children').read_text().split():
                    child = int(child)
                    if child in result:
                        continue
                    info = process(child)
                    if info['state'] not in {'Z', 'X'}:
                        result[child] = info
                        pending.append(child)
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def poll_pattern(args):
    """Recognize only a while-pgrep loop whose regex matches its own shell."""
    if not args or Path(args[0]).name not in {'bash', 'sh', 'dash'} or '-c' not in args:
        return None
    script = args[args.index('-c') + 1]
    try:
        words = shlex.split(script)
        if 'eval' in words:
            script = words[words.index('eval') + 1]
        match = re.search(r'''while\s+pgrep\s+-f\s+('[^']*'|"[^"]*"|[^\s;]+)''', script)
        if not match:
            return None
        pattern = shlex.split(match[1])[0]
        if re.search(pattern, ' '.join(args)):
            return pattern
    except (ValueError, IndexError, re.error):
        pass
    return None


def other_match(pattern, excluded):
    for path in Path('/proc').iterdir():
        if not path.name.isdigit() or int(path.name) in excluded:
            continue
        try:
            info = process(int(path.name))
            if info['state'] not in {'Z', 'X'} and re.search(pattern, ' '.join(info['args'])):
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False


def group_has_work(group, excluded):
    # Include background experiments reparented away from the native CLI.
    for path in Path('/proc').iterdir():
        if not path.name.isdigit() or int(path.name) in excluded:
            continue
        try:
            info = process(int(path.name))
            if info['group'] == group and info['state'] not in {'Z', 'X'}:
                return True
        except (FileNotFoundError, ProcessLookupError):
            continue
    return False


class NativeWatchdog:
    def __init__(self, idle_seconds=1800, tool_seconds=300):
        self.idle_seconds = idle_seconds
        self.tool_seconds = tool_seconds
        self.last_activity = time.monotonic()
        self.next_check = 0
        self.candidates = {}

    def activity(self):
        self.last_activity = time.monotonic()

    def check(self, native_pid, *, waiting=False, wrapper_pid=None, missing_replay=False):
        now = time.monotonic()
        if now < self.next_check or self.idle_seconds <= 0:
            return None
        self.next_check = now + 10
        try:
            children = descendants(native_pid)
            seen = set()
            for pid, info in children.items():
                args = info['args']
                pattern = poll_pattern(args)
                bare_cat = len(args) == 1 and Path(args[0]).name == 'cat'
                if not pattern and not bare_cat:
                    continue
                seen.add(pid)
                previous = self.candidates.get(pid)
                # A polling shell uses a small amount of CPU on every loop;
                # advancing CPU disqualifies cat, but not the exact self-match.
                if not previous or previous[0] != info['start'] or (bare_cat and previous[1] != info['cpu']):
                    self.candidates[pid] = (info['start'], info['cpu'], now)
                    continue
                if now - previous[2] < self.tool_seconds:
                    continue
                nested = descendants(pid)
                if bare_cat:
                    if nested:
                        continue
                    fd = os.readlink(f'/proc/{pid}/fd/0')
                    if not (fd.startswith('/dev/pts/') or fd == os.readlink(f'/proc/{native_pid}/fd/0')):
                        continue
                    reason = 'stdin-only cat made no progress'
                else:
                    if any(not p['args'] or Path(p['args'][0]).name not in {'sleep', 'pgrep'} for p in nested.values()):
                        continue
                    if other_match(pattern, {pid, *nested}):
                        continue
                    reason = 'pgrep polling loop matches only itself'
                # Revalidate PID identity and command immediately before signal.
                current = process(pid)
                if current['start'] != info['start'] or current['args'] != args:
                    continue
                os.kill(pid, signal.SIGTERM)
                self.candidates.pop(pid, None)
                self.activity()
                return {'action': 'terminate_blocked_tool', 'pid': pid, 'reason': reason}
            self.candidates = {pid: v for pid, v in self.candidates.items() if pid in seen}
            # An input still unacknowledged after a successful result can block
            # the durable queue forever. Allow late output/replays to arrive,
            # then reuse the same conservative process-safety checks.
            quiet_seconds = min(self.idle_seconds, 60) if missing_replay else self.idle_seconds
            if waiting or children or now - self.last_activity < quiet_seconds:
                return None
            if group_has_work(os.getpgid(native_pid), {wrapper_pid or os.getpid(), native_pid}):
                return None
            reason = ('Claude completed without replaying pending input' if missing_replay
                      else 'native CLI silent without a live tool')
            return {'action': 'resume_native', 'reason': reason}
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            # Uncertain process ownership/liveness is not grounds for termination.
            return None

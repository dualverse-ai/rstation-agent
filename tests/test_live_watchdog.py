import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live_cli import LiveCLI
from live_watchdog import NativeWatchdog, descendants, poll_pattern


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.02)
    raise AssertionError('Fixture did not become ready')


def test_poll_pattern_handles_claude_eval():
    import shlex
    script = "while pgrep -f 'missing_trial_ABC.py' >/dev/null; do sleep 20; done"
    assert poll_pattern(['bash', '-c', 'source missing 2>/dev/null; eval ' + shlex.quote(script)]) == 'missing_trial_ABC.py'
    assert poll_pattern(['bash', '-c', "while pgrep -f '[p]ython' >/dev/null; do sleep 20; done"]) is None


@pytest.mark.parametrize('mode', ['stdin', 'self_poll', 'real_work', 'valid_poll', 'separate_pipe'])
def test_real_child_tools_recovery_and_preservation(tmp_path, monkeypatch, mode):
    import live_watchdog as mod
    monkeypatch.setattr(mod, 'group_has_work', lambda *args: False)
    marker = 'watchdog_real_trial_' + tmp_path.name
    if mode == 'stdin':
        script = "import subprocess,time;subprocess.Popen(['cat']);time.sleep(60)"
    elif mode == 'separate_pipe':
        script = "import subprocess,time;subprocess.Popen(['cat'],stdin=subprocess.PIPE);time.sleep(60)"
    elif mode == 'real_work':
        script = "import subprocess,time;subprocess.Popen(['sleep','60']);time.sleep(60)"
    else:
        shell = f"while pgrep -f '{marker}' >/dev/null; do sleep .1; done"
        script = 'import subprocess,time;'
        if mode == 'valid_poll':
            script += f"subprocess.Popen(['bash','-c','exec -a {marker} sleep 60']);"
        script += f"subprocess.Popen(['bash','-c',{shell!r}]);time.sleep(60)"
    fixture = tmp_path/'tool_fixture.py'
    fixture.write_text(script)
    p = subprocess.Popen([sys.executable, str(fixture)], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        wait_for(lambda: descendants(p.pid))
        w = NativeWatchdog(idle_seconds=.03, tool_seconds=.05)
        w.check(p.pid)
        time.sleep(.12)
        w.next_check = 0
        result = w.check(p.pid)
        if mode in {'stdin', 'self_poll'}:
            assert result['action'] == 'terminate_blocked_tool'
            assert result['pid'] != p.pid
            assert p.poll() is None
        else:
            assert result is None
    finally:
        os.killpg(p.pid, signal.SIGKILL);p.wait();p.stdin.close()


@pytest.mark.parametrize('kind', ['claude', 'gemini'])
def test_silent_live_native_recovers_with_mail_durable(tmp_path, monkeypatch, kind):
    import live_watchdog as mod
    monkeypatch.setattr(mod, 'group_has_work', lambda *args: False)
    import live_cli
    monkeypatch.setattr(live_cli, 'group_has_work', lambda *args: False)
    script = tmp_path/'native.py'
    script.write_text('import sys,time\nsys.stdin.readline()\ntime.sleep(60)\n')
    args = SimpleNamespace(workspace=str(tmp_path),kind=kind,model='test',prompt='Continue.',conversation='saved')
    args.command = [sys.executable, str(script), '--model', 'test'] + (['-p', 'Continue.'] if kind == 'gemini' else ['Continue.'])
    cli = LiveCLI(args)
    cli.messages.add('Important pending instruction')
    cli.watchdog = NativeWatchdog(.1)
    original = cli.watchdog.check
    def fast_check(*args, **kwargs):
        cli.watchdog.next_check = 0
        return original(*args, **kwargs)
    monkeypatch.setattr(cli.watchdog, 'check', fast_check)
    with pytest.raises(RuntimeError, match='Native CLI stalled'):
        cli.run()
    assert cli.child.poll() is not None
    cli.messages.recover()
    assert cli.messages.pending()[0]['text'] == 'Important pending instruction'


def test_supervisor_wait_and_permission_uncertainty_are_preserved(monkeypatch):
    import live_watchdog as mod
    monkeypatch.setattr(mod, 'descendants', lambda _: {})
    monkeypatch.setattr(mod, 'group_has_work', lambda *args: False)
    w = NativeWatchdog(.1);w.last_activity -= 10
    assert w.check(os.getpid(), waiting=True) is None
    w.next_check=0
    monkeypatch.setattr(mod, 'descendants', lambda _: (_ for _ in ()).throw(PermissionError()))
    assert w.check(os.getpid()) is None


def test_claude_completed_without_replay_resumes_with_queue_intact(tmp_path, monkeypatch):
    import live_watchdog as mod
    import live_cli
    monkeypatch.setattr(mod, 'group_has_work', lambda *args: False)
    monkeypatch.setattr(live_cli, 'group_has_work', lambda *args: False)
    script = tmp_path/'native.py'
    script.write_text('import sys,json,time\nsys.stdin.readline()\n'
                      'print(json.dumps({"type":"result","subtype":"success","is_error":False}),flush=True)\n'
                      'time.sleep(60)\n')
    args = SimpleNamespace(workspace=str(tmp_path), kind='claude', model='test',
                           prompt='Continue.', conversation='saved')
    args.command = [sys.executable, str(script), '--model', 'test', 'Continue.']
    cli = LiveCLI(args)
    cli.messages.add('Pending supervisor instruction')
    cli.messages.add('Later operator instruction')
    original = cli.watchdog.check
    def elapsed_quiet_period(*args, **kwargs):
        cli.watchdog.next_check = 0
        if kwargs.get('missing_replay'):
            cli.watchdog.last_activity -= 61
        return original(*args, **kwargs)
    monkeypatch.setattr(cli.watchdog, 'check', elapsed_quiet_period)
    with pytest.raises(RuntimeError, match='Native CLI stalled'):
        cli.run()
    # Initial delivery batches the pending messages; neither has a native receipt.
    assert [m['status'] for m in cli.messages.list()] == ['sent', 'sent']
    cli.messages.recover()
    assert [m['status'] for m in cli.messages.list()] == ['queued', 'queued']
    assert cli.child.poll() is not None
    cli.messages.recover()
    assert [m['status'] for m in cli.messages.list()] == ['queued', 'queued']


@pytest.mark.parametrize('protection', ['activity', 'descendant', 'experiment', 'permission', 'none'])
def test_missing_replay_recovery_keeps_native_activity_and_experiments(monkeypatch, protection):
    import live_watchdog as mod
    monkeypatch.setattr(mod, 'descendants', lambda _: {})
    monkeypatch.setattr(mod, 'group_has_work', lambda *args: protection == 'experiment')
    w = NativeWatchdog(1800)
    w.last_activity -= 61
    if protection == 'activity':
        w.activity()
    elif protection == 'descendant':
        monkeypatch.setattr(mod, 'descendants', lambda _: {123: {'args': ['python', 'experiment.py']}})
    elif protection == 'permission':
        monkeypatch.setattr(mod, 'descendants', lambda _: (_ for _ in ()).throw(PermissionError()))
    result = w.check(os.getpid(), missing_replay=True)
    if protection == 'none':
        assert result['action'] == 'resume_native'
        assert 'without replaying' in result['reason']
    else:
        assert result is None

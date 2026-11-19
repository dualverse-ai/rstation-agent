import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import alive, pid_token, read, save
from controller_guard import ControllerGuard, ensure_guardian, reclaim_idle_clis, reload_when_idle


def test_large_journal_defers_and_codex_symlink_is_recognized(tmp_path):
    from controller_guard import pending_tools,native_child
    run,runtime,cfg=fixture_run(tmp_path)
    launch=runtime/'launch_000001';launch.mkdir()
    with (launch/'transcript.jsonl').open('wb') as out:out.truncate(33*1024*1024)
    assert pending_tools(run,cfg['workers'][0],{'launches':1})
    target=tmp_path/'node_modules/@openai/codex/bin/codex.js';target.parent.mkdir(parents=True);target.touch()
    link=tmp_path/'codex';link.symlink_to(target)
    assert native_child({'args':['node',str(link),'--config']})
    assert not native_child({'args':['node',str(tmp_path/'experiment.js')]})


def fixture_run(path):
    run=path/'run';runtime=run/'claude_1/.runtime';runtime.mkdir(parents=True)
    cfg={'workers':[{'name':'claude_1','kind':'claude'}]}
    save(run/'run.json',cfg)
    return run,runtime,cfg


def cleanup(p):
    if p.poll() is None:
        os.killpg(p.pid,signal.SIGKILL)
    p.wait(timeout=5)


def test_restart_then_stop_and_secret_not_written(tmp_path):
    run,runtime,cfg=fixture_run(tmp_path)
    service=ControllerGuard(run,dict(os.environ,PRIVATE_TEST_SECRET='not-for-disk'),[sys.executable,'-c','import time;time.sleep(60)'])
    try:
        assert service.tick()
        first=service.child
        assert first.poll() is None
        save(run/'controller.json',dict(pid=first.pid,token=pid_token(first.pid)))
        assert service.tick() and service.child is first
        first.kill();first.wait(timeout=5)
        service.next_start=0
        assert service.tick() and service.child.pid!=first.pid
        assert read(run/'controller-recovery.json')['status']=='awaiting_response'
        assert 'not-for-disk' not in ''.join(p.read_text() for p in run.glob('*.json'))
        (run/'STOP').touch()
        assert not service.tick()
        assert service.child.poll() is None
    finally:
        if service.child:cleanup(service.child)


def test_solved_not_restarted(tmp_path):
    run,_,_=fixture_run(tmp_path)
    save(run/'controller.json',dict(reason='problem_solved'))
    guard=ControllerGuard(run,os.environ)
    assert not guard.tick() and guard.child is None


def test_failed_child_backoff(tmp_path):
    run,_,_=fixture_run(tmp_path)
    guard=ControllerGuard(run,os.environ,[sys.executable,'-c','pass'])
    guard.tick();first=guard.child;first.wait(timeout=5)
    guard.tick()
    assert guard.child is None and guard.failures==1
    assert read(run/'guardian.json')['status']=='waiting'


@pytest.mark.parametrize('protection',['none','tool','journal','unknown_journal','node_tool'])
def test_reclaim_preserves_work_and_unfreezes(tmp_path,protection):
    run,runtime,cfg=fixture_run(tmp_path)
    child_line=""
    if protection in {'tool','node_tool'}:
        args="['sleep','60']" if protection=='tool' else "['bash','-c','exec -a node sleep 60']"
        child_line=f"p=subprocess.Popen({args});pathlib.Path(sys.argv[1]).write_text(str(p.pid))\n"
    code='import subprocess,time,pathlib,sys\n'+child_line+'time.sleep(60)'
    p=subprocess.Popen([sys.executable,'-c',code,str(tmp_path/'child')],start_new_session=True)
    try:
        if child_line:
            deadline=time.monotonic()+5
            while not (tmp_path/'child').exists() and time.monotonic()<deadline:time.sleep(.01)
            assert (tmp_path/'child').exists()
        save(runtime/'state.json',dict(pid=p.pid,pid_token=pid_token(p.pid),launches=1))
        launch=runtime/'launch_000001';launch.mkdir()
        if protection!='unknown_journal':
            event={'type':'assistant','message':{'content':[{'type':'tool_use','id':'a','name':'Bash'}]}} if protection=='journal' else {'type':'result','is_error':False}
            (launch/'transcript.jsonl').write_text(json.dumps(event)+'\n')
        result=reclaim_idle_clis(run,cfg)
        if protection=='none':
            assert result;p.wait(timeout=5)
        else:
            assert not result and alive(p.pid,pid_token(p.pid))
            assert Path(f'/proc/{p.pid}/stat').read_text().rsplit(')',1)[1].split()[0] not in {'T','t'}
            if child_line:assert Path('/proc/'+(tmp_path/'child').read_text()).exists()
    finally:cleanup(p)


def test_missing_worker_pid_does_not_block_new_controller(tmp_path):
    run,_,cfg=fixture_run(tmp_path)
    assert reclaim_idle_clis(run,cfg)


@pytest.mark.parametrize('finished_tool',[False,True])
def test_large_journal_uses_completed_turn_but_protects_new_tools(tmp_path,finished_tool):
    from controller_guard import pending_tools
    run,runtime,cfg=fixture_run(tmp_path)
    worker=dict(name=cfg['workers'][0]['name'],kind='codex')
    launch=runtime/'launch_000001';launch.mkdir()
    with (launch/'transcript.jsonl').open('wb') as f:
        f.write(b'{}\n'*(12*1024*1024))
        f.write(b'{"type":"turn.completed"}\n')
        f.write(b'{"type":"item.started","item":{"id":"tool","type":"command_execution"}}\n')
        if finished_tool:
            f.write(b'{"type":"item.completed","item":{"id":"tool","type":"command_execution"}}\n')
    assert pending_tools(run,worker,{'launches':1}) is not finished_tool


def test_idle_detached_code_mode_host_reclaimed(tmp_path):
    run,runtime,cfg=fixture_run(tmp_path)
    p=subprocess.Popen([sys.executable,'-c',
        "import subprocess,pathlib,sys,time; c=subprocess.Popen(['bash','-c','exec -a codex-code-mode-host sleep 60'],start_new_session=True);pathlib.Path(sys.argv[1]).write_text(str(c.pid));c.wait()",
        str(tmp_path/'child')],start_new_session=True)
    child=None
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'child').exists() and time.monotonic()<deadline:time.sleep(.01)
        child=int((tmp_path/'child').read_text());time.sleep(.1)
        child_token=pid_token(child)
        save(runtime/'state.json',dict(pid=p.pid,pid_token=pid_token(p.pid),launches=1))
        launch=runtime/'launch_000001';launch.mkdir();(launch/'transcript.jsonl').write_text('{}\n')
        assert reclaim_idle_clis(run,cfg)
        p.wait(timeout=5)
        assert not alive(child,child_token)
    finally:
        cleanup(p)
        if child and pid_token(child):os.killpg(child,signal.SIGKILL)


def test_guardian_singleton_and_stop(tmp_path):
    run,_,_=fixture_run(tmp_path)
    save(run/'controller.json',dict(pid=os.getpid(),token=pid_token(os.getpid())))
    child=ensure_guardian(run,os.environ)
    try:
        assert child is not None
        assert ensure_guardian(run,os.environ) is None
        (run/'STOP').touch();child.wait(timeout=5)
        assert read(run/'guardian.json')['status']=='stopped'
    finally:cleanup(child)


def test_reload_replaces_controller_only_at_safe_checkpoint(tmp_path):
    run,runtime,_=fixture_run(tmp_path)
    ctl=subprocess.Popen(['sleep','60'],start_new_session=True)
    native=subprocess.Popen(['sleep','60'],start_new_session=True)
    try:
        state=dict(pid=native.pid,pid_token=pid_token(native.pid),launches=1)
        save(runtime/'state.json',state)
        info=dict(pid=ctl.pid,token=pid_token(ctl.pid))
        assert not reload_when_idle(run,info)
        assert ctl.poll() is None and native.poll() is None
        launch=runtime/'launch_000001';launch.mkdir();(launch/'transcript.jsonl').write_text('{}\n')
        assert reload_when_idle(run,info)
        ctl.wait(timeout=5);native.wait(timeout=5)
    finally:cleanup(native);cleanup(ctl)


def test_obsolete_upgrade_does_not_kill_current_controller(tmp_path, monkeypatch):
    import controller_guard as module
    run, _, _ = fixture_run(tmp_path)
    save(run/'controller.json', dict(pid=os.getpid(), token=pid_token(os.getpid()),
                                    code_revision='installed-new'))
    save(run/'controller-upgrade.json', dict(revision='obsolete-old'))
    monkeypatch.setattr(module, 'code_revision', lambda: 'installed-new')
    def forbidden_reload(*args):
        raise AssertionError('Current controller must not be killed for an obsolete upgrade')
    monkeypatch.setattr(module, 'reload_when_idle', forbidden_reload)
    guard = ControllerGuard(run, os.environ)
    for _ in range(3):
        assert guard.tick()
    assert not (run/'controller-upgrade.json').exists()
    assert guard.child is None
    assert read(run/'guardian.json')['status'] == 'watching'


def test_obsolete_upgrade_still_updates_old_controller(tmp_path, monkeypatch):
    import controller_guard as module
    run, _, _ = fixture_run(tmp_path)
    save(run/'controller.json', dict(pid=os.getpid(), token=pid_token(os.getpid()),
                                    code_revision='running-old'))
    save(run/'controller-upgrade.json', dict(revision='obsolete-middle'))
    monkeypatch.setattr(module, 'code_revision', lambda: 'installed-new')
    calls = []
    monkeypatch.setattr(module, 'reload_when_idle', lambda *args: calls.append(args) or False)
    guard = ControllerGuard(run, os.environ)
    assert guard.tick()
    assert len(calls) == 1
    assert (run/'controller-upgrade.json').exists()
    assert guard.child is None

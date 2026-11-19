import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import save, pid_token
from watchdog_service import PoolWatchdog


@pytest.mark.parametrize('protected', ['none', 'paused', 'stopped', 'tool', 'supervisor'])
def test_pool_guard_recovers_native_without_stopping_wrapper(tmp_path, protected):
    root = tmp_path/'agents';run = root/'trial';work = run/'claude_1';runtime = work/'.runtime'
    runtime.mkdir(parents=True)
    script = tmp_path/'wrapper.py'
    native_script = tmp_path/'native.py'
    native_script.write_text('import subprocess,time\n' +
        ("subprocess.Popen(['sleep','60'])\n" if protected=='tool' else '') + 'time.sleep(60)\n')
    script.write_text('import subprocess,sys,time,pathlib\np=subprocess.Popen([sys.executable,sys.argv[1]])\npathlib.Path(sys.argv[2]).write_text(str(p.pid))\np.wait()\ntime.sleep(60)\n')
    p=subprocess.Popen([sys.executable,str(script),str(native_script),str(tmp_path/'native-pid')],start_new_session=True)
    try:
        deadline=time.monotonic()+5
        while not (tmp_path/'native-pid').exists() and time.monotonic()<deadline:time.sleep(.02)
        native=int((tmp_path/'native-pid').read_text());time.sleep(.1)
        save(run/'controller.json',dict(pid=os.getpid(),token=pid_token(os.getpid())))
        save(run/'run.json',dict(workers=[dict(name='claude_1',kind='claude')],idle_seconds=.01,
            role='supervisor' if protected=='supervisor' else 'researcher'))
        save(runtime/'state.json',dict(status='running',pid=p.pid,pid_token=pid_token(p.pid),last_response=time.time()-100))
        save(runtime/'live_input.json',dict(pid=native,token=pid_token(native)))
        os.utime(runtime/'live_input.json',(time.time()-100,time.time()-100))
        if protected=='paused':(runtime/'PAUSE').touch()
        if protected=='stopped':(run/'STOP').touch()
        if protected=='supervisor':
            import supervision
            from unittest.mock import patch
            with patch.object(supervision,'sleeping',return_value=True):actions=PoolWatchdog(root).tick()
        else:actions=PoolWatchdog(root).tick()
        assert p.poll() is None
        if protected=='none':
            assert len(actions)==1 and actions[0]['action']=='resume_native'
            deadline=time.monotonic()+3
            while Path(f'/proc/{native}').exists() and time.monotonic()<deadline:time.sleep(.02)
            assert not Path(f'/proc/{native}').exists()
        else:
            assert actions==[] and Path(f'/proc/{native}').exists()
    finally:
        os.killpg(p.pid,signal.SIGKILL);p.wait()


def test_legacy_direct_cli_recovery(tmp_path):
    root=tmp_path/'agents';run=root/'trial';runtime=run/'claude_1/.runtime';runtime.mkdir(parents=True)
    p=subprocess.Popen(['bash','-c','exec -a claude sleep 60'],start_new_session=True)
    try:
        time.sleep(.1)
        save(run/'controller.json',dict(pid=os.getpid(),token=pid_token(os.getpid())))
        save(run/'run.json',dict(workers=[dict(name='claude_1',kind='claude')],idle_seconds=.01))
        state=dict(status='running',pid=p.pid,pid_token=pid_token(p.pid),started=time.time()-100,last_response=time.time()-100)
        save(runtime/'state.json',state)
        service=PoolWatchdog(root)
        actions=service.tick()
        assert len(actions)==1 and actions[0]['action']=='resume_native'
        p.wait(timeout=3)
        state['last_response']=time.time()
        save(runtime/'state.json',state)
        service.tick()
        assert json.loads((runtime/'watchdog-recovery.json').read_text())['status']=='recovered'
    finally:
        if p.poll() is None:os.killpg(p.pid,signal.SIGKILL);p.wait()


def test_controller_ensures_one_guard_and_restarts_after_exit(tmp_path):
    from watchdog_service import ensure_watchdog
    root=tmp_path/'agents';root.mkdir()
    p=ensure_watchdog(root)
    try:
        deadline=time.monotonic()+5
        while not (root/'.maintenance-watchdog.json').exists() and time.monotonic()<deadline:time.sleep(.02)
        assert json.loads((root/'.maintenance-watchdog.json').read_text())['pid']==p.pid
        assert ensure_watchdog(root) is None
        p.terminate();p.wait(timeout=3)
        p=ensure_watchdog(root)
        assert p is not None
    finally:
        p.terminate();p.wait(timeout=3)

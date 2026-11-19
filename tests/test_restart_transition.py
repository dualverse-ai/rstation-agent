"""Host transition safety, including optional real Linux UID/ACL fixtures.

Run root-only cases on a disposable copy of the toolkit, never a live run.
No provider or GPU computation is used.
"""
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import restart_transition as rt
from common import read, save


def test_rollback_waits_for_successor_gpu_helpers_before_resuming_old(tmp_path, monkeypatch):
    import gpu
    old = tmp_path / 'old/codex_1'
    (old / '.runtime').mkdir(parents=True)
    (old / '.runtime/PAUSE').write_text('operation')
    run = tmp_path / 'new'; (run / 'codex_1/.runtime/gpu-jobs').mkdir(parents=True)
    save(run / 'run.json', {})
    save(run / 'codex_1/.runtime/gpu-jobs/job.json', {'helper_pid': 42, 'helper_token': 'fixture'})
    monkeypatch.setattr(rt, 'alive', lambda pid, token: pid == 42)
    monkeypatch.setattr(gpu, 'reap_orphan_gpu_jobs', lambda *a, **k: None)
    monkeypatch.setattr(rt.HostTransition, '_move_mailbox', lambda *a: None)
    record = dict(id='operation', workspace=str(old), successor_run=str(run), worker='codex_1')
    with pytest.raises(ValueError, match='GPU jobs are still stopping'):
        rt.HostTransition().rollback(record)
    assert (old / '.runtime/PAUSE').exists()
    monkeypatch.setattr(rt, 'alive', lambda *a: False)
    rt.HostTransition().rollback(record)
    assert not (old / '.runtime/PAUSE').exists()


def test_stop_selected_native_when_busy_controller_delays_pause(tmp_path, monkeypatch):
    import gpu
    old = tmp_path / 'old/codex_1'; (old / '.runtime').mkdir(parents=True)
    save(old.parent / 'controller.json', dict(pid=100,token='controller'))
    save(old / '.runtime/state.json', dict(status='running',pid=200,pid_token='selected'))
    selected_alive = [True]; stopped=[]
    monkeypatch.setattr(rt, 'alive', lambda pid,token: pid==100 or (pid==200 and selected_alive[0]))
    def stop(pid):
        stopped.append(pid);selected_alive[0]=False
    monkeypatch.setattr(gpu, 'stop_group', stop)
    monkeypatch.setattr(gpu, 'reap_orphan_gpu_jobs', lambda *a,**k: None)
    monkeypatch.setattr(rt.HostTransition, '_read_only', lambda *a: None)
    times=iter([0,41,42]);monkeypatch.setattr(rt.time,'monotonic',lambda:next(times))
    host=rt.HostTransition()
    host.stop_predecessor(dict(id='operation',workspace=str(old),run=str(old.parent),worker='codex_1',unix_user=pwd.getpwuid(os.getuid()).pw_name))
    assert stopped==[200]
    assert (old / '.runtime/PAUSE').read_text()=='operation'


def test_native_running_excludes_token_matched_zombie(monkeypatch):
    monkeypatch.setattr(rt,'alive',lambda *args:True)
    monkeypatch.setattr(Path,'read_text',lambda *args:'200 (worker with spaces) Z 1 200 200')
    assert not rt.native_running(200,'selected')
    monkeypatch.setattr(Path,'read_text',lambda *args:'200 (worker with spaces) S 1 200 200')
    assert rt.native_running(200,'selected')


@pytest.fixture
def unix_fixture(tmp_path, monkeypatch):
    if os.geteuid() != 0 or not all(shutil.which(x) for x in ('setfacl', 'getfacl', 'useradd', 'setpriv')):
        pytest.skip('Disposable Linux root fixture requires useradd and POSIX ACL tools')
    # Refuse to apply source-code ACLs to a production checkout by accident.
    if not str(rt.ROOT).startswith('/tmp/rstation-restart-audit.'):
        pytest.skip('Run root integration from an isolated /tmp/rstation-restart-audit.* toolkit copy')
    from mailbox_store import Mailbox
    from supervision import install
    oldrun = tmp_path / 'old'; old = oldrun / 'codex_1'
    (old / '.runtime').mkdir(parents=True)
    (old / 'HANDOVER.md').write_text('A promising mechanism and one reproducible experiment.')
    (old / 'retained.txt').write_text('must remain unchanged')
    (old / '.rstation').mkdir()
    save(old / '.rstation/credentials.json', dict(handle='fixture', api_key='dummy-never-sent'))
    worker = dict(name='codex_1', kind='codex', model='fixture-model', gpus=[])
    config = dict(name='old', problem='fixture', category='open_ended', url='http://127.0.0.1:1',
                  workers=[worker], gpu_per_agent=1, gpu_job_seconds=300, storage_gb=1, live_input=True)
    save(oldrun / 'run.json', config)
    ident = secrets.token_hex(4); user = 'rstest_' + ident
    record = dict(id=ident, workspace=str(old), run=str(oldrun), successor_run=str(tmp_path / 'fresh'),
                  worker='codex_1', unix_user=user, config=config, selected=worker,
                  first_instruction='Read the handover and problem statement first.')
    env = dict(PATH='/usr/local/bin:/usr/bin:/bin', CODEX_BIN_PATH='/bin/true',
               RSTATION_WORKER_CODEX_API_KEY='dummy', RSTATION_WORKER_CODEX_BASE_URL='http://127.0.0.1:1')
    monkeypatch.setattr(rt, 'process_environment', lambda _: dict(env))
    acl_changes = []
    original_command = rt.command
    def track_command(args, **kwargs):
        if args[0] == 'setfacl' and '-m' in args:
            acl_changes.append((args[-1], args[args.index('-m')+1], '-R' in args))
        return original_command(args, **kwargs)
    monkeypatch.setattr(rt, 'command', track_command)
    try:
        yield record, rt.HostTransition()
    finally:
        try:
            account = pwd.getpwnam(user)
        except KeyError:
            pass
        else:
            # Temporary named ACLs on shared interpreter/ancestor paths must
            # not outlive this temporary UID and be inherited by a later user.
            for path, spec, recursive in reversed(acl_changes):
                prefix = 'd:u:' if spec.startswith('d:') else 'u:'
                if not spec.startswith(prefix + str(account.pw_uid) + ':'):
                    continue
                subprocess.run(['setfacl'] + (['-R', '-P'] if recursive else []) +
                               ['-x', prefix+str(account.pw_uid), '--', path], capture_output=True)
            subprocess.run(['userdel', user], check=True, capture_output=True)


def run_as(record, code, *args):
    return rt.command(rt.as_user(record['unix_user']) + [sys.executable, '-c', code, *map(str, args)],
                      cwd=Path(record['successor_run']), timeout=40)


def test_real_prepare_account_reuse_and_readonly_operations(unix_fixture):
    record, host = unix_fixture
    host.prepare(record)
    old = Path(record['workspace']); newrun = Path(record['successor_run']); new = newrun / record['worker']
    assert read(new / '.rstation/credentials.json') == read(old / '.rstation/credentials.json')
    assert not (new / '.codex').exists()
    assert (new / 'RESEARCH_INSTRUCTIONS.md').read_text().startswith(record['first_instruction'])
    run_as(record, '''import os,pathlib,sys
old,new=map(pathlib.Path,sys.argv[1:]); assert old.joinpath('retained.txt').read_text()=='must remain unchanged'
new.joinpath('new-result.txt').write_text('fresh writable workspace')
ops=[lambda:old.joinpath('retained.txt').write_text('overwrite'),lambda:old.joinpath('retained.txt').unlink(),
     lambda:old.joinpath('retained.txt').chmod(0o666),lambda:old.rename(old.with_name('renamed')),
     lambda:old.joinpath('new-file').write_text('new'),lambda:os.link(old/'retained.txt',new/'hardlink')]
for op in ops:
 try:op()
 except PermissionError:pass
 else:raise AssertionError('Predecessor mutation was allowed')
''', old, new)
    assert (old / 'retained.txt').read_text() == 'must remain unchanged'


def test_readonly_rejects_writable_external_symlink(unix_fixture):
    record, host = unix_fixture
    host.prepare(record)
    old = Path(record['workspace'])
    external = old.parent.parent / 'outside'; external.write_text('outside original'); external.chmod(0o666)
    (old / 'outside-link').symlink_to(external)
    with pytest.raises(subprocess.CalledProcessError):
        host._read_only(old, pwd.getpwnam(record['unix_user']).pw_uid)
    assert not (old / '.runtime/PAUSE').exists()


def test_readonly_tolerates_private_directory_created_after_acl_pass(unix_fixture, monkeypatch):
    record, host = unix_fixture
    host.prepare(record)
    old = Path(record['workspace'])
    original_acl = rt.acl
    def live_directory(path, uid, mode, **kwargs):
        result = original_acl(path, uid, mode, **kwargs)
        if path == old:
            private = old / 'new-private-temporary-directory'
            private.mkdir(mode=0o700)
            (private / 'private-key').write_text('fixture-only')
        return result
    monkeypatch.setattr(rt, 'acl', live_directory)
    host._read_only(old, pwd.getpwnam(record['unix_user']).pw_uid)
    run_as(record, '''import pathlib,sys
p=pathlib.Path(sys.argv[1])/'new-private-temporary-directory'
try:list(p.iterdir())
except PermissionError:pass
else:raise AssertionError('New private predecessor directory became accessible')
''', old)


def test_real_mailbox_transfer_and_rollback_preserve_pending_proposal(unix_fixture):
    from mailbox_store import Mailbox
    from supervision import install
    record, host = unix_fixture
    old = Path(record['workspace']); root = old.parent.parent
    supervisor = root / 'supervisor/supervisor'; supervisor.mkdir(parents=True)
    box = Mailbox(root / '.supervision/group/mail.sqlite3')
    box.create([dict(actor='researcher', label='researcher', role='researcher', workspace=str(old)),
                dict(actor='supervisor', label='supervisor', role='supervisor', workspace=str(supervisor))])
    install(old, box.path, 'researcher')
    proposal = box.send('researcher', 'supervisor', 'Scientific proposal', kind='proposal')
    save(box.path.parent / 'group.json', dict(name='group', members=['old:codex_1'], supervisor_run=str(supervisor.parent)))
    host.prepare(record); host.transfer(record)
    new = Path(record['successor_run']) / record['worker']
    result = rt.command(rt.as_user(record['unix_user']) + [str(new / 'mail'), 'send', 'supervisor', 'New account can write mailbox'],
                        cwd=new, timeout=40)
    assert result.returncode == 0
    host.rollback(record)
    assert next(p for p in box.participants() if p['actor'] == 'researcher')['workspace'] == str(old)
    with box.transaction() as con:
        row = con.execute('SELECT * FROM messages WHERE id=?', (proposal['id'],)).fetchone()
    assert row['outcome'] == 'pending'


def test_real_supervisor_prepare_reads_continuing_researcher_and_preserves_it(unix_fixture):
    from mailbox_store import Mailbox
    from supervision import install
    record, host = unix_fixture
    old = Path(record['workspace']); root = old.parent.parent
    peer = root / 'continuing/claude_1'; (peer / '.runtime').mkdir(parents=True)
    (peer / 'PLAN.md').write_text('Keep the two-seed experiment running.')
    (peer / '.runtime/state.json').write_text('{"status":"running","fixture":true}')
    before = (peer / '.runtime/state.json').read_bytes()
    peer_config = dict(record['config'], name='continuing', workers=[dict(name='claude_1',kind='claude',model='fixture-claude',gpus=[])])
    save(peer.parent / 'run.json', peer_config)
    box = Mailbox(root / '.supervision/group/mail.sqlite3')
    box.create([dict(actor='researcher', label='researcher', role='researcher', workspace=str(peer)),
                dict(actor='supervisor', label='supervisor', role='supervisor', workspace=str(old))])
    install(old, box.path, 'supervisor', supervisor=True)
    save(box.path.parent / 'group.json', dict(name='group', members=['continuing:claude_1'], supervisor_run=str(old.parent)))
    record['config'].update(role='supervisor',supervision_database=str(box.path))
    save(old.parent / 'run.json', record['config'])
    host.prepare(record); host.transfer(record)
    new = Path(record['successor_run']) / record['worker']
    assert 'Continuing researcher; not replaced' in (new / 'RUNTIME_ASSIGNMENT.md').read_text()
    run_as(record, '''import pathlib,os,sys
p=pathlib.Path(sys.argv[1]);assert p.read_text()=='Keep the two-seed experiment running.';assert not os.access(p,os.W_OK)
''', peer / 'PLAN.md')
    rt.command(rt.as_user(record['unix_user'])+[str(new/'mail'),'participants'],cwd=new,timeout=40)
    host.rollback(record)
    assert (peer / '.runtime/state.json').read_bytes() == before
    assert not (peer / '.runtime/PAUSE').exists()


def test_real_start_log_can_be_reopened_by_successor_guardian(unix_fixture, monkeypatch):
    record, host = unix_fixture
    host.prepare(record)
    old = Path(record['workspace']); run = Path(record['successor_run'])
    (old / '.runtime/PAUSE').write_text(record['id'])
    fake = run / 'fake-toolkit'; fake.mkdir(); fake.chmod(0o755)
    (fake/'main.py').write_text('''import os,pathlib,sys,json,subprocess,signal,time
r=pathlib.Path(sys.argv[2]);w=r/'codex_1'/'.runtime'
def token(pid):return pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(90)'],start_new_session=True)
(r/'controller.json').write_text(json.dumps(dict(pid=os.getpid(),token=token(os.getpid()))))
(w/'state.json').write_text(json.dumps(dict(pid=p.pid,pid_token=token(p.pid))))
def stop(*a):p.terminate();p.wait();sys.exit(0)
signal.signal(signal.SIGTERM,stop)
while True:time.sleep(.1)
''')
    (fake/'main.py').chmod(0o644)
    monkeypatch.setattr(rt,'ROOT',fake)
    try:
        host.start(record)
        log = run/'controller.log'
        assert log.stat().st_uid == pwd.getpwnam(record['unix_user']).pw_uid
        run_as(record, "import pathlib,sys;pathlib.Path(sys.argv[1]).open('a').write('guardian reopened log\\n')", log)
    finally:
        host.rollback(record)


def test_container_environment_read_uses_controller_owner_without_printing(tmp_path, monkeypatch):
    from types import SimpleNamespace
    save(tmp_path / 'controller.json', {'pid': 123456789, 'token': 'fixture'})
    monkeypatch.setattr(rt, 'alive', lambda *_: True)
    monkeypatch.setattr(rt, 'safe_environment', lambda: {'PATH': '/bin'})
    original_read = Path.read_bytes
    original_stat = Path.stat
    def blocked(path):
        if str(path) == '/proc/123456789/environ': raise PermissionError()
        return original_read(path)
    def stat(path, **kwargs):
        if str(path) == '/proc/123456789': return SimpleNamespace(st_uid=os.getuid())
        return original_stat(path, **kwargs)
    monkeypatch.setattr(Path, 'read_bytes', blocked)
    monkeypatch.setattr(Path, 'stat', stat)
    calls=[]
    monkeypatch.setattr(rt, 'command', lambda args, **kw: (calls.append(args) or SimpleNamespace(stdout=b'TEST_DUMMY=private-fixture\0')))
    env=rt.process_environment(tmp_path)
    assert env['TEST_DUMMY']=='private-fixture'
    assert calls[0][-2:] == ['/bin/cat', '/proc/123456789/environ']
    assert '--no-new-privs' in calls[0]


def test_missing_handover_uses_retained_notes_without_modifying_them(tmp_path):
    (tmp_path/'PLAN.md').write_text('Promising mechanism.')
    (tmp_path/'CONTINUATION.md').write_text('Next experiment.')
    before = {p.name:p.read_bytes() for p in tmp_path.iterdir()}
    result = rt.retained_notes(tmp_path)
    assert 'HANDOVER.md was not supplied' in result and 'PLAN.md' in result and 'CONTINUATION.md' in result
    assert {p.name:p.read_bytes() for p in tmp_path.iterdir()} == before
    (tmp_path/'HANDOVER.md').symlink_to(tmp_path/'PLAN.md')
    assert 'HANDOVER.md was not supplied' in rt.retained_notes(tmp_path)


def test_no_notes_does_not_invent_a_handover(tmp_path):
    assert 'no handover or summary notes' in rt.retained_notes(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_controller_owner_cli_discovery_does_not_use_timer_root_home(tmp_path, monkeypatch):
    from types import SimpleNamespace
    ownerhome=tmp_path/'owner';binary=ownerhome/'.local/bin/agy';binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o755)
    save(tmp_path/'controller.json',dict(pid=123456789,token='fixture'))
    monkeypatch.setattr(rt,'alive',lambda *_:True)
    monkeypatch.setattr(rt,'safe_environment',lambda:dict(PATH='/bin'))
    original_read=Path.read_bytes;original_stat=Path.stat
    monkeypatch.setattr(rt.pwd,'getpwuid',lambda uid:SimpleNamespace(pw_dir=str(ownerhome),pw_name='owner'))
    def stat(path,**kw):
        if str(path)=='/proc/123456789':return SimpleNamespace(st_uid=12345)
        return original_stat(path,**kw)
    def data(path):
        if str(path)=='/proc/123456789/environ':return b'PATH=/usr/bin:/bin\0'
        return original_read(path)
    monkeypatch.setattr(Path,'stat',stat);monkeypatch.setattr(Path,'read_bytes',data)
    env=rt.process_environment(tmp_path)
    assert env['ANTIGRAVITY_BIN_PATH']==str(binary)
    assert env['PATH'].split(':')[0]==str(binary.parent)


def test_real_alias_predecessor_remains_readonly_and_alias_is_preserved(unix_fixture):
    record,host=unix_fixture
    from restart_transition import workspace_identity
    old=Path(record['workspace']);target=old.parent.parent/'retained';old.rename(target);old.symlink_to(target,target_is_directory=True)
    record['workspace_identity']=workspace_identity(old)
    host.prepare(record)
    assert old.is_symlink()
    run_as(record,'''import pathlib,sys
p=pathlib.Path(sys.argv[1]);assert (p/'retained.txt').read_text()=='must remain unchanged'
for target in (p/'retained.txt',p/'new-file'):
 try:target.write_text('must fail')
 except PermissionError:pass
 else:raise AssertionError('Predecessor alias allowed mutation')
''',old)

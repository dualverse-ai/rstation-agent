"""Exercise the standalone issuer in a disposable /var/lib directory on Linux."""
import argparse
import json
import os
from pathlib import Path
import runpy
import stat
import subprocess
import sys
import tempfile
import uuid


PYTHON = '/usr/bin/python3'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def trusted_path(path, *, file=False):
    for part in reversed(path.parents):
        info = part.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'Expected root-controlled directory ancestors.')
    info = path.lstat()
    kind = stat.S_ISREG if file else stat.S_ISDIR
    require(kind(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            'Expected a root-controlled path.')


def proposal():
    sid = str(uuid.uuid4())
    return dict(id=str(uuid.uuid4()), worker='claude_probe', status='pending',
                spec=dict(action='fresh', expected_session=sid, expected_revision=3),
                workspace=dict(path='/unused-synthetic-workspace', device=1, inode=2),
                identity=dict(base_sha256='0' * 64, pool_sha256='1' * 64, model='mock-model',
                              control_revision=3, session_id=sid, pin=1, provider_revision=0),
                route=1, previous_launch=4, process_group=None, expected_block='model_refusal')


def probe(issuer, parent, uid, gid):
    store = parent / 'approvals'
    store.mkdir(mode=0o755)
    store.chmod(0o755)
    staging = parent / 'researcher'
    staging.mkdir(mode=0o700)
    os.chown(staging, uid, gid)

    module = runpy.run_path(str(issuer))
    issue, approved = module['issue'], module['approved']
    issuer_globals = issue.__globals__
    issuer_globals['APPROVALS'] = store
    request = proposal()
    key = request['id']
    record = issue(request)
    require(record['request'] == request and issue(request) == approved(key) == record,
            'Issuance or idempotence failed.')
    filename = store / (key + '.json')
    info = filename.stat()
    require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o444, 'Unexpected approval permissions.')
    original = filename.read_bytes()

    # The child reads the approval and performs filesystem operations on its
    # own behalf after subprocess has dropped supplementary groups and IDs.
    subprocess.run([PYTHON, '-I', '-c', '''
import json, os, pathlib, runpy, sys
issuer, store, filename, staging = map(pathlib.Path, sys.argv[1:5])
uid, gid = map(int, sys.argv[5:])
if (os.geteuid(), os.getegid(), os.getgroups()) != (uid, gid, []):
    raise RuntimeError('Child credentials were not dropped.')
record = json.loads(filename.read_text())
issue = runpy.run_path(str(issuer))['issue']
issue.__globals__['APPROVALS'] = store
try:
    issue(record['request'])
except ValueError as exc:
    if str(exc) != 'Fresh conversation approval requires root.':
        raise
else:
    raise RuntimeError('Researcher issued an approval.')
source = staging / 'replacement'
source.write_text('synthetic replacement')
for operation in (lambda: (store / 'new.json').write_text('synthetic'),
                  lambda: filename.write_text('synthetic'),
                  lambda: os.replace(source, filename)):
    try:
        operation()
    except PermissionError:
        continue
    raise RuntimeError('Researcher modified the approval store.')
''', str(issuer), str(store), str(filename), str(staging), str(uid), str(gid)],
        user=uid, group=gid, extra_groups=[], cwd='/', env={}, check=True, timeout=15)
    require(filename.read_bytes() == original, 'Approval changed during researcher checks.')

    filename.chmod(0o644)
    require(approved(key) is None, 'Wrong approval mode was accepted.')
    filename.chmod(0o444)
    os.chown(filename, uid, gid)
    require(approved(key) is None, 'Wrong approval owner was accepted.')
    os.chown(filename, 0, 0)
    parent.chmod(0o775)
    require(approved(key) is None, 'Writable ancestor was accepted.')
    parent.chmod(0o755)
    os.chown(store, uid, gid)
    require(approved(key) is None, 'Wrong directory owner was accepted.')
    os.chown(store, 0, 0)

    alias = parent / 'alias'
    alias.symlink_to(store, target_is_directory=True)
    issuer_globals['APPROVALS'] = alias
    require(approved(key) is None, 'Directory symlink was accepted.')
    issuer_globals['APPROVALS'] = store
    backup = store / 'original.json'
    filename.rename(backup)
    filename.symlink_to(backup)
    require(approved(key) is None, 'Approval symlink was accepted.')
    filename.unlink()
    backup.rename(filename)
    require(approved(key) == record, 'Restored approval failed validation.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uid', type=int, default=2001)
    parser.add_argument('--gid', type=int, default=2001)
    args = parser.parse_args()
    require(os.geteuid() == 0 and sys.flags.isolated, 'Run as root with /usr/bin/python3 -I.')
    require(args.uid > 0 and args.gid > 0, 'Use non-root researcher IDs.')
    script = Path(__file__).absolute()
    issuer = script.with_name('fresh_approval.py')
    parent = Path('/var/lib')
    trusted_path(script, file=True)
    trusted_path(issuer, file=True)
    trusted_path(parent)
    with tempfile.TemporaryDirectory(prefix='rstation-approval-probe-', dir=parent) as directory:
        test_root = Path(directory)
        test_root.chmod(0o755)
        probe(issuer, test_root, args.uid, args.gid)
    print('PASS: issuer idempotence, researcher permissions, ownership, modes, symlinks and cleanup')


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from None

"""Privileged, idempotent host operations for individually approved replacements.

A successor runs as a new, non-login Unix user. ACLs permit reading predecessor
files without changing their ownership, contents or paths. No model is launched
by validation or preparation. Pool replacements require an operator-prepared plan.
"""
import copy
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import time

from common import ROOT, alive, pid_token, read, save, safe_environment


def command(args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def native_running(pid, token):
    if not alive(pid, token):
        return False
    try:
        # A busy parent may not yet have reaped its terminated child. Zombies
        # cannot execute tools or relaunch; the durable PAUSE fence remains.
        return Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[0] != 'Z'
    except (OSError, IndexError):
        return alive(pid, token)


def as_user(user):
    account = pwd.getpwnam(user)
    return ['setpriv', '--reuid', str(account.pw_uid), '--regid', str(account.pw_gid),
            '--clear-groups', '--no-new-privs', '--']


def acl(path, uid, rights, recursive=False):
    args = ['setfacl'] + (['-R', '-P'] if recursive else [])
    command(args + ['-m', f'u:{uid}:{rights}', '--', str(path)], timeout=600)


def traversal(path, uid):
    parents = list(Path(path).absolute().parents) + list(Path(path).resolve().parents)
    for parent in dict.fromkeys(reversed(parents)):
        if parent == Path('/'):
            continue
        # Do not reduce an existing read grant on a shared parent.
        command(['setfacl', '-m', f'u:{uid}:r-x', '--', str(parent)], timeout=20)


def credential_path(work):
    for path in (Path(work) / '.rstation/credentials.json', Path(work) / 'secrets/credentials.json'):
        if path.is_file() and not path.is_symlink():
            return path
    raise ValueError('No canonical saved rStation account found. Leave this agent running until its account can be preserved.')


def workspace_identity(path):
    target = Path(path).resolve(strict=True)
    info = target.stat()
    return dict(path=str(target), device=info.st_dev, inode=info.st_ino)


def verify_workspace(record):
    expected = record.get('workspace_identity')
    if expected is not None and workspace_identity(record['workspace']) != expected:
        raise ValueError('Predecessor workspace changed after arming; leave the agent running.')


def resource_manifest(record):
    path = Path(record['run']).parent / '.restart-timers/environments' / (record['id'] + '.json')
    if not path.exists():
        return {}
    info = path.stat()
    if path.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Successor environment manifest must be controlled by root.')
    data = read(path)
    if data.get('id') != record['id'] or data.get('unix_user') != record['unix_user']:
        raise ValueError('Successor environment manifest belongs to another operation.')
    return data


def retained_notes(work):
    """Missing handover must not indefinitely retain an approved predecessor."""
    work = Path(work)
    handover = work / 'HANDOVER.md'
    if handover.is_file() and not handover.is_symlink():
        return str(handover)
    notes = [str(work / name) for name in ('CONTINUATION.md', 'PLAN.md', 'DIARY.md')
             if (work / name).is_file() and not (work / name).is_symlink()]
    if notes:
        return 'the retained notes (' + ', '.join(notes) + '); HANDOVER.md was not supplied'
    return f'the retained research files under {work}; no handover or summary notes were supplied'


def process_environment(run):
    env = safe_environment()
    controller = read(Path(run) / 'controller.json', {})
    if alive(controller.get('pid'), controller.get('token')):
        # Never persist or print credentials. Preserve this roster's active profile.
        environ_path = Path(f"/proc/{controller['pid']}/environ")
        owner = pwd.getpwuid(environ_path.parent.stat().st_uid)
        try:
            raw = environ_path.read_bytes()
        except PermissionError:
            # Container root may lack CAP_SYS_PTRACE. Read as the controller's
            # own UID; keep the environment in memory and never print it.
            raw = command(as_user(owner.pw_name) + ['/bin/cat', str(environ_path)], timeout=15).stdout
        env.update(dict(item.decode().split('=', 1) for item in raw.split(b'\0') if b'=' in item))
        # The privileged timer runs with root's home; native launchers discover
        # user installations from the controller owner's home instead.
        home = Path(owner.pw_dir)
        bins = [home / '.local/bin', home / '.local/claude-cli/node_modules/.bin']
        bins.extend(sorted((home / '.nvm/versions/node').glob('*/bin')))
        env['PATH'] = ':'.join([str(p) for p in bins] + [env.get('PATH', '')])
        for name, executable in (('ANTIGRAVITY_BIN_PATH', 'agy'), ('CODEX_BIN_PATH', 'codex'), ('CLAUDE_BIN_PATH', 'claude')):
            if not env.get(name):
                found = shutil.which(executable, path=env['PATH'])
                if found:
                    env[name] = found
    return env


def pending_gpu_helpers(run, worker):
    """Old, already-waiting helpers cannot acquire the newly added cutoff guard."""
    running = set()
    for path in (Path(run) / worker / '.runtime/gpu-jobs').glob('*.json'):
        job = read(path, {})
        if alive(job.get('pid'), job.get('token')):
            running.add(job.get('helper_pid'))
    waiting = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) in running:
            continue
        try:
            args = (entry / 'cmdline').read_bytes().decode().split('\0')
            if '_gpu' not in args:
                continue
            index = args.index('_gpu')
            if (index >= 1 and Path(args[index - 1]).resolve() == ROOT / 'main.py'
                    and args[index + 1:index + 3] == [str(Path(run).resolve()), worker]):
                waiting.append(int(entry.name))
        except (OSError, UnicodeError):
            continue
    return waiting


class HostTransition:
    def validate(self, run, work, config, worker):
        if os.geteuid() != 0:
            raise ValueError('The replacement service must run as the host operator (root).')
        missing = [name for name in ('setfacl', 'getfacl', 'setpriv', 'useradd') if not shutil.which(name)]
        if missing:
            raise ValueError('Missing host prerequisites: ' + ', '.join(missing))
        state = read(work / '.runtime/state.json', {})
        if state.get('status') == 'paused':
            raise ValueError('This worker is paused.')
        controller = read(run / 'controller.json', {})
        if not alive(controller.get('pid'), controller.get('token')):
            raise ValueError('The roster controller is not running; repair it before arming.')
        if not getattr(self, 'allow_unregistered', False):
            credential_path(work)
        if pending_gpu_helpers(run, worker['name']):
            raise ValueError('A GPU helper is waiting already. Let it finish or cancel that helper before arming; do not restart the agent.')
        if worker.get('gpus') and not Path(config['gpu_coord_file']).is_file():
            raise ValueError('The existing shared GPU coordination file is missing.')
        from recovery import routes, executable
        env = process_environment(run)
        routes(worker['kind'], env)
        executable(worker['kind'], env)
        command(['getfacl', '--absolute-names', '--', str(work)], timeout=20)

    def _account(self, record):
        user = record['unix_user']
        home = str(Path(record['successor_run']) / record['worker'] / '.home')
        try:
            account = pwd.getpwnam(user)
        except KeyError:
            command(['useradd', '--system', '--user-group', '--no-create-home',
                     '--home-dir', home, '--shell', '/usr/sbin/nologin', user], timeout=30)
            account = pwd.getpwnam(user)
        if account.pw_dir != home or account.pw_uid == 0:
            raise ValueError('Successor Unix account collides with an unrelated account.')
        return account

    def _environment(self, record):
        from recovery import executable
        env = process_environment(record['run'])
        binary, path = executable(record['selected']['kind'], env)
        key = {'codex': 'CODEX_BIN_PATH', 'claude': 'CLAUDE_BIN_PATH', 'gemini': 'ANTIGRAVITY_BIN_PATH'}[record['selected']['kind']]
        env[key] = binary
        env['PATH'] = path
        account = pwd.getpwnam(record['unix_user'])
        env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name,
                   RSTATION_AGENT_DATA=str(Path(record['run']).parent), RSTATION_MANAGED_ENVIRONMENT="1")
        # Suppress accidental attachment to the predecessor's CLI home.
        for name in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'GEMINI_CLI_HOME'):
            env.pop(name, None)
        env.update(resource_manifest(record).get('environment', {}))
        return env, Path(binary)

    def _code_access(self, uid, binary):
        # Executable code only: never grant access to .env, CLI homes or all agent_data.
        traversal(ROOT, uid)
        acl(ROOT, uid, 'r-x')
        for path in ROOT.iterdir():
            if path.suffix == '.py' or path.name in {'agents', 'vendor', 'prompts', 'skills', '.venv'}:
                acl(path, uid, 'r-X', recursive=path.is_dir())
        for path in (Path(sys.executable), binary):
            traversal(path, uid)
            traversal(path.resolve(), uid)
            acl(path.resolve(), uid, 'r-X')
            # npm executables import their package and sibling dependencies.
            if 'node_modules' in path.resolve().parts:
                parts = path.resolve().parts
                modules = Path(*parts[:parts.index('node_modules') + 1])
                acl(modules, uid, 'r-X', recursive=True)

    def _read_only(self, old, uid):
        traversal(old, uid)
        old = Path(old).resolve(strict=True)
        traversal(old, uid)
        acl(old, uid, 'r-X', recursive=True)
        # A named-user ACL overrides group/other write permissions. Parent access
        # is read/traverse only, so the successor cannot rename/delete the root.
        probe = '''import os,sys,pathlib
p=pathlib.Path(sys.argv[1]); assert os.access(p,os.R_OK|os.X_OK)
assert not os.access(p,os.W_OK); assert not os.access(p.parent,os.W_OK)
for name in ('HANDOVER.md','CONTINUATION.md','PLAN.md','DIARY.md'):
 f=p/name
 if f.is_file() and not f.is_symlink():
  assert os.access(f,os.R_OK); assert not os.access(f,os.W_OK)
pending=[p]
while pending:
    directory=pending.pop()
    try:
        entries=os.scandir(directory)
    except (FileNotFoundError, PermissionError):
        # The live predecessor can create/remove private temporary directories
        # after the ACL pass. An inaccessible directory exposes no symlinks to
        # this UID; it must not prevent the already-approved replacement.
        continue
    with entries:
        for entry in entries:
            if entry.is_symlink():
                link=pathlib.Path(entry.path)
                target=link.resolve()
                if target.is_relative_to(p.resolve()):continue
                if os.access(target,os.W_OK) or os.access(target.parent,os.W_OK):
                    raise RuntimeError('Writable external symlink in predecessor workspace')
            elif entry.is_dir(follow_symlinks=False):
                pending.append(entry.path)
'''
        user = pwd.getpwuid(uid).pw_name
        command(as_user(user) + [sys.executable, '-c', probe, str(old)], timeout=600)

    def prepare(self, record):
        verify_workspace(record)
        old = Path(record['workspace'])
        notes = retained_notes(old)
        live = read(Path(record['run']) / 'run.json')
        if live != record['config']:
            raise ValueError('Roster settings changed after arming. Review and rearm without stopping this agent.')
        account = self._account(record)
        uid, gid = account.pw_uid, account.pw_gid
        run = Path(record['successor_run'])
        run.mkdir(mode=0o700, exist_ok=True)
        os.chown(run, uid, gid)
        config = read(run / 'run.json')
        if config and config.get('replacement_id') != record['id']:
            raise ValueError('Successor directory belongs to a different operation.')
        config = copy.deepcopy(record['config'])
        worker = copy.deepcopy(record['selected'])
        if record.get('account_policy') == 'register_new':
            try:
                credential_path(old)
            except ValueError:
                pass
            else:
                # Registration may be repaired during the notice window.
                # Preserve that account instead of creating a duplicate.
                record['account_policy'] = 'reuse_existing'
        if record.get('account_policy') != 'register_new':
            worker.pop('invitation_code', None)  # Keep the account's already-granted access.
        resources = resource_manifest(record)
        for before, after in resources.get('instruction_replacements', {}).items():
            config['instructions'] = config.get('instructions', '').replace(before, after)
        config.update(name=run.name, tmux='rs-' + run.name, workers=[worker],
                      replacement_managed=True, replacement_id=record['id'],
                      predecessor_workspace=str(old), gpu_pool={})
        if record.get('pool_plan'):
            config['gpu_pool'] = {worker['name']: record['pool_plan']['client_attachment']}
        config['replacement_first_instruction'] = record['first_instruction'] + (
            f'\n\nRead {notes} and {config["url"]}/problems/{config["problem"]} first. '
            'Do not repeat a listed failure without a stated new reason. '
            f'The predecessor workspace is available read-only at {old}. '
            + ('This is a fresh conversation. Your predecessor never registered on rStation. Register your account and redeem the prepared invitation in your research instructions. Do not import the old conversation or CLI home.'
             if record.get('account_policy') == 'register_new' else
             'This is a fresh conversation. Reuse the saved rStation account in your new workspace; do not register a new identity or import the old conversation or CLI home.'))
        save(run / 'run.json', config, owner=(uid, gid))
        env, binary = self._environment(record)
        self._code_access(uid, binary)
        from council import enabled as council_enabled
        if council_enabled(config, worker):
            from recovery import executable
            policy = read(Path(record['run']) / '.council/policy.json')
            if not policy or worker['name'] not in policy.get('enabled', []):
                raise ValueError('Enabled research council has no usable saved policy.')
            policy = copy.deepcopy(policy)
            policy['enabled'] = [worker['name']]
            for family in policy['models']:
                council_binary, _ = executable(family, env)
                self._code_access(uid, Path(council_binary))
            council_dir = run / '.council'
            council_dir.mkdir(mode=0o700, exist_ok=True)
            os.chown(council_dir, uid, gid)
            save(council_dir / 'policy.json', policy, owner=(uid, gid))
        from coder import enabled as coder_enabled
        if coder_enabled(config,worker):
            from recovery import executable
            policy=read(Path(record['run'])/'.coder/policy.json')
            if not policy or worker['name'] not in policy.get('enabled',[]):
                raise ValueError('Enabled coder has no usable saved policy.')
            self._code_access(uid,Path(executable('codex',env)[0]))
            policy=copy.deepcopy(policy);policy['enabled']=[worker['name']]
            coder_dir=run/'.coder';coder_dir.mkdir(mode=0o700,exist_ok=True);os.chown(coder_dir,uid,gid)
            save(coder_dir/'policy.json',policy,owner=(uid,gid))
        for resource in resources.get('read_paths', []):
            path = Path(resource)
            if not path.is_absolute() or not path.exists():
                raise ValueError('Prepared successor resource is missing.')
            traversal(path, uid)
            acl(path.resolve(), uid, 'r-X', recursive=path.is_dir())
        traversal(run, uid)
        self._read_only(old, uid)
        self._mail_access(record, uid)
        if worker.get('gpus'):
            coord = Path(config['gpu_coord_file'])
            traversal(coord, uid)
            acl(coord, uid, 'rw-')  # Station updates this locked inode in place.
        work = run / worker['name']
        work.mkdir(mode=0o700, exist_ok=True)
        os.chown(work, uid, gid)
        credentials = work / '.rstation/credentials.json'
        credentials.parent.mkdir(mode=0o700, exist_ok=True)
        os.chown(credentials.parent, uid, gid)
        if record.get('account_policy') != 'register_new':
            save(credentials, read(credential_path(old)), owner=(uid, gid))
        if record.get('pool_plan'):
            plan = record['pool_plan']
            token = work / '.runtime/gpu-pool.token'
            token.parent.mkdir(mode=0o700, exist_ok=True)
            os.chown(token.parent, uid, gid)
            token.write_text(Path(plan['successor_token_file']).read_text())
            token.chmod(0o600); os.chown(token, uid, gid)
            owner_client = read(plan['owner_client'])
            save(plan['client_attachment']['client_config'], {'url':owner_client['url'], 'project':plan['new_project'], 'token_file':str(token)}, owner=(uid,gid))
        # Prepare under the successor UID so its venv, helpers and runtime are writable.
        code = 'import sys;from pathlib import Path;from common import read;from main import prepare_workspaces;r=Path(sys.argv[1]);prepare_workspaces(r,read(r/"run.json"))'
        command(as_user(account.pw_name) + [sys.executable, '-c', code, str(run)],
                cwd=ROOT, env=env, timeout=300)
        for probe in resources.get('probes', []):
            command(as_user(account.pw_name) + probe, cwd=work, env=env, timeout=120)
        if config.get('role') == 'supervisor':
            from supervision import protocol_text
            target = work / 'supervisee-announcement.md'
            target.write_text(protocol_text('supervisee-announcement.md', config))
            os.chown(target, uid, gid)
        # Existing supervision still needs to read this researcher's scientific files.
        old_uid = old.stat().st_uid
        acl(run, old_uid, 'r-x')
        acl(run / 'run.json', old_uid, 'r--')
        acl(work, old_uid, 'r-X', recursive=True)
        command(['setfacl', '-m', f'd:u:{old_uid}:r-x', '--', str(work)], timeout=20)

    def stop_predecessor(self, record):
        verify_workspace(record)
        from gpu import stop_group, reap_orphan_gpu_jobs
        work = Path(record['workspace']); run = Path(record['run'])
        pause = work / '.runtime/PAUSE'
        if pause.exists() and pause.read_text() != record['id']:
            raise ValueError('The operator paused this worker independently; replacement will not override it.')
        pause.write_text(record['id'])
        os.chown(pause, work.stat().st_uid, work.stat().st_gid)
        until = time.monotonic() + 40
        while True:
            controller = read(run / 'controller.json', {})
            state = read(work / '.runtime/state.json', {})
            if not alive(controller.get('pid'), controller.get('token')):
                if native_running(state.get('pid'), state.get('pid_token')):
                    stop_group(state['pid'])
                break
            if state.get('status') == 'paused' and not native_running(state.get('pid'), state.get('pid_token')):
                break
            if time.monotonic() >= until:
                # A controller can spend minutes measuring a large workspace.
                # The durable per-worker fence prevents its next launch; stop
                # only the token-verified native process instead of disturbing
                # the roster's other researchers.
                if native_running(state.get('pid'), state.get('pid_token')):
                    stop_group(state['pid'])
                if native_running(state.get('pid'), state.get('pid_token')):
                    raise ValueError('Selected worker did not pause cleanly; no successor launched.')
                break
            time.sleep(.2)
        reap_orphan_gpu_jobs(run, record['worker'], stop_all=True)
        until = time.monotonic() + 20
        while True:
            jobs = [read(p, {}) for p in (work / '.runtime/gpu-jobs').glob('*.json')]
            if not any(alive(j.get('helper_pid'), j.get('helper_token')) or alive(j.get('pid'), j.get('token')) for j in jobs):
                break
            if time.monotonic() >= until:
                raise ValueError('Selected worker GPU jobs are still stopping; no successor launched.')
            time.sleep(.2)
        self._read_only(work, pwd.getpwnam(record['unix_user']).pw_uid)

    def _mail_access(self, record, uid):
        from supervision import binding
        from mailbox_store import Mailbox
        bound = read(binding(record['workspace']))
        if not bound:
            return
        box = Mailbox(bound['database'])
        traversal(box.path, uid)
        acl(box.path.parent, uid, 'rwx')
        for path in box.path.parent.glob('mail.sqlite3*'):
            acl(path, uid, 'rw-')
        command(['setfacl', '-m', f'd:u:{uid}:rw-', '--', str(box.path.parent)], timeout=20)
        if bound.get('supervisor'):
            for person in box.participants():
                if person['role'] == 'researcher':
                    peer = Path(person['workspace'])
                    traversal(peer, uid)
                    acl(peer, uid, 'r-X', recursive=True)
                    acl(peer.parent / 'run.json', uid, 'r--')
                    command(['setfacl', '-m', f'd:u:{uid}:r-x', '--', str(peer)], timeout=20)

    def _move_mailbox(self, record, forward):
        from supervision import binding, install
        from mailbox_store import Mailbox
        old = Path(record['workspace'])
        new = Path(record['successor_run']) / record['worker']
        binding_data = read(binding(old))
        if not binding_data:
            return
        box = Mailbox(binding_data['database'])
        target, source = (new, old) if forward else (old, new)
        account = pwd.getpwnam(record['unix_user'])
        if forward:
            self._mail_access(record, account.pw_uid)
            install(new, box.path, binding_data['actor'], supervisor=binding_data.get('supervisor', False))
            # Root-created bindings/helpers must remain usable by the new account.
            for name in ('.runtime/supervision.json', 'mail', 'meta_reflection', 'supervisor-wait'):
                path = new / name
                if path.exists(): os.chown(path, account.pw_uid, account.pw_gid)
        with box.transaction() as con:
            person = con.execute('SELECT workspace FROM participants WHERE actor=?', (binding_data['actor'],)).fetchone()
            if not person or person['workspace'] not in {str(old), str(new)}:
                raise ValueError('Mailbox participant changed independently; stop and review the replacement.')
            if person['workspace'] == str(source):
                con.execute('UPDATE participants SET workspace=? WHERE actor=?', (str(target), binding_data['actor']))
                con.execute('UPDATE messages SET native_id=NULL WHERE recipient=? AND archived IS NULL', (binding_data['actor'],))
        group_path = box.path.parent / 'group.json'
        group = read(group_path)
        if group:
            if binding_data.get('supervisor'):
                group['supervisor_run'] = str(target.parent)
            else:
                before, after = f'{source.parent.name}:{source.name}', f'{target.parent.name}:{target.name}'
                group['members'] = [after if item == before else item for item in group['members']]
            owner = group_path.stat()
            save(group_path, group, owner=(owner.st_uid, owner.st_gid))

    def transfer(self, record):
        from common import initial_prompt
        from supervision import assignment
        self._move_mailbox(record, True)
        run = Path(record['successor_run']); work = run / record['worker']
        config = read(run / 'run.json')
        account = pwd.getpwnam(record['unix_user'])
        contents = {'RESEARCH_INSTRUCTIONS.md': initial_prompt(config, config['workers'][0], work)}
        if config.get('role') == 'supervisor':
            contents['RUNTIME_ASSIGNMENT.md'] = assignment(config, work)
        for name, text in contents.items():
            path = work / name
            path.write_text(text)
            os.chown(path, account.pw_uid, account.pw_gid)

    def start(self, record):
        run = Path(record['successor_run'])
        info = read(run / 'controller.json', {})
        guard_path = run / 'replacement-controller.json'
        guard = read(guard_path, {})
        env, _ = self._environment(record)
        if not alive(info.get('pid'), info.get('token')) and not alive(guard.get('pid'), guard.get('token')):
            (run / 'STOP').unlink(missing_ok=True)
            with (run / 'controller.log').open('ab') as log:
                # The unprivileged guardian reopens this log on recovery.
                owner = run.stat()
                os.fchown(log.fileno(), owner.st_uid, owner.st_gid)
                process = subprocess.Popen(as_user(record['unix_user']) + [sys.executable,
                    str(ROOT / 'main.py'), '_controller', str(run)], cwd=ROOT, env=env,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
            save(guard_path, {'pid': process.pid, 'token': pid_token(process.pid)})
        until = time.monotonic() + 30
        while time.monotonic() < until:
            info = read(run / 'controller.json', {})
            state = read(run / record['worker'] / '.runtime/state.json', {})
            if (alive(info.get('pid'), info.get('token')) and alive(state.get('pid'), state.get('pid_token'))):
                return
            time.sleep(.2)
        raise ValueError('The successor did not start its native CLI; the predecessor will be resumed.')

    def rollback(self, record):
        run = Path(record['successor_run'])
        info = read(run / 'controller.json', {})
        guard = read(run / 'replacement-controller.json', {})
        if run.exists():
            (run / 'STOP').touch()
        signalled = set()
        for proc in (info, guard):
            identity = (proc.get('pid'), proc.get('token'))
            if identity in signalled:
                continue
            if alive(proc.get('pid'), proc.get('token')):
                os.kill(proc['pid'], signal.SIGTERM)
                signalled.add(identity)
        until = time.monotonic() + 40
        while any(alive(p.get('pid'), p.get('token')) for p in (info, guard)):
            if time.monotonic() >= until:
                raise ValueError('Successor is still stopping. Predecessor remains paused to prevent concurrent identities.')
            time.sleep(.2)
        if run.exists():
            state = read(run / record['worker'] / '.runtime/state.json', {})
            if alive(state.get('pid'), state.get('pid_token')):
                from gpu import stop_group
                stop_group(state['pid'])
            # A late-starting successor may already have launched a detached
            # GPU helper. Do not resume its predecessor while that job survives.
            if (run / 'run.json').exists():
                from gpu import reap_orphan_gpu_jobs
                reap_orphan_gpu_jobs(run, record['worker'], stop_all=True)
                jobs = [read(p, {}) for p in (run / record['worker'] / '.runtime/gpu-jobs').glob('*.json')]
                if any(alive(j.get('helper_pid'), j.get('helper_token')) or alive(j.get('pid'), j.get('token')) for j in jobs):
                    raise ValueError('Successor GPU jobs are still stopping; predecessor remains paused.')
            if (run / 'run.json').exists():
                self._move_mailbox(record, False)
        if record.get('pool_plan'):
            from restart_pool import rollback_pool
            rollback_pool(record)
        pause = Path(record['workspace']) / '.runtime/PAUSE'
        if pause.exists() and pause.read_text() == record['id']:
            pause.unlink()

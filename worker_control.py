"""Durable, controller-owned settings and restart requests for one researcher."""
import argparse
from contextlib import contextmanager
import copy
import functools
import threading
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from common import alive, lock, pid_token, read, save
from fresh_approval import (approved, canonical_uuid, digest, fields, identifier, integer,
                            read_record, valid_request, valid_workspace)

_held = threading.local()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def base_settings(config, name):
    worker = next((w for w in config.get('workers', []) if w['name'] == name), None)
    if worker is None:
        raise ValueError('Worker not found.')
    # Sibling edits must not invalidate this worker. Shared execution settings do.
    shared = {k: v for k, v in config.items() if k not in ('workers', 'live_input')}
    return {'worker': worker, 'shared': shared}


def effective_worker(worker, state):
    return {**worker, **state.get('worker_control', {}).get('settings', {})}


def runtime(run, name):
    base_settings(read(Path(run) / 'run.json', {}), name)
    if Path(name).name != name or name in ('.', '..'):
        raise ValueError('Invalid worker name.')
    return Path(run) / name / '.runtime'


@contextmanager
def control_lock(folder):
    key = (os.getpid(), str(folder.resolve()))
    held = getattr(_held, 'paths', set())
    if key in held:
        yield
        return
    with lock(folder / 'worker-control.lock') as handle:
        if os.geteuid() == 0:
            st = folder.stat()
            os.fchown(handle.fileno(), st.st_uid, st.st_gid)
        _held.paths = held | {key}
        try: yield
        finally: _held.paths = held


def serialize_failure(method):
    @functools.wraps(method)
    def wrapped(manager, name, *args, **kwargs):
        # Queueing and failure recovery must see one consistent saved session.
        with control_lock(manager.run / name / '.runtime'):
            return method(manager, name, *args, **kwargs)
    return wrapped


def write(folder, filename, data):
    st = folder.stat()
    save(folder / filename, data, owner=(st.st_uid, st.st_gid) if os.geteuid() == 0 else None)


def status(run, name):
    folder = runtime(run, name)
    config = read(Path(run) / 'run.json', {})
    state = read(folder / 'state.json', {})
    control = state.get('worker_control', {})
    worker = effective_worker(base_settings(config, name)['worker'], state)
    route = control.get('provider_route')
    if route is None: route = state.get('session_adoption', {}).get('provider_route', {}).get('number')
    result = {'worker': name, 'model': worker['model'], 'provider_route': route,
            'revision': control.get('revision', 0), 'session_id': state.get('token'),
            'request': read_request(folder),
            'receipt': read(folder / 'worker-control-receipt.json', None)}
    if state.get('fresh_session'):
        result['fresh_session'] = {**state['fresh_session'], 'blocked_reason': state.get('blocked_reason')}
    return result


def require_uuid(request_id):
    if not canonical_uuid(request_id):
        raise ValueError('Use a canonical request UUID.')


def workspace_identity(folder):
    workspace = folder.parent.resolve(strict=True)
    stat = workspace.stat()
    return dict(path=str(workspace), device=stat.st_dev, inode=stat.st_ino)


def fresh_receipt(request_id):
    require_uuid(request_id)
    return f'worker-fresh-{request_id}-receipt.json'


def fresh_process_absent(folder, state, old_group=None):
    """Treat an occupied PID with a different token as ambiguous too."""
    from live_watchdog import group_has_work
    native = read(folder / 'live_input.json', {})
    if any(pid and pid_token(pid) for pid in (state.get('pid'), native.get('pid'))):
        return False
    groups = {state.get('pid'), state.get('last_process_group'), old_group} - {None, 0}
    return not any(group_has_work(group, set()) for group in groups)


def queue_fresh(run, name, *, expected_revision, expected_session, request_id, environment=None):
    from provider_control import operator_pool, require_provider_current, snapshot
    require_uuid(request_id)
    if not integer(expected_revision, maximum=2**63-2) or not identifier(expected_session):
        raise ValueError('Supply the saved session ID and a nonnegative expected revision from worker status.')
    run = Path(run)
    folder = runtime(run, name)
    spec = dict(action='fresh', expected_revision=expected_revision, expected_session=expected_session)
    with control_lock(folder):
        previous = read_request(folder)
        approval = approved(request_id)
        receipt = read_receipt(folder, request_id) if approval and approval['request']['workspace'] == workspace_identity(folder) else None
        for existing in (receipt, previous):
            if existing and existing['id'] == request_id:
                if existing['spec'] != spec:
                    raise ValueError('Request ID already has different settings.')
                return existing
        if previous or (folder / 'session-adoption.json').exists():
            raise ValueError('Finish the pending worker or session-adoption request first.')
        config = read(run / 'run.json', {})
        worker = base_settings(config, name)['worker']
        if worker['kind'] != 'claude' or not config.get('live_input') or config.get('role') == 'supervisor':
            raise ValueError('Fresh requires an existing Claude researcher with live input enabled.')
        state = read(folder / 'state.json', {})
        identity = snapshot(config, name, state)
        if identity['session_id'] != expected_session or identity['control_revision'] != expected_revision:
            raise ValueError('Worker session or revision changed; read worker status again.')
        if not fresh_process_absent(folder, state):
            raise ValueError('Wait for the saved wrapper, native CLI and their process group to exit.')
        require_provider_current(run, name, state)
        pool = operator_pool(run, name, state, os.environ if environment is None else environment)
        pool_hash = fingerprint(pool)
        if identity['pool_sha256'] != pool_hash:
            raise ValueError('Run with the current controller provider configuration.')
        route = identity['pin']
        if route is None:
            route = state.get('route_index', (worker.get('route_seed', 0) + state.get('route_offset', 0)) % len(pool)) + 1
        if not 1 <= route <= len(pool):
            raise ValueError('The saved route is outside the current provider pool.')
        request = dict(id=request_id, spec=spec, worker=name, status='pending',
                       workspace=workspace_identity(folder), identity=identity,
                       route=route, previous_launch=state.get('launches', 0),
                       process_group=state.get('pid') or state.get('last_process_group'),
                       expected_block=state.get('blocked_reason'))
        if not valid_request(request) or (approval and approval['request'] != request):
            raise ValueError('Invalid proposal or request UUID already approved for different settings.')
        durable_write(folder, 'worker-control-request.json', request)
        return request


def sync_directory(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_write(folder, filename, data):
    write(folder, filename, data)
    sync_directory(folder)


def read_receipt(folder, request_id):
    try:
        receipt = read_record(folder / fresh_receipt(request_id))
        if isinstance(receipt, dict) and receipt.get('id') == request_id and receipt.get('status') in ('applied', 'rejected'):
            proposal = {k: v for k, v in receipt.items() if k not in ('revision', 'reason', 'finished_at')}
            if valid_request(dict(proposal, status='pending')):
                return receipt
    except (OSError, ValueError, RecursionError):
        pass
    return None


def finish_fresh(folder, request, **fields):
    filename = fresh_receipt(request['id'])
    receipt = read_receipt(folder, request['id'])
    if receipt is None:
        receipt = dict(request, finished_at=time.time(), **fields)
        durable_write(folder, filename, receipt)
    durable_write(folder, 'worker-control-receipt.json', receipt)
    (folder / 'worker-control-request.json').unlink(missing_ok=True)
    sync_directory(folder)


def process_fresh(manager, name, folder, request):
    from provider_control import provider_update_pending, snapshot
    approval = approved(request['id'])
    if approval is None:
        return  # Proposal stays pending until an operator independently approves it.
    if approval['request'] != request or request['worker'] != name or request['workspace'] != workspace_identity(folder):
        reject_malformed(folder, 'Fresh proposal differs from the operator approval.')
        return
    if read_receipt(folder, request['id']):
        finish_fresh(folder, request)
        return
    state = manager.states[name]
    if fresh_state_approval(folder, state, record=approval):
        sync_directory(folder)
        finish_fresh(folder, request, status='applied', revision=state['worker_control']['revision'])
        return
    if (folder / 'PAUSE').exists() or (manager.run / 'STOP').exists():
        return
    if (name in manager.active_sessions or not fresh_process_absent(folder, state, request.get('process_group'))):
        return
    current = read(manager.run / 'run.json', {})
    if (request['identity'] != snapshot(current, name, state) or
            request['identity'] != snapshot(manager.config, name, state) or
            request['workspace'] != workspace_identity(folder) or
            request['identity']['pool_sha256'] != fingerprint(manager.provider_pool(name)) or
            request['route'] > len(manager.provider_pool(name)) or
            request['previous_launch'] != state.get('launches', 0) or
            request['expected_block'] != state.get('blocked_reason') or
            (folder / 'session-adoption.json').exists() or provider_update_pending(manager.run, name, state)):
        finish_fresh(folder, request, status='rejected', reason='Worker, session, configuration or routes changed.')
        return

    archive = f"worker-fresh-{request['id']}.json"
    try:
        evidence = read_record(folder / archive)
    except (OSError, ValueError, RecursionError):
        evidence = None
    if not isinstance(evidence, dict) or evidence.get('request') != request or evidence.get('state') != state:
        launch = state.get('launches', 0)
        durable_write(folder, archive, dict(request=request, state=state, archived_at=time.time(),
            previous_transcript=f'launch_{launch:06d}/transcript.jsonl' if launch else None,
            native_process=read(folder / 'live_input.json', None)))
    updated = copy.deepcopy(state)
    control = state.get('worker_control', {})
    updated['worker_control'] = dict(revision=request['spec']['expected_revision'] + 1,
        request_id=request['id'], session_id=None, settings=control.get('settings', {}),
        provider_route=request['identity']['pin'], pool_sha256=request['identity']['pool_sha256'],
        base_sha256=request['identity']['base_sha256'])
    updated['fresh_session'] = dict(id=request['id'], native_id=approval['native_id'], route=request['route'],
        previous_session_id=request['spec']['expected_session'], workspace=request['workspace'], phase='ready')
    seed = manager.worker(name).get('route_seed', 0)
    updated.update(token=None, pid=None, resumes=0, next=0, route_count=0,
                   route_offset=request['route'] - 1 - seed, status='continuing')
    for key in ('pid_token', 'last_process_group', 'blocked_reason', 'worker_control_hold', 'session_adoption',
                'session_unavailable', 'session_failure_streak', 'session_recovery', 'claude_compaction',
                'provider_interrupt', 'kill_after', 'api_retries', 'last_error', 'last_failure_category'):
        updated.pop(key, None)
    updated['providers'].pop('session_id', None)
    write(folder, 'state.json', updated)
    manager.states[name] = updated
    manager._endpoint_resume_counts[name] = 0
    sync_directory(folder)
    finish_fresh(folder, request, status='applied', revision=updated['worker_control']['revision'])


def fresh_start_failed(state, category=None):
    fresh = state.get('fresh_session', {})
    if fresh.get('phase') != 'started':
        return False
    fresh['phase'] = 'unconfirmed'
    state.update(status='blocked', blocked_reason='model_refusal' if category == 'model_refusal' else 'fresh_session_unconfirmed',
                 next=0, last_failure_category=category or 'fresh_session_unconfirmed')
    return True


def bind_fresh_session(state, launch, event):
    fresh = state.get('fresh_session', {})
    if (fresh.get('phase') == 'started' and fresh['launch'] == launch and
            event.get('type') == 'system' and event.get('subtype') == 'init' and
            event.get('session_id') == fresh['native_id']):
        fresh['phase'] = 'bound'
        state['token'] = state['worker_control']['session_id'] = fresh['native_id']
        if state['providers'].get('revision'):
            state['providers']['session_id'] = fresh['native_id']
        return True
    return False


def recover_fresh_session(folder, state):
    fresh = state['fresh_session']
    if fresh['phase'] != 'started':
        return
    from station.workers.job_manager import CliJobManager
    path = folder / fresh['launch'] / 'transcript.jsonl'
    for event in CliJobManager._read_transcript_events(str(path)):
        if isinstance(event, dict) and bind_fresh_session(state, fresh['launch'], event):
            return
    fresh_start_failed(state)


def fresh_state_approval(folder, state, *, record=None):
    fresh = state.get('fresh_session')
    if not isinstance(fresh, dict) or not canonical_uuid(fresh.get('id')):
        return None
    record = approved(fresh['id']) if record is None else record
    if not record:
        return None
    request = record['request']
    phase = fresh.get('phase')
    if (request['id'] != fresh['id'] or request['worker'] != folder.parent.name or
            request['workspace'] != fresh.get('workspace') or request['workspace'] != workspace_identity(folder) or
            record['native_id'] != fresh.get('native_id') or request['route'] != fresh.get('route') or
            request['spec']['expected_session'] != fresh.get('previous_session_id')):
        return None
    launch = fresh.get('launch')
    count = state.get('launches')
    if not integer(count) or (phase == 'ready' and (launch is not None or count != request['previous_launch'])):
        return None
    if phase != 'ready':
        if (not isinstance(launch, str) or len(launch) > 27 or not launch.startswith('launch_') or
                not launch[7:].isascii() or not launch[7:].isdecimal() or
                not request['previous_launch'] < int(launch[7:]) <= count):
            return None
    control = state.get('worker_control')
    if not isinstance(control, dict):
        return None
    if phase == 'bound':
        return record if control.get('session_id') == record['native_id'] == state.get('token') else None
    if (phase not in ('ready', 'started', 'unconfirmed') or state.get('token') is not None or
            control.get('session_id') is not None or control.get('request_id') != request['id'] or
            control.get('revision') != request['spec']['expected_revision'] + 1 or
            (phase == 'started' and launch != f'launch_{count:06d}') or
            any(control.get(key) != request['identity'][key] for key in ('base_sha256', 'pool_sha256')) or
            control.get('provider_route') != request['identity']['pin']):
        return None
    return record


def validate_fresh_wrapper(workspace, request_id, model, command, kind, conversation):
    work = Path(workspace)
    folder = work / '.runtime'
    with control_lock(folder):
        state = read(folder / 'state.json', {})
        approval = fresh_state_approval(folder, state)
        if (not approval or approval['request']['id'] != request_id or state['fresh_session']['phase'] != 'started' or
                kind != 'claude' or conversation or '--resume' in command or '--continue' in command or
                command.count('--session-id') != 1 or command[-2:] != ['--session-id', approval['native_id']] or
                command.count('--model') != 1 or command[command.index('--model') + 1:][:1] != [model] or
                model != approval['request']['identity']['model'] or
                fingerprint(base_settings(read(work.parent / 'run.json', {}), work.name)) != approval['request']['identity']['base_sha256'] or
                state.get('pid') != os.getpid() or not alive(state['pid'], state.get('pid_token')) or
                (folder / 'PAUSE').exists() or (work.parent / 'STOP').exists()):
            raise RuntimeError('Fresh conversation launch is no longer authorized.')


def queue_request(run, name, *, expected_revision, model=None, provider_route=None,
                  change_route=False, restart=False, environment=None, request_id=None):
    run = Path(run)
    folder = runtime(run, name)
    if not restart and model is None and not change_route:
        raise ValueError('Select a model, provider route, or restart.')
    if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c.isspace() for c in model)):
        raise ValueError('Use a nonempty model identifier without whitespace.')
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('Expected revision must be a nonnegative integer.')
    if request_id is not None:
        require_uuid(request_id)
    with control_lock(folder):
        config = read(run / 'run.json', {})
        state = read(folder / 'state.json', {})
        base = base_settings(config, name)
        if base['worker']['kind'] == 'gemini' and provider_route is not None:
            raise ValueError('Gemini automatically selects among enabled providers. Use auto.')
        control = state.get('worker_control', {})
        spec = dict(expected_revision=expected_revision, model=model, provider_route=provider_route,
                    change_route=change_route, restart=restart)
        previous = read(folder / 'worker-control-request.json', None)
        receipt = read(folder / 'worker-control-receipt.json', None)
        if request_id and (folder / fresh_receipt(request_id)).exists():
            raise ValueError('Request ID already belongs to a fresh request.')
        for existing in (previous, receipt):
            if existing and (existing['id'] == request_id if request_id else existing.get('spec') == spec):
                if existing['spec'] != spec: raise ValueError('Request ID already has different settings.')
                return existing
        if previous: raise ValueError('Another worker change is pending.')
        if control.get('revision', 0) != expected_revision:
            raise ValueError('Worker settings changed; read status and use its current revision.')
        if not state.get('token') or state.get('session_unavailable'):
            raise ValueError('A usable saved session is required; this command never starts a new conversation.')
        if state.get('blocked_reason') not in (None, 'worker_control_failed', 'session_adoption_failed'):
            raise ValueError('Resolve the existing worker block before requesting a settings change.')
        if any(state.get(key, {}).get('failure_category') in ('model_refusal', 'session_unavailable')
               for key in ('session_adoption', 'worker_control', 'worker_control_hold')):

            raise ValueError('Resolve the existing refusal or missing-session condition first.')
        if (folder / 'session-adoption.json').exists():
            raise ValueError('Finish the pending session adoption first.')
        from provider_control import operator_pool
        pool = operator_pool(run,name,state,os.environ if environment is None else environment)
        if change_route and provider_route is not None and (type(provider_route) is not int or not 1 <= provider_route <= len(pool)):
            raise ValueError('Provider route must be a one-based configured index, or auto.')
        work = folder.parent.resolve(strict=True); stat = work.stat()
        request = dict(id=request_id or str(uuid.uuid4()), spec=spec, created_at=time.time(),
                       session_id=state['token'], process_group=state.get('pid') or state.get('last_process_group'), expected_block=state.get('blocked_reason'), workspace=dict(path=str(work), device=stat.st_dev, inode=stat.st_ino),
                       base_sha256=fingerprint(base), pool_sha256=fingerprint(pool), status='pending')
        write(folder, 'worker-control-request.json', request)
        return request


REQUEST = 'worker-control-request.json'


def read_request(folder, *, strict=False):
    try:
        value = read_record(folder / REQUEST)
        if valid_request(value):
            return value
        if fields(value, 'id', 'spec', 'created_at', 'session_id', 'process_group', 'expected_block',
                  'workspace', 'base_sha256', 'pool_sha256', 'status'):
            spec = value['spec']
            if (fields(spec, 'expected_revision', 'model', 'provider_route', 'change_route', 'restart') and
                    canonical_uuid(value['id']) and value['status'] == 'pending' and
                    integer(spec['expected_revision']) and (spec['model'] is None or identifier(spec['model'])) and
                    (spec['provider_route'] is None or integer(spec['provider_route'], 1, 4096)) and
                    type(spec['change_route']) is bool and type(spec['restart']) is bool and
                    (spec['change_route'] or spec['provider_route'] is None) and
                    type(value['created_at']) in (float, int) and 0 <= value['created_at'] <= 2**53 and
                    identifier(value['session_id']) and valid_workspace(value['workspace']) and
                    digest(value['base_sha256']) and digest(value['pool_sha256']) and
                    (value['process_group'] is None or integer(value['process_group'], 1, 2**31-1)) and
                    (value['expected_block'] is None or identifier(value['expected_block']))):
                return value
        raise ValueError()
    except FileNotFoundError:
        return None
    except (OSError, ValueError, RecursionError):
        if strict:
            raise ValueError('Malformed worker control request.') from None
        return None


def reject_malformed(folder, reason):
    write(folder, 'worker-control-receipt.json', dict(status='rejected', reason=reason, finished_at=time.time()))
    try:
        (folder / REQUEST).unlink(missing_ok=True)
    except IsADirectoryError:
        # An invalid entry can contain unrelated files. Leave it in place.
        pass


def checkpoint_requested(work):
    request = read_request(Path(work) / '.runtime')
    return request if request and request['spec'].get('action') != 'fresh' else None


def offer_checkpoint(cli):
    """Called only by a live wrapper after a complete turn, before new input."""
    request = checkpoint_requested(cli.args.workspace)
    if not request or cli.busy or cli.output:
        return False
    from common import pid_token
    folder = Path(cli.args.workspace) / '.runtime'
    write(folder, 'worker-control-ready.json', dict(request_id=request['id'], pid=os.getpid(),
          token=pid_token(os.getpid())))
    return True  # Suppress new model turns until the controller consumes/cancels it.


def has_jobs(work):
    from live_watchdog import group_has_work
    config = read(work.parent / 'run.json', {})
    ledger_path = config.get('gpu_coord_file')
    ledger = read(Path(ledger_path), {}) if ledger_path else {}
    for job in (read(p, {}) for p in (work / '.runtime/gpu-jobs').glob('*.json')):
        if (alive(job.get('pid'), job.get('token')) or
                alive(job.get('helper_pid'), job.get('helper_token'))): return True
        if job.get('pid') and group_has_work(job['pid'], set()): return True
        if job.get('owner') and any(v.get('station_id') == job['owner']
                                   for v in ledger.get('allocations', {}).values()): return True
    return False


def _reject(folder, request, reason):
    write(folder, 'worker-control-receipt.json', {**request, 'status': 'rejected', 'reason': reason, 'finished_at': time.time()})
    (folder / 'worker-control-request.json').unlink(missing_ok=True)
    (folder / 'worker-control-ready.json').unlink(missing_ok=True)


def _process_requests(manager):
    """Called after polling, before scheduling launches. Never inspect a sibling's jobs."""
    if manager.probe or (manager.run / 'STOP').exists(): return
    for original in manager.config['workers']:
        name = original['name']; folder = manager.run / name / '.runtime'
        if not os.path.lexists(folder / REQUEST): continue
        with control_lock(folder):
            try:
                request = read_request(folder, strict=True)
            except ValueError:
                reject_malformed(folder, 'Malformed worker control request.')
                continue
            if not request: continue
            if request['spec'].get('action') == 'fresh':
                process_fresh(manager, name, folder, request)
                continue
            state = manager.states[name]; control = state.get('worker_control', {})
            if (state.get('blocked_reason') not in (None, 'worker_control_failed', 'session_adoption_failed') or
                    any(state.get(key, {}).get('failure_category') in ('model_refusal', 'session_unavailable')
                        for key in ('session_adoption', 'worker_control', 'worker_control_hold'))):
                reject_malformed(folder, 'Resolve the existing worker block before requesting a settings change.')
                continue
            if control.get('request_id') == request['id']:
                manager.persist(name)
                write(folder, 'worker-control-receipt.json', {**request, 'status': 'applied', 'revision': control['revision'], 'finished_at': time.time()})
                (folder / 'worker-control-request.json').unlink(missing_ok=True)
                (folder / 'worker-control-ready.json').unlink(missing_ok=True)
                continue
            current = read(manager.run / 'run.json', {})
            pool = manager.provider_pool(name)
            work = folder.parent.resolve(); stat = work.stat()
            if (control.get('revision', 0) != request['spec']['expected_revision'] or
                    state.get('token') != request['session_id'] or state.get('session_unavailable') or
                    state.get('blocked_reason') != request.get('expected_block') or (folder / 'session-adoption.json').exists() or
                    request['base_sha256'] != fingerprint(base_settings(current, name)) or
                    request['base_sha256'] != fingerprint(base_settings(manager.config, name)) or
                    request['pool_sha256'] != fingerprint(pool) or
                    request['workspace'] != dict(path=str(work), device=stat.st_dev, inode=stat.st_ino)):
                _reject(folder, request, 'Worker, session, configuration or configured routes changed.')
                continue
            route = request['spec']['provider_route']
            if route is not None and (original['kind'] == 'gemini' or route > len(pool)):
                _reject(folder, request, 'Provider route is unavailable for this worker.')
                continue
            if has_jobs(work): continue
            from live_watchdog import group_has_work
            group = state.get('pid') or state.get('last_process_group') or request.get('process_group')
            if group and not alive(state.get('pid'), state.get('pid_token')) and group_has_work(group, set()): continue
            session = manager.active_sessions.get(name)
            native = read(folder / 'live_input.json', {})
            state_alive = alive(state.get('pid'), state.get('pid_token'))
            if alive(native.get('pid'), native.get('token')) and not state_alive: continue
            if session is not None or state_alive:
                ready = read(folder / 'worker-control-ready.json', {})
                if (ready.get('request_id') != request['id'] or ready.get('pid') != state.get('pid') or
                        not alive(ready.get('pid'), ready.get('token'))): continue
            # Reuse the guarded process/journal check for this worker alone.
            from controller_guard import reclaim_idle_clis
            if not reclaim_idle_clis(manager.run, {'workers': [original]}): continue
            if session is not None:
                session.process.wait(timeout=5)
                manager._close_cli_job_session_handles(session)
                manager.active_sessions.pop(name, None)
            updated = copy.deepcopy(state)
            settings = dict(control.get('settings', {}))
            if request['spec']['model'] is not None: settings['model'] = request['spec']['model']
            route = control.get('provider_route')
            if request['spec']['change_route']: route = request['spec']['provider_route']
            updated['worker_control'] = dict(revision=control.get('revision', 0) + 1,
                request_id=request['id'], session_id=request['session_id'], settings=settings,
                provider_route=route, pool_sha256=request['pool_sha256'], base_sha256=request['base_sha256'])
            updated.update(token=request['session_id'], pid=None, resumes=0, next=0,
                           status='paused' if (folder / 'PAUSE').exists() else 'continuing')
            updated.pop('gemini_recovery_route', None)
            updated.pop('blocked_reason', None)
            updated.pop('worker_control_hold', None)
            # This explicit control request supersedes the target's prior route pin only.
            if request['spec']['change_route'] and 'session_adoption' in updated:
                updated['session_adoption'].pop('provider_route', None)
            manager.states[name] = updated
            manager.persist(name)  # Atomic commit; request remains replayable until receipt is written.
            manager._endpoint_resume_counts[name] = 0
            write(folder, 'worker-control-receipt.json', {**request, 'status': 'applied',
                  'revision': updated['worker_control']['revision'], 'finished_at': time.time()})
            (folder / 'worker-control-request.json').unlink(missing_ok=True)
            (folder / 'worker-control-ready.json').unlink(missing_ok=True)


def process_requests(manager):
    for worker in manager.config['workers']:
        # A narrow view keeps the loop and all mutation scoped to one worker.
        class View:
            def __getattr__(self, key): return getattr(manager, key)
        view = View()
        view.config = {**manager.config, 'workers': [worker]}
        # Base fingerprints exclude siblings, so this view has identical identity.
        try:
            _process_requests(view)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            try:
                manager.log(worker['name'], 'Worker control deferred (' + type(exc).__name__ + '); request retained.')
            except OSError:
                # The same filesystem fault can also prevent writing the log.
                pass


def guard_launch(manager, name):
    from provider_control import guard_session
    folder = manager.run / name / '.runtime'
    state = manager.states[name]
    approval = None
    if 'fresh_session' in state:
        approval = fresh_state_approval(folder, state)
        if not approval:
            return False
    guard_session(manager,name)
    state = manager.states[name]
    if not manager.probe and manager.worker(name)['kind'] == 'gemini':
        from gemini_failover import guard
        if not guard(manager, name):
            return False
    hold = state.get('worker_control_hold')
    if hold:
        if state.get('token') != hold['session_id']:
            state['token'] = hold['session_id']; manager.persist(name)
        return False
    request = read_request(folder)
    if request:
        if request['spec'].get('action') != 'fresh':
            return False
        pending = approved(request['id'])
        if pending and pending['request'] == request:
            return False
    state = manager.states[name]; control = state.get('worker_control')
    if not control: return True
    if state.get('blocked_reason'): return False
    fresh = state.get('fresh_session')
    if fresh and fresh['phase'] != 'bound':
        if (fresh['phase'] != 'ready' or fresh['workspace'] != workspace_identity(folder) or
                (folder / 'PAUSE').exists() or (manager.run / 'STOP').exists()):
            return False
        request = approval['request']
        if (manager.worker(name)['model'] != request['identity']['model'] or
                not fresh_process_absent(folder, state, request['process_group'])):
            return False
    original = next(w for w in manager.config['workers'] if w['name'] == name)
    valid = (state.get('token') == control['session_id'] and not state.get('session_unavailable') and
             control['base_sha256'] == fingerprint(base_settings(manager.config, name)) and
             control['base_sha256'] == fingerprint(base_settings(read(manager.run / 'run.json', {}), name)) and
             control['pool_sha256'] == fingerprint(manager.provider_pool(name)))
    if not valid:
        state.update(token=control['session_id'], status='blocked', blocked_reason='worker_control_conflict')
        manager.persist(name)
    return valid


def selected_route(state, default):
    route = state.get('worker_control', {}).get('provider_route')
    return default if route is None else route - 1


def handle_failure(manager, name, failure):
    state = manager.states[name]
    if fresh_start_failed(state, failure.category):
        state['pid'] = None  # Called after the CLI exits.
        manager.persist(name)
        return True
    control = state.get('worker_control')
    if not control:
        from provider_control import failure_control
        control = failure_control(manager,name)
    request = checkpoint_requested(manager.run / name)
    if not control and not request: return False
    session_id = control['session_id'] if control else request['session_id']
    state['token'] = session_id
    state.pop('session_unavailable', None)
    outage = failure.category == 'provider_unavailable' or (
        failure.category == 'resume_limit_exceeded' and control and control.get('provider_retry'))
    if outage:
        if control: control['provider_retry'] = True
        state.update(status='retrying', pid=None, next=time.time() + 60, resumes=0)
    else:
        state.update(status='blocked', pid=None, next=0,
                     blocked_reason='model_refusal' if failure.category == 'model_refusal' else 'worker_control_failed')
        # A failure before application must also outlive rejection/removal of the request.
        state['worker_control_hold'] = dict(session_id=session_id, failure_category=failure.category)
        if control: control['failure_category'] = failure.category
    manager.persist(name)
    return True


def preflight_resume(run, config, environment, preflight):
    """Validate effective models without rewriting an applied override's base."""
    effective = copy.deepcopy(config)
    controlled = set()
    for index, worker in enumerate(effective['workers']):
        state = read(Path(run) / worker['name'] / '.runtime/state.json', {})
        effective['workers'][index] = effective_worker(worker, state)
        if state.get('worker_control'): controlled.add(worker['name'])
    preflight(effective, environment)
    for raw, checked in zip(config['workers'], effective['workers']):
        if raw['name'] not in controlled and 'route_seed' in checked:
            raw['route_seed'] = checked['route_seed']


def cli(argv):
    from main import run_dir
    from common import safe_environment
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('run'); p.add_argument('worker'); p.add_argument('action', choices=('status', 'update', 'restart', 'fresh'))
    p.add_argument('--expect-revision', type=int)
    p.add_argument('--model'); p.add_argument('--provider-route', help='One-based configured route, or auto.')
    p.add_argument('--expect-session', help='Current native session ID, required for fresh.')
    p.add_argument('--request-id', help='Request UUID, required for fresh.')
    a = p.parse_args(argv); run = run_dir(a.run)
    if a.action == 'status': result = status(run, a.worker)
    elif a.action == 'fresh':
        if a.model is not None or a.provider_route is not None:
            p.error('fresh preserves the configured model and provider.')
        from controller_guard import code_revision
        controller = read(run / 'controller.json', {})
        if (controller.get('code_revision') != code_revision() or
                not alive(controller.get('pid'), controller.get('token'))):
            raise ValueError('Fresh requires the running controller to load this launcher revision.')
        result = queue_fresh(run, a.worker, expected_revision=a.expect_revision, expected_session=a.expect_session,
                             environment=safe_environment(), request_id=a.request_id)
    else:
        if a.expect_revision is None: raise ValueError('Read worker status and supply --expect-revision.')
        route = None if a.provider_route in (None, 'auto') else int(a.provider_route)
        result = queue_request(run, a.worker, expected_revision=a.expect_revision, model=a.model,
            provider_route=route, change_route=a.provider_route is not None, restart=a.action == 'restart',
            environment=safe_environment(), request_id=a.request_id)
    print(json.dumps(result, indent=2))

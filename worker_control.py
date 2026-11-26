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

from common import alive, lock, read, save

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
    return {'worker': name, 'model': worker['model'], 'provider_route': route,
            'revision': control.get('revision', 0), 'session_id': state.get('token'),
            'request': read(folder / 'worker-control-request.json', None),
            'receipt': read(folder / 'worker-control-receipt.json', None)}


def queue_request(run, name, *, expected_revision, model=None, provider_route=None,
                  change_route=False, restart=False, environment=None, request_id=None):
    from recovery import routes
    run = Path(run)
    folder = runtime(run, name)
    if not restart and model is None and not change_route:
        raise ValueError('Select a model, provider route, or restart.')
    if model is not None and (not isinstance(model, str) or not model.strip() or len(model) > 200 or any(c.isspace() for c in model)):
        raise ValueError('Use a nonempty model identifier without whitespace.')
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError('Expected revision must be a nonnegative integer.')
    if request_id is not None:
        try:
            if str(uuid.UUID(request_id)) != request_id: raise ValueError()
        except (ValueError, TypeError): raise ValueError('Use a canonical request UUID.') from None
    with control_lock(folder):
        config = read(run / 'run.json', {})
        state = read(folder / 'state.json', {})
        base = base_settings(config, name)
        control = state.get('worker_control', {})
        spec = dict(expected_revision=expected_revision, model=model, provider_route=provider_route,
                    change_route=change_route, restart=restart)
        previous = read(folder / 'worker-control-request.json', None)
        receipt = read(folder / 'worker-control-receipt.json', None)
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


def checkpoint_requested(work):
    return read(Path(work) / '.runtime/worker-control-request.json', None)


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
        if not (folder / 'worker-control-request.json').exists(): continue
        with control_lock(folder):
            request = read(folder / 'worker-control-request.json', None)
            if not request: continue
            state = manager.states[name]; control = state.get('worker_control', {})
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
            manager.log(worker['name'], 'Worker control deferred (' + type(exc).__name__ + '); request retained.')


def guard_launch(manager, name):
    from provider_control import guard_session
    guard_session(manager,name)
    state = manager.states[name]
    hold = state.get('worker_control_hold')
    if hold:
        if state.get('token') != hold['session_id']:
            state['token'] = hold['session_id']; manager.persist(name)
        return False
    if (manager.run / name / '.runtime/worker-control-request.json').exists(): return False
    state = manager.states[name]; control = state.get('worker_control')
    if not control: return True
    if state.get('blocked_reason'): return False
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
    p.add_argument('run'); p.add_argument('worker'); p.add_argument('action', choices=('status', 'update', 'restart'))
    p.add_argument('--expect-revision', type=int)
    p.add_argument('--model'); p.add_argument('--provider-route', help='One-based configured route, or auto.')
    p.add_argument('--request-id', help='Optional UUID for retrying an operator request.')
    a = p.parse_args(argv); run = run_dir(a.run)
    if a.action == 'status': result = status(run, a.worker)
    else:
        if a.expect_revision is None: raise ValueError('Read worker status and supply --expect-revision.')
        route = None if a.provider_route in (None, 'auto') else int(a.provider_route)
        result = queue_request(run, a.worker, expected_revision=a.expect_revision, model=a.model,
            provider_route=route, change_route=a.provider_route is not None, restart=a.action == 'restart',
            environment=safe_environment(), request_id=a.request_id)
    print(json.dumps(result, indent=2))

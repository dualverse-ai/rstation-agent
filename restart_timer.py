"""Opt-in replacement timers. Control records belong to the host operator.

Native receipt, not send time, starts the clock. The privileged service executes
transitions; ordinary controllers and unarmed workers retain their old behavior.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import time

from common import NAME, lock, read, save
from messages import Messages

TERMINAL = {'cancelled', 'complete', 'blocked'}
ACTIVE = {'awaiting_receipt', 'armed', 'preparing', 'pausing', 'stopped', 'launching', 'rolling_back', 'cancelling'}


def iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None


def timer_path(run, worker):
    run = Path(run).resolve()
    if not NAME.fullmatch(worker):
        raise ValueError('Invalid worker name.')
    key = hashlib.sha256((str(run) + '/' + worker).encode()).hexdigest()[:24]
    return run.parent / '.restart-timers' / (key + '.json')


def marker(work):
    return Path(work) / '.runtime/restart-timer.json'


def write_marker(record):
    work = Path(record['workspace'])
    owner = work.stat()
    save(marker(work), {k: record.get(k) for k in (
        'id', 'status', 'message_id', 'seconds', 'received_at', 'deadline')},
        owner=(owner.st_uid, owner.st_gid) if os.geteuid() == 0 else None)


def observed_receipt(work, number):
    message = next((m for m in Messages(work).list() if m['id'] == number), {})
    stamp = message.get('first_received_at') or message.get('received_at')
    return datetime.fromisoformat(stamp).timestamp() if stamp else None


def gpu_start_allowed(work, now=None):
    """Also used by a helper already waiting when the cutoff is reached."""
    record = read(marker(work), {})
    if record.get('status') == 'complete':
        return False
    if record.get('status') not in ACTIVE:
        return True
    deadline = record.get('deadline')
    if deadline is None:
        received = observed_receipt(work, record['message_id'])
        if received is None:
            return True
        deadline = received + record['seconds']
    return (time.time() if now is None else now) < deadline - 3600


def inspect_target(run, worker, allow_pool=False):
    run = Path(run).resolve()
    config = read(run / 'run.json')
    selected = next((w for w in (config or {}).get('workers', []) if w['name'] == worker), None)
    if selected is None or not NAME.fullmatch(worker):
        raise ValueError('Named worker not found in this roster.')
    work = run / worker
    if not work.is_dir():
        raise ValueError('Worker workspace must be an existing directory.')
    if work.is_symlink():
        # Historic rosters point at independent workspaces outside agent_data.
        # Keep the alias, but never replace a workspace referenced by two rosters.
        target = work.resolve(strict=True)
        if target.stat().st_uid != run.stat().st_uid:
            raise ValueError('Workspace alias and roster must have the same owner.')
        for settings in run.parent.glob('*/run.json'):
            other_run = settings.parent
            other = read(settings, {})
            for person in other.get('workers', []):
                candidate = other_run / person['name']
                if candidate == work:
                    continue
                if candidate.exists() and candidate.resolve() == target:
                    raise ValueError('Workspace alias is shared by another roster; leave both agents unchanged.')
    if not config.get('live_input'):
        raise ValueError('Native message receipts are not enabled for this roster.')
    if worker in config.get('gpu_pool', {}) and not allow_pool:
        raise ValueError('Cross-host gpu-pool cannot enforce the final-hour start cutoff yet; leave this agent running.')
    if (run / 'STOP').exists() or (work / '.runtime/PAUSE').exists():
        raise ValueError('Paused or stopped workers cannot be armed.')
    return run, work, config, selected


def receipt(record):
    return {**{k: record.get(k) for k in ('id', 'status', 'run', 'worker', 'message_id',
              'successor_run', 'account_policy', 'operation', 'error', 'waiting_for', 'pool_error')},
            **{k: iso(record.get(k)) for k in ('created_at', 'received_at', 'deadline', 'completed_at')},
            'receipt_meaning': ('Direct supervisor renewal; no notice receipt or notice period.'
                                if record.get('operation') == 'supervisor_renewal' else
                                'Native conversation accepted the notice; this does not claim a thoughtful response.')}


class Timers:
    def __init__(self, backend, clock=time.time):
        self.backend = backend
        self.clock = clock

    def renew_supervisor(self, run, first_instruction, dry_run=False):
        """An explicitly ordered fresh supervisor session, without a notice timer."""
        run, work, config, selected = inspect_target(run, 'supervisor')
        if (config.get('role') != 'supervisor' or len(config['workers']) != 1
                or selected.get('gpus') or config.get('gpu_pool')):
            raise ValueError('Immediate renewal is only for a supervisor-only roster without compute.')
        if not first_instruction.strip():
            raise ValueError('Supply a nonempty first-instruction file.')
        self.backend.allow_unregistered = False
        self.backend.validate(run, work, config, selected)
        if dry_run:
            return dict(status='dry_run_ok', run=str(run), worker='supervisor', armed=False,
                        account_policy='reuse_existing', identity='fresh conversation and Unix account')
        path = timer_path(run, 'supervisor')
        with lock(path.with_suffix('.lock')):
            old = read(path, {})
            if old.get('status') in ACTIVE | {'complete'}:
                raise ValueError('This supervisor already has an active transition or has been replaced.')
            from restart_transition import workspace_identity
            ident = secrets.token_hex(6)
            now = self.clock()
            record = dict(id=ident, run=str(run), workspace=str(work), workspace_identity=workspace_identity(work),
                          worker='supervisor', status='armed', seconds=0, created_at=now,
                          received_at=None, deadline=now, account_policy='reuse_existing',
                          operation='supervisor_renewal', first_instruction=first_instruction.strip(),
                          config=config, selected=selected, after=[], pool_plan=None,
                          successor_run=str(run.parent / (run.name[:48] + '_fresh_' + ident)),
                          unix_user='rsr_' + ident)
            save(path, record)
            write_marker(record)
            return receipt(record)

    def arm(self, run, worker, notice, first_instruction, hours=8, dry_run=False, after=(), unregistered=False, pool_plan=None):
        if not math.isfinite(hours) or hours < 1:
            raise ValueError('Notice must be at least one hour, including the final-hour GPU cutoff.')
        if not notice.strip() or not first_instruction.strip():
            raise ValueError('Supply nonempty notice and first-instruction files.')
        run, work, config, selected = inspect_target(run, worker, allow_pool=pool_plan is not None)
        prepared_pool = None
        if pool_plan is not None:
            from restart_pool import load_plan
            prepared_pool = load_plan(pool_plan, run, worker)
            if worker not in config.get('gpu_pool', {}):
                raise ValueError('A Pool plan requires a Pool-enabled target.')
        self.backend.allow_unregistered = False
        if unregistered:
            from restart_transition import credential_path
            try: credential_path(work)
            except ValueError: pass
            else: raise ValueError('This target has saved credentials; preserve its existing account.')
            if not selected.get('invitation_code'):
                raise ValueError('An unregistered successor needs its prepared invitation.')
            self.backend.allow_unregistered = True
        self.backend.validate(run, work, config, selected)
        dependencies = []
        for dependency_run, dependency_worker in after:
            dependency_run = Path(dependency_run).resolve()
            if not NAME.fullmatch(dependency_worker) or not (dependency_run / dependency_worker).is_dir():
                raise ValueError('Unknown dependency worker.')
            if dependency_run.parent != run.parent or (dependency_run == run and dependency_worker == worker):
                raise ValueError('Dependencies must be other workers in the same toolkit.')
            dependency_path = timer_path(dependency_run, dependency_worker)
            other = read(dependency_path, {})
            if other and other.get('status') not in ACTIVE | {'complete'}:
                raise ValueError('A dependency was cancelled or blocked; review it before arming the supervisor.')
            if other.get('after'):
                raise ValueError('Chained dependencies are not supported; list the researchers directly.')
            dependencies.append(dict(path=str(dependency_path), id=other.get('id'),
                                     target=dependency_run.name + ':' + dependency_worker))
        if dependencies and config.get('role') != 'supervisor':
            raise ValueError('Only supervisors may wait for researcher replacements.')
        if dry_run:
            return dict(status='dry_run_ok', run=str(run), worker=worker, hours=hours,
                        account_policy='register_new' if unregistered else 'reuse_existing', identity='fresh conversation and Unix account',
                        readonly='predecessor remains in place; successor receives read-only ACL access',
                        armed=False)
        path = timer_path(run, worker)
        with lock(path.with_suffix('.lock')):
            old = read(path, {})
            if old.get('status') in ACTIVE or old.get('status') == 'complete':
                raise ValueError('This worker already has an active timer or has been replaced.')
            ident = prepared_pool['id'] if prepared_pool else secrets.token_hex(6)
            from restart_transition import workspace_identity
            record = dict(id=ident, run=str(run), workspace=str(work), workspace_identity=workspace_identity(work), worker=worker,
                          status='awaiting_receipt', seconds=hours * 3600, created_at=self.clock(),
                          received_at=None, deadline=None, account_policy='register_new' if unregistered else 'reuse_existing',
                          notice=notice.strip(), first_instruction=first_instruction.strip(),
                          config=config, selected=selected,
                          successor_run=str(run.parent / (run.name[:48] + '_fresh_' + ident)),
                          unix_user='rsr_' + ident, after=dependencies, pool_plan=prepared_pool)
            # Persist intent before queueing. tick repairs a crash between these writes.
            save(path, record)
            self._send(record)
            save(path, record)
            write_marker(record)
            return receipt(record)

    def _send(self, record):
        record['message_id'] = Messages(record['workspace']).add(
            record['notice'] + f"\n\nThe replacement window is {record['seconds']/3600:g} hours from this notice's native receipt. "
            'Continue working, prepare HANDOVER.md, and start no new GPU experiment during the final hour. '
            'Existing GPU jobs may finish before the deadline. The operator can cancel this timer.',
            source='Message from your human operator', priority='owner',
            delivery_key='restart-notice:' + record['id'])

    def _observe(self, record):
        if record['status'] == 'awaiting_receipt' and record.get('message_id'):
            accepted = observed_receipt(record['workspace'], record['message_id'])
            if accepted is not None:
                record.update(status='armed', received_at=accepted, deadline=accepted + record['seconds'])
                return True
        return False

    def status(self, path):
        with lock(Path(path).with_suffix('.lock')):
            record = read(path)
            if record and self._observe(record):
                save(path, record)
                write_marker(record)
            return receipt(record) if record else None

    def cancel(self, path):
        with lock(Path(path).with_suffix('.lock')):
            record = read(path)
            if not record:
                raise ValueError('No timer exists for this worker.')
            if record['status'] not in {'awaiting_receipt', 'armed', 'cancelling', 'cancelled', 'blocked'}:
                raise ValueError('Replacement has begun or completed; inspect status before any intervention.')
            if record['status'] != 'cancelled':
                record.update(status='cancelling', cancelled_at=self.clock())
                save(path, record)
                self._finish_cancel(path, record)
            return receipt(record)

    def retry(self, path):
        """Retry an approved failed transition without restarting its notice clock."""
        with lock(Path(path).with_suffix('.lock')):
            record = read(path)
            if not record or record.get('status') != 'blocked':
                raise ValueError('Only a blocked replacement can be retried.')
            if record.get('deadline') is None or (record.get('received_at') is None
                    and record.get('operation') != 'supervisor_renewal'):
                raise ValueError('The original notice must have a confirmed receipt.')
            if record.get('pool_plan'):
                raise ValueError('A blocked Pool transfer needs a fresh reviewed plan; its cleared cutoff cannot be reused.')
            self.backend.allow_unregistered = record.get('account_policy') == 'register_new'
            self.backend.validate(Path(record['run']), Path(record['workspace']), record['config'], record['selected'])
            record.setdefault('previous_errors', []).append(dict(error=record.get('error'), at=self.clock()))
            record.update(status='armed', error=None, pool_error=None)
            save(path, record)
            write_marker(record)
            return receipt(record)

    def _finish_cancel(self, path, record):
        from restart_pool import clear_cutoff
        try:
            clear_cutoff(record)
        except Exception as exc:
            record['pool_error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            save(path, record)
            return
        record.update(status='cancelled', pool_error=None)
        save(path, record)
        self._cancel_notice(record)
        write_marker(record)

    def _rollback(self, path, record):
        try:
            self.backend.rollback(record)
        except Exception as exc:
            record['pool_error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            save(path, record)
            return
        record.update(status='blocked', pool_error=None)
        save(path, record)
        write_marker(record)
        Messages(record['workspace']).add('The scheduled replacement could not be completed. Continue working; the operator must inspect restart-timer status before rearming.',
            source='Replacement service', delivery_key='restart-blocked:' + record['id'])

    def _cancel_notice(self, record):
        messages = Messages(record['workspace'])
        messages.cancel_queued('restart-notice:' + record['id'])
        messages.add('The human operator has cancelled the scheduled replacement. Continue your research normally; the timer GPU cutoff is lifted.',
                     source='Message from your human operator', priority='owner',
                     delivery_key='restart-cancel:' + record['id'])

    def tick(self, path):
        path = Path(path)
        with lock(path.with_suffix('.lock')):
            record = read(path)
            if not record:
                return
            if record['status'] in {'cancelled', 'cancelling'}:
                self._finish_cancel(path, record)
                return
            if record['status'] == 'rolling_back':
                self._rollback(path, record)
                return
            if record['status'] in TERMINAL:
                return
            if record['status'] == 'awaiting_receipt':
                self._send(record)
                self._observe(record)
                save(path, record)
                write_marker(record)
            if record.get('pool_plan') and record.get('deadline'):
                from restart_pool import sync_cutoff
                try:
                    sync_cutoff(record)
                except Exception as exc:
                    record['pool_error'] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    save(path, record)
                    return
                if record.pop('pool_error', None): save(path, record)
            if record.get('deadline') is None or self.clock() < record['deadline']:
                return
            if record['status'] == 'armed':
                waiting = []
                for dependency in record.get('after', []):
                    other = read(dependency['path'], {})
                    if dependency['id'] is None and other.get('status') in ACTIVE | {'complete'}:
                        dependency['id'] = other['id']
                    if dependency['id'] is None or other.get('id') != dependency['id'] or other.get('status') != 'complete':
                        waiting.append(dependency['target'])
                record['waiting_for'] = waiting
                save(path, record)
                if waiting:
                    return
            try:
                if record['status'] in {'armed', 'preparing'}:
                    record['status'] = 'preparing'; save(path, record)
                    self.backend.prepare(record)
                    record['status'] = 'pausing'; save(path, record)
                if record['status'] == 'pausing':
                    self.backend.stop_predecessor(record)
                    if record.get('pool_plan'):
                        from restart_pool import stop_jobs
                        if not stop_jobs(record): return
                    record['status'] = 'stopped'; save(path, record)
                if record['status'] == 'stopped':
                    if record.get('pool_plan'):
                        from restart_pool import activate
                        record['pool_activation_requested'] = True
                        save(path, record)
                        activate(record)
                    self.backend.transfer(record)
                    record['status'] = 'launching'; save(path, record)
                if record['status'] == 'launching':
                    self.backend.start(record)
                    record.update(status='complete', completed_at=self.clock())
                    save(path, record)
                    write_marker(record)
            except Exception as exc:
                record.update(status='rolling_back', error=str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
                save(path, record)
                write_marker(record)
                self._rollback(path, record)


def cli(argv):
    from main import run_dir
    from restart_transition import HostTransition
    p = argparse.ArgumentParser(description='Opt-in single-worker replacement; arm only after individual owner approval.')
    sub = p.add_subparsers(dest='action', required=True)
    renew = sub.add_parser('renew-supervisor', help='Owner-approved fresh supervisor session without a notice period.')
    renew.add_argument('run')
    renew.add_argument('--first-instruction-file', type=Path, required=True)
    renew.add_argument('--dry-run', action='store_true')
    arm = sub.add_parser('arm')
    arm.add_argument('run'); arm.add_argument('worker')
    arm.add_argument('--file', type=Path, required=True)
    arm.add_argument('--first-instruction-file', type=Path, required=True)
    arm.add_argument('--hours', type=float, default=8)
    arm.add_argument('--dry-run', action='store_true')
    arm.add_argument('--pool-plan', type=Path, help='Root-owned prepared successor Pool project and quota transfer.')
    arm.add_argument('--unregistered', action='store_true', help='Explicitly allow a verified never-registered target with a prepared invitation.')
    arm.add_argument('--after', action='append', default=[], metavar='RUN:WORKER',
                     help='Supervisor waits for these specific researcher replacements, even if not armed yet.')
    arm.add_argument('--wait-seconds', type=float, default=30)
    sub.add_parser('resume-service', help='Recover the independent timer service without restarting researchers.')
    for name in ('status', 'cancel', 'retry'):
        item = sub.add_parser(name)
        item.add_argument('run', nargs='?' if name == 'status' else None)
        item.add_argument('worker', nargs='?' if name == 'status' else None)
    args = p.parse_args(argv)
    if os.geteuid() != 0:
        raise ValueError('Use the host operator account (root) for restart-timer controls.')
    timers = Timers(HostTransition())
    if args.action == 'renew-supervisor':
        result = timers.renew_supervisor(run_dir(args.run), args.first_instruction_file.read_text(), args.dry_run)
        if not args.dry_run:
            from restart_service import ensure_service
            ensure_service(run_dir(args.run).parent)
        print(json.dumps(result, indent=2))
    elif args.action == 'arm':
        if not math.isfinite(args.wait_seconds) or not 0 <= args.wait_seconds <= 60:
            raise ValueError('--wait-seconds must be between 0 and 60.')
        dependencies = []
        for target in args.after:
            if ':' not in target:
                raise ValueError('--after requires RUN:WORKER.')
            other_run, other_worker = target.rsplit(':', 1)
            dependencies.append((run_dir(other_run), other_worker))
        result = timers.arm(run_dir(args.run), args.worker, args.file.read_text(),
                            args.first_instruction_file.read_text(), args.hours, args.dry_run, dependencies, args.unregistered, args.pool_plan)
        if not args.dry_run:
            from restart_service import ensure_service
            ensure_service(run_dir(args.run).parent)
            until = time.monotonic() + args.wait_seconds
            path = timer_path(run_dir(args.run), args.worker)
            while time.monotonic() < until:
                result = timers.status(path)
                if result.get('received_at') or result['status'] != 'awaiting_receipt':
                    break
                time.sleep(.25)
        print(json.dumps(result, indent=2))
    elif args.action == 'resume-service':
        from restart_service import ensure_service, service_status
        ensure_service(run_dir('unused').parent)
        print(json.dumps(service_status(run_dir('unused').parent), indent=2))
    elif args.action == 'retry':
        result = timers.retry(timer_path(run_dir(args.run), args.worker))
        from restart_service import ensure_service
        ensure_service(run_dir(args.run).parent)
        print(json.dumps(result, indent=2))
    elif args.action == 'cancel':
        print(json.dumps(timers.cancel(timer_path(run_dir(args.run), args.worker)), indent=2))
    else:
        if args.worker and not args.run:
            raise ValueError('Specify RUN with WORKER.')
        paths = [timer_path(run_dir(args.run), args.worker)] if args.worker else sorted(
            (run_dir('unused').parent / '.restart-timers').glob('*.json'))
        rows = [timers.status(path) for path in paths if path.name != 'service.json']
        from restart_service import service_status
        print(json.dumps({'service': service_status(run_dir('unused').parent), 'timers':
            [row for row in rows if row and (not args.run or Path(row['run']).name == args.run)]}, indent=2))

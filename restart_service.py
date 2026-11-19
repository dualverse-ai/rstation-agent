"""Root-owned service independent of research controllers. No timers by default."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from common import ROOT, lock, read, save, alive, pid_token
from restart_timer import Timers
from restart_transition import HostTransition


def service_status(root):
    record = read(Path(root) / '.restart-timers/service.json', {})
    return {'running': alive(record.get('pid'), record.get('token')),
            'heartbeat': record.get('heartbeat'),
            'heartbeat_age_seconds': max(0, time.time() - record['heartbeat']) if record.get('heartbeat') else None}


def ensure_service(root):
    root = Path(root).resolve()
    directory = root / '.restart-timers'
    directory.mkdir(mode=0o700, exist_ok=True)
    try:
        handle = lock(directory / 'guard.lock', True)
    except BlockingIOError:
        handle = None
    if handle is not None:
        handle.close()
        with (directory / 'service.log').open('ab') as log:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve()), str(root), '--guard'],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, close_fds=True)
    until = time.monotonic() + 10
    while time.monotonic() < until:
        state = service_status(root)
        if state['running']:
            return
        time.sleep(.1)
    raise ValueError('Replacement service did not start. The durable notice remains queued; use restart-timer resume-service and inspect status.')


def tick(root):
    timers = Timers(HostTransition())
    paths = [p for p in sorted((Path(root) / '.restart-timers').glob('*.json')) if p.name != 'service.json']
    # Freeze all observed receipts before a potentially slow host transition.
    for path in paths:
        timers.status(path)
    for path in paths:
        try:
            timers.tick(path)
            record = read(path, {})
            if record.get('status') == 'complete':
                # The root service owns cross-user mailbox delivery. Research
                # model calls continue in each successor's unprivileged controller.
                from supervision import binding, tick as mailbox_tick
                from mailbox_store import Mailbox
                work = Path(record['successor_run']) / record['worker']
                bound = read(binding(work))
                if bound:
                    box = Mailbox(bound['database'])
                    # Repair native outbox writes that raced with participant transfer.
                    from messages import Messages
                    keys = {m.get('delivery_key') for m in Messages(work).list()}
                    with box.transaction() as con:
                        for row in con.execute('SELECT id FROM messages WHERE recipient=? AND archived IS NULL AND native_id IS NOT NULL', (bound['actor'],)).fetchall():
                            if f'supervision:{box._setting(con,"id")}:{row["id"]}' not in keys:
                                con.execute('UPDATE messages SET native_id=NULL WHERE id=?', (row['id'],))
                    mailbox_tick({'supervision_database': bound['database']})
        except Exception as exc:
            # Avoid provider details, source text or secrets in the service log.
            print(json.dumps({'timer': path.stem, 'error': type(exc).__name__}), flush=True)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path); p.add_argument('--guard', action='store_true')
    args = p.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Run the replacement service as the host operator (root).')
    directory = args.root / '.restart-timers'; directory.mkdir(mode=0o700, exist_ok=True)
    if args.guard:
        with lock(directory / 'guard.lock', True):
            while True:
                subprocess.run([sys.executable, str(Path(__file__).resolve()), str(args.root)])
                time.sleep(5)
    else:
        with lock(directory / 'service.lock', True):
            while True:
                save(directory / 'service.json', {'pid': os.getpid(), 'token': pid_token(os.getpid()), 'heartbeat': time.time()})
                tick(args.root)
                time.sleep(2)


if __name__ == '__main__':
    main()

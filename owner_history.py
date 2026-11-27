"""Receipt-based owner instruction history. No CLI input or process control."""
import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from common import lock, read, save

REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
POINTER = ('Earlier owner messages and their receipt times are in user_instruction.txt; '
           're-check relevant entries when planning, apply newer instructions where they conflict, '
           'and treat older instructions as potentially outdated. '
           'The launcher maintains this file; keep your own notes elsewhere.')
START = '<!-- rstation owner message history -->'
END = '<!-- /rstation owner message history -->'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def provenance(reference):
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 240 or any(ord(c) < 32 for c in reference):
        raise ValueError('Provide a short owner instruction reference without control characters.')
    return {'kind': 'owner', 'reference': reference.strip()}


def owner_of(work):
    s = Path(work).stat()
    return (s.st_uid, s.st_gid) if os.geteuid() == 0 else None


def regular(path):
    if path.is_symlink() or (path.exists() and not stat.S_ISREG(path.stat().st_mode)):
        raise ValueError('History and instruction paths must be ordinary files.')


def atomic_text(path, content, owner):
    regular(path)
    fd, name = tempfile.mkstemp(prefix='.owner-history-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            if owner is not None:
                os.fchown(stream.fileno(), *owner)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def receipt(row):
    stamp = row.get('first_received_at') or row.get('received_at')
    if not stamp:
        return None
    parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Native receipt needs an explicit timezone.')
    return parsed.astimezone(timezone.utc).isoformat()


def render(entries):
    text = ['Owner instruction history\n\n'
            'Recorded from native receipt evidence. Older entries may be outdated; '
            'this is a reference, not a list of tasks to replay.\n'
            'Maintained by the launcher; keep your notes elsewhere.\n']
    for e in entries:
        utc = datetime.fromisoformat(e['received_at'])
        hkt = utc.astimezone(timezone(timedelta(hours=8)))
        text.append('\n---\nMessage {id}{historical}\nReceived: {utc} UTC / {hkt} HKT\n'
                    'Owner reference: {reference}\n\n{text}\n'.format(
                        id=e['id'], historical=' (historical backfill)' if e.get('historical') else '',
                        utc=utc.strftime('%Y-%m-%d %H:%M:%S.%f'), hkt=hkt.strftime('%Y-%m-%d %H:%M:%S.%f'),
                        reference=e['owner_message']['reference'], text=e['text']))
    return ''.join(text).encode('utf-8')


def sync_locked(work, data, owner=None):
    """Caller holds operator_messages.lock, also used by old live wrappers."""
    work = Path(work)
    runtime = work / '.runtime'
    ledger_path = runtime / 'owner_history.json'
    target = work / 'user_instruction.txt'
    regular(ledger_path)
    regular(target)
    ledger = read(ledger_path, {'entries': []})
    entries = ledger['entries']
    known = {e['id']: e for e in entries}
    additions = []
    for row in data.get('messages', []):
        mark = row.get('owner_message')
        if not isinstance(mark, dict) or mark.get('kind') != 'owner':
            continue
        mark = provenance(mark.get('reference'))
        when = receipt(row)
        if when is None:
            continue
        if row['id'] in known:
            # Legacy wrappers can overwrite receipts before the new hook loads.
            # Preserve the original history receipt across that migration.
            when = known[row['id']]['received_at']
        e = {'id': row['id'], 'received_at': when, 'text': row['text'], 'owner_message': mark}
        if row.get('owner_history_backfill'):
            e['historical'] = True
        if row['id'] in known:
            # First receipt and the original text must never be rewritten.
            if e != known[row['id']]:
                raise ValueError('Owner message changed after its history was recorded.')
        else:
            additions.append(e)
    entries.extend(sorted(additions, key=lambda e: (e['received_at'], e['id'])))
    if not ledger_path.exists() or additions:
        save(ledger_path, ledger, owner=owner)
    content = render(entries)
    old = target.read_bytes() if target.exists() else None
    if old is not None and old != content and sha(old) != ledger.get('render_sha256'):
        backup = runtime / ('user_instruction.conflict-' + sha(old) + '.txt')
        regular(backup)
        if not backup.exists():
            atomic_text(backup, old, owner)
    if old != content:
        atomic_text(target, content, owner)
    if ledger.get('render_sha256') != sha(content):
        ledger['render_sha256'] = sha(content)
        save(ledger_path, ledger, owner=owner)
    status = {'revision': REVISION, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'entries': len(entries), 'history_sha256': sha(content), 'error': None}
    save(runtime / 'owner_history_status.json', status, owner=owner)
    return status


def sync_safely(work, data, owner=None):
    """A history disk error must not turn successful native receipt into failure."""
    try:
        return sync_locked(work, data, owner)
    except (OSError, ValueError, KeyError, TypeError):
        # No raw exceptions or message bodies in logs/status.
        status = {'revision': REVISION, 'checked_at': datetime.now(timezone.utc).isoformat(),
                  'error': 'history_sync_failed'}
        try:
            save(Path(work) / '.runtime/owner_history_status.json', status, owner=owner)
        except OSError:
            pass
        return status


def install_pointer_locked(work, owner):
    for name in ('AGENTS.md', 'CLAUDE.md', 'GEMINI.md'):
        path = work / name
        regular(path)
        text = path.read_text() if path.exists() else ''
        block = START + '\n' + POINTER + '\n' + END
        pattern = re.escape(START) + '.*?' + re.escape(END)
        if START in text or END in text:
            if text.count(START) != 1 or text.count(END) != 1 or not re.search(pattern, text, flags=re.S):
                raise ValueError('Invalid owner history pointer block; preserve existing instructions.')
            new = re.sub(pattern, lambda _: block, text, flags=re.S)
        else:
            new = text + ('\n\n' if text and not text.endswith('\n\n') else '') + block + '\n'
        if new != text:
            atomic_text(path, new.encode('utf-8'), owner)


def sync(workspace, *, backfill=None, install_pointer=False):
    work = Path(workspace).resolve(strict=True)
    runtime = work / '.runtime'
    if runtime.is_symlink():
        raise ValueError('Runtime must be an ordinary directory.')
    owner = owner_of(work)
    if not runtime.exists():
        runtime.mkdir(mode=0o700)
        if owner is not None:
            os.chown(runtime, *owner)
    queue = runtime / 'operator_messages.json'
    regular(queue)
    with lock(runtime / 'operator_messages.lock') as handle:
        if owner is not None:
            os.fchown(handle.fileno(), *owner)
        data = read(queue, {'next': 1, 'messages': []})
        if backfill is not None:
            rows = {m['id']: m for m in data['messages']}
            planned = []
            seen = set()
            for item in backfill:
                number = item['message_id']
                if number in seen or number not in rows:
                    raise ValueError('Backfill must name existing unique message IDs.')
                seen.add(number)
                row = rows[number]
                mark = provenance(item['reference'])
                if sha(row['text'].encode('utf-8')) != item['text_sha256'] or receipt(row) is None:
                    raise ValueError('Backfill needs matching text and recorded native receipt.')
                if row.get('owner_message') not in (None, mark):
                    raise ValueError('Backfill conflicts with existing owner attribution.')
                planned.append((row, mark))
            for row, mark in planned:
                if 'owner_message' not in row:
                    row['owner_message'] = mark
                    row['owner_history_backfill'] = True
            if planned:
                save(queue, data, owner=owner)
        result = sync_locked(work, data, owner)
        if install_pointer:
            install_pointer_locked(work, owner)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    one = sub.add_parser('sync')
    one.add_argument('workspace', type=Path)
    one.add_argument('--backfill', type=Path, help='Reviewed JSON list of message_id, text_sha256 and owner reference.')
    one.add_argument('--install-pointer', action='store_true')
    watch = sub.add_parser('watch')
    watch.add_argument('manifest', type=Path, help='JSON list of existing absolute worker workspace paths.')
    watch.add_argument('--interval', type=float, default=5)
    watch.add_argument('--status', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == 'sync':
        print(json.dumps(sync(args.workspace, backfill=read(args.backfill) if args.backfill else None,
                              install_pointer=args.install_pointer)))
        return
    if args.interval < 1:
        parser.error('Interval must be at least one second.')
    # A manifest deliberately enrolls workspaces; never discover or revive retired runs.
    paths = read(args.manifest)
    if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and Path(p).is_absolute() for p in paths):
        parser.error('Manifest must be a nonempty list of absolute workspaces.')
    while True:
        results = []
        for path in dict.fromkeys(paths):
            try:
                result = sync(path)
            except (OSError, ValueError, KeyError, TypeError):
                result = {'error': 'history_sync_failed'}
            results.append({'workspace': path, **result})
        save(args.status, {'revision': REVISION, 'checked_at': datetime.now(timezone.utc).isoformat(), 'workers': results})
        time.sleep(args.interval)


if __name__ == '__main__':
    main()

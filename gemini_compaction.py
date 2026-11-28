"""One native checkpoint for an oversized Gemini conversation, with a restorable backup."""
from collections import deque
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid

from common import lock, read, save
from gemini_stream import GeminiStream


def note_excerpt(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ''
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return ''
        first = stream.read(12000)
        if info.st_size > 12000:
            stream.seek(-3000, os.SEEK_END)
            first = first[:9000] + b'\n[...]\n' + stream.read(3000)
        return path.name + '\n' + first.decode(errors='replace')


def summary_body(raw, work):
    """Only the explicit native /compact directive can use the abbreviated history."""
    body = json.loads(raw)
    history = body['contents']
    final = history[-1]
    directive = next((part.get('text') for part in reversed(final['parts'])
                      if 'summary that will allow' in (part.get('text') or '').lower()), None)
    if final.get('role') != 'user' or not directive or len(directive) > 16000:
        raise ValueError('No native summary directive')
    notes = [note_excerpt(work / name) for name in ('IDENTITY.md', 'CONTINUATION.md', 'PLAN.md', 'HANDOVER.md')]
    recent = deque(maxlen=80)
    for turn in history[-90:]:
        for part in turn['parts']:
            if part.get('thought') or part.get('text') == directive:
                continue
            value = part.get('text') or part.get('functionCall') or part.get('functionResponse')
            if value:
                excerpt = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                recent.append(turn.get('role', '') + ' ' + excerpt[:520])
    context = '\n'.join(filter(None, notes)) + '\n' + '\n'.join(recent)[-35000:]
    body['contents'] = [{'role': 'user', 'parts': [{'text':
        'Workspace records and recent conversation excerpts follow. Large outputs have been abbreviated.\n'
        + context + '\n' + directive}]}]
    body.pop('tools', None)
    body.pop('toolConfig', None)
    return json.dumps(body, ensure_ascii=False).encode()


def backup_conversation(database, backup):
    source_uri = database.as_uri() + '?mode=ro'
    deadline = time.monotonic() + 90

    def check_deadline(_status, _remaining, _total):
        if time.monotonic() >= deadline:
            raise TimeoutError('Conversation backup timed out')

    with closing(sqlite3.connect(source_uri, uri=True)) as src:
        size = src.execute('pragma page_count').fetchone()[0] * src.execute('pragma page_size').fetchone()[0]
        if shutil.disk_usage(backup.parent).free < size + 64 * 1024 * 1024:
            raise OSError('Insufficient space for a restorable backup')
        with tempfile.TemporaryDirectory(prefix='.compact-', dir=backup.parent) as temp:
            staged = Path(temp) / 'snapshot.db'
            with closing(sqlite3.connect(staged)) as dest:
                src.backup(dest, pages=256, progress=check_deadline)
                last_step, = dest.execute('select max(idx) from steps').fetchone()
            if type(last_step) is not int:
                raise ValueError('No conversation steps')
            staged.chmod(0o600)
            os.replace(staged, backup)
    return last_step


def checkpoint_step(transcript, sid, original_last_step):
    transcript.seek(0)
    checkpoint = None
    for line in transcript:
        if not line.startswith(b'{'):
            continue
        event = json.loads(line)
        item = event.get('step_update') or event.get('result') or event
        if item.get('conversation_id') and item['conversation_id'] != sid:
            return None
        index = item.get('step_index')
        if (event.get('event') == 'step_update' and item.get('conversation_id') == sid
                and item.get('step_type') == 'checkpoint' and item.get('state') == 'DONE'
                and type(index) is int and index > original_last_step):
            checkpoint = index
    return checkpoint


def compact_once(work, sid, command, env, launch):
    """Return true only after native stdout and the same database agree on a new checkpoint."""
    folder = work / '.runtime'
    receipt = folder / 'gemini-compaction.json'
    try:
        with lock(folder / 'gemini-compaction.lock', nonblocking=True) as fence:
            previous = read(receipt, {})
            if previous.get('launch') == launch and previous.get('session') == sid:
                return previous.get('status') == 'checkpoint'
            record = dict(session=sid, launch=launch, status='started', started_at=time.time())
            save(receipt, record)
            try:
                args = list(command)
                if (str(uuid.UUID(sid)) != sid or args.count('--conversation') != 1
                        or args[args.index('--conversation') + 1] != sid
                        or Path(env['HOME']).resolve() != (work / '.home').resolve()):
                    raise ValueError('Conversation identity mismatch')
                args[args.index('-p') + 1] = '/compact'
                args[args.index('--print-timeout') + 1] = '150s'
                database = work / '.home/.gemini/antigravity-cli/conversations' / f'{sid}.db'
                backup = folder / f'pre-compact-{sid}.sqlite'
                last_step = backup_conversation(database, backup)
                record.update(backup=backup.name, original_last_step=last_step)
                save(receipt, record)
                with GeminiStream(env['GOOGLE_GEMINI_BASE_URL'],
                                  transform_body=lambda raw: summary_body(raw, work)) as bridge:
                    with tempfile.TemporaryFile(dir=folder) as transcript:
                        subprocess.run(args, cwd=work, env=dict(env, GOOGLE_GEMINI_BASE_URL=bridge.url),
                                       stdin=subprocess.DEVNULL, stdout=transcript, stderr=subprocess.DEVNULL,
                                       timeout=180, pass_fds=(fence.fileno(),))
                        index = checkpoint_step(transcript, sid, last_step)
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                    # Native protobuf enums CORTEX_STEP_TYPE_CHECKPOINT and CORTEX_STEP_STATUS_DONE.
                    persisted = db.execute('select 1 from steps where idx=? and step_type=23 and status=3 '
                                           'and length(step_payload)>0', (index,)).fetchone()
                if persisted:
                    record.update(status='checkpoint', step=index)
            except (OSError, ValueError, TypeError, IndexError, KeyError, sqlite3.Error, subprocess.SubprocessError) as error:
                record['error_type'] = type(error).__name__
            finally:
                if record['status'] == 'started':
                    record['status'] = 'failed'
                record['finished_at'] = time.time()
                save(receipt, record)
            return record['status'] == 'checkpoint'
    except (OSError, ValueError):
        # A live peer or unreadable receipt cannot authorize another native invocation.
        return False

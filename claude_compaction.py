"""Recover from a native Claude context overflow in a private copy of the saved session."""
from collections import deque
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import sys
import time
import uuid

from common import lock, read, save


def overflow(event):
    if not (event.get('type') == 'result' and event.get('is_error') is True
            and event.get('stop_reason') != 'refusal'):
        return False
    result = event.get('result')
    if not isinstance(result, str):
        return False
    return bool(re.match(r'^API Error:\s*(?:(?:400|413)\b\s*)?.{0,200}?(?:上下文超长|prompt is too long|context length exceeded|maximum context length|context_length_exceeded)', result, re.I))


def digest(path):
    with path.open('rb') as stream:
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def exchange_offsets(path, sid):
    recent = deque(maxlen=8)
    pending = set()
    with path.open('rb') as stream:
        while line := stream.readline():
            row = json.loads(line)
            if row.get('isSidechain') or row.get('sessionId') != sid:
                continue
            if row.get('subtype') == 'compact_boundary':
                recent.clear()
                pending.clear()
            if row.get('isApiErrorMessage'):
                continue
            message = row.get('message') or {}
            parts = message.get('content')
            if not isinstance(parts, list):
                continue
            complete = False
            for part in parts:
                if part.get('type') == 'tool_use':
                    pending.add(part['id'])
                elif part.get('type') == 'tool_result' and part.get('tool_use_id') in pending:
                    pending.remove(part['tool_use_id'])
                    complete = True
            if not pending and (complete or (row.get('type') == 'assistant' and
                    message.get('stop_reason') == 'end_turn' and message.get('usage', {}).get('output_tokens'))):
                recent.append(stream.tell())
    return list(reversed(recent))


def checkpoint(path, sid, offset):
    boundary = None
    with path.open('rb') as stream:
        stream.seek(offset)
        for line in stream:
            row = json.loads(line)
            if row.get('sessionId') != sid:
                continue
            if row.get('subtype') == 'compact_boundary':
                boundary = row.get('uuid')
            if boundary and row.get('isCompactSummary') and row.get('message', {}).get('content'):
                return boundary
    return None


def compact_body(raw, work):
    from gemini_compaction import note_excerpt
    body = json.loads(raw)
    messages = body['messages']
    # This endpoint belongs to an isolated, tool-free /compact process.
    last_user = max(i for i, message in enumerate(messages) if message.get('role') == 'user')
    content = messages[last_user]['content']
    directive = next((p.get('text', '') for p in reversed(content) if p.get('type') == 'text'), '')
    if (body.get('tools') or not directive.startswith('CRITICAL: Respond with TEXT ONLY.')
            or '<summary>' not in directive or 'Do NOT call any tools' not in directive):
        raise ValueError('Expected the native Claude summary request')
    records = []
    for turn in messages[-130:]:
        if turn.get('role') not in ('assistant','user'):
            continue
        parts = turn.get('content', [])
        if isinstance(parts, str):
            parts = [{'type':'text','text':parts}]
        for part in parts:
            kind = part.get('type')
            if kind == 'text' and part.get('text') != directive:
                records.append(turn['role'] + ' ' + part['text'][:1000])
            elif kind == 'tool_use':
                records.append('Tool ' + str(part.get('name','')) + ' ' + str(part.get('input',{}))[:400])
            elif kind == 'tool_result':
                records.append('Result ' + str(part.get('content',''))[:400])
    notes = [note_excerpt(work / filename) for filename in ('user_instruction.txt', 'PLAN.md','CONTINUATION.md')]
    excerpts = '\n'.join(filter(None, notes)) + '\n' + '\n'.join(records)[-40000:]
    brief = {'role':'user','content':[{'type':'text','text':'Workspace notes and recent conversation excerpts for compaction:\n' + excerpts + '\n\n' + directive}]}
    # Preserve native system instructions before and after the summary request.
    body['messages'] = [m for m in messages[:last_user] if m.get('role') == 'system'] + [brief] + [
        m for m in messages[last_user+1:] if m.get('role') == 'system']
    return json.dumps(body,ensure_ascii=False).encode()


def queue_handoff(work, backup):
    from messages import Messages
    Messages(work).add(
        'The saved Claude conversation was compacted after a context overflow. Read user_instruction.txt '
        'and the latest continuation notes. Check files and active job receipts before repeating earlier work. '
        'The complete original conversation is archived at ' + str(backup.relative_to(work)) + '.',
        source='Monitor Admin', delivery_key='claude-compaction:' + backup.stem)


def compact(work, sid, binary, model, env):
    if str(uuid.UUID(sid)) != sid or Path(env['CLAUDE_CONFIG_DIR']).resolve() != work / '.claude':
        raise ValueError('Claude session identity mismatch')
    projects = work / '.claude/projects'
    source, = projects.glob('*/' + sid + '.jsonl')
    source.resolve().relative_to(projects.resolve())
    if source.is_symlink() or not source.is_file():
        raise ValueError('Expected a regular native journal')
    folder = work / '.runtime/claude-compaction' / sid
    folder.mkdir(exist_ok=True, parents=True, mode=0o700)
    with lock(folder / 'lock', nonblocking=True) as fence:
        offsets = exchange_offsets(source, sid)
        original = digest(source)
        receipt = folder / 'receipt.json'
        prev = read(receipt, {})
        # Repeated provider errors append records without completing another exchange.
        if prev.get('checkpoint_sha') == original:
            queue_handoff(work, folder / prev['backup'])
            return True
        if 'exchange' in prev and prev['exchange'] == offsets[:1]:
            return False
        backup = folder / (original + '.jsonl')
        if not backup.exists():
            with tempfile.NamedTemporaryFile(dir=folder) as stage:
                with source.open('rb') as inp:
                    shutil.copyfileobj(inp, stage)
                stage.flush()
                os.fsync(stage.fileno())
                if digest(Path(stage.name)) != original:
                    raise ValueError('Native journal changed during backup')
                os.link(stage.name, backup)
        if digest(backup) != original:
            raise ValueError('Backup verification failed')
        record = dict(backup=backup.name, exchange=offsets[:1], status='attempting', attempts=[])
        save(receipt, record)
        for index in (0, 3, 7):
            if index >= len(offsets):
                break
            offset = offsets[index]
            with tempfile.TemporaryDirectory(dir=folder) as tmp:
                config = Path(tmp) / 'config'
                candidate = config / 'projects' / source.parent.name / source.name
                candidate.parent.mkdir(parents=True)
                shutil.copyfile(backup, candidate)
                with candidate.open('rb+') as journal:
                    journal.truncate(offset)
                args = [binary, '--bare', '--print', '--verbose', '--resume', sid, '--model', model,
                        '--tools', '', '--permission-mode', 'dontAsk', '--setting-sources', '',
                        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--output-format', 'stream-json', '/compact']
                trial_env = dict(env, HOME=tmp, CLAUDE_CONFIG_DIR=str(config))
                from gemini_stream import GeminiStream
                bridge = (GeminiStream(env['ANTHROPIC_BASE_URL'], transform_body=lambda raw: compact_body(raw, work))
                          if index else nullcontext())
                try:
                    with bridge as endpoint:
                        if endpoint:
                            trial_env['ANTHROPIC_BASE_URL'] = endpoint.url
                        result = subprocess.run(args, cwd=work, env=trial_env, stdin=subprocess.DEVNULL,
                                            capture_output=True, timeout=480 if index else 150, pass_fds=(fence.fileno(),))
                    events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith(b'{')]
                    terminal = next((event for event in reversed(events) if event.get('type') == 'result'), {})
                    succeeded = (result.returncode == 0 and terminal.get('session_id') == sid
                                 and terminal.get('is_error') is False and terminal.get('stop_reason') != 'refusal')
                    if terminal.get('stop_reason') == 'refusal':
                        record.update(status='failed', finished_at=time.time())
                        save(receipt, record)
                        return False
                    boundary = checkpoint(candidate, sid, offset) if succeeded else None
                except subprocess.TimeoutExpired:
                    boundary = None
                record['attempts'].append(dict(exchange=index + 1, checkpoint=boundary))
                save(receipt, record)
                if not boundary:
                    continue
                if digest(source) != original:
                    raise ValueError('Native journal changed before checkpoint installation')
                with candidate.open('rb') as journal:
                    os.fsync(journal.fileno())
                os.chmod(candidate, source.stat().st_mode & 0o777)
                record.update(checkpoint_sha=digest(candidate), boundary=boundary, finished_at=time.time())
                save(receipt, record)
                os.replace(candidate, source)
                queue_handoff(work, backup)
                record['status'] = 'checkpoint'
                save(receipt, record)
                return True
        record.update(status='failed', finished_at=time.time())
        save(receipt, record)
        return False


def schedule(manager, name, session, failure):
    if getattr(session, 'backend', None) != 'claude' or failure.category not in ('claude_context_overflow', 'claude_compaction_failed'):
        return False
    state = manager.states[name]
    sid = state.get('token') or failure.resume_token
    progress = state.get('last_response', 0)
    previous = state.get('claude_compaction', {})
    repeated = (sid == previous.get('session') and progress == previous.get('response'))
    if not sid or repeated or failure.category == 'claude_compaction_failed':
        state.update(status='blocked', blocked_reason='claude_compaction_failed', pid=None, next=0,
                     last_error='Native Claude compaction could not recover the saved session')
    else:
        state.update(claude_compaction=dict(session=sid, response=progress, pending=True),
                     token=sid, status='retrying', next=time.time()+1, pid=None)
    manager.persist(name)
    manager.log(name, state.get('last_error') if state['status']=='blocked' else 'Preparing a backed-up native Claude checkpoint.')
    return True


if __name__ == '__main__':
    work, sid, native, model, *command = sys.argv[1:]
    try:
        ok = compact(Path(work).resolve(), sid, native, model, os.environ)
    except (OSError, ValueError, IndexError, KeyError, TypeError, subprocess.SubprocessError):
        ok = False
    if not ok:
        print(json.dumps(dict(type='result', subtype='error_during_execution', session_id=sid,
                              is_error=True, result='rstation: native Claude compaction failed')), flush=True)
        sys.exit(1)
    os.execvpe(command[0], command, os.environ)

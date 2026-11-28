import http.server
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import uuid

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gemini_compaction as compact
from antigravity import prepare_profile
from common import lock, read, save


@pytest.fixture
def conversation(tmp_path):
    sid = str(uuid.uuid4())
    folder = tmp_path / '.home/.gemini/antigravity-cli/conversations'
    folder.mkdir(parents=True)
    db = folder / f'{sid}.db'
    with sqlite3.connect(db) as conn:
        conn.execute('create table steps(idx integer primary key, step_type, status, step_payload)')
        conn.execute('insert into steps values(1,23,3,?)', (b'old saved summary',))
    env = {'HOME': str(tmp_path / '.home'), 'GOOGLE_GEMINI_BASE_URL': 'http://127.0.0.1:1'}
    command = ['agy', '--conversation', sid, '-p', 'continue', '--print-timeout', '0']
    return tmp_path, sid, db, command, env


@pytest.mark.parametrize('problem', ['stale', 'no_db_step', 'wrong_type', 'wrong_state', 'foreign_session',
                                     'timeout', 'wrong_command_sid', 'no_backup_space', 'none'])
def test_verify_backup_and_one_attempt(conversation, monkeypatch, problem):
    work, sid, db, command, env = conversation
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert args[args.index('-p') + 1] == '/compact'
        assert args[args.index('--conversation') + 1] == sid
        if problem == 'timeout':
            raise subprocess.TimeoutExpired('agy', 180)
        if problem not in ('stale', 'no_db_step'):
            with sqlite3.connect(db) as conn:
                conn.execute('insert into steps values(2,?,?,?)',
                             (15 if problem == 'wrong_type' else 23, 2 if problem == 'wrong_state' else 3, b'summary'))
        event = {'event': 'step_update', 'step_update': {
            'conversation_id': str(uuid.uuid4()) if problem == 'foreign_session' else sid,
            'step_type': 'checkpoint', 'state': 'DONE', 'step_index': 1 if problem == 'stale' else 2}}
        kwargs['stdout'].write(json.dumps(event).encode() + b'\n')

    monkeypatch.setattr(compact.subprocess, 'run', run)
    if problem == 'wrong_command_sid':
        command[2] = str(uuid.uuid4())
    if problem == 'no_backup_space':
        monkeypatch.setattr(compact.shutil, 'disk_usage', lambda path: type('Usage', (), {'free': 0}))
    assert compact.compact_once(work, sid, command, env, '4') is (problem == 'none')
    assert compact.compact_once(work, sid, command, env, '4') is (problem == 'none')
    assert len(calls) == (0 if problem in ('wrong_command_sid', 'no_backup_space') else 1)
    receipt = read(work / '.runtime/gemini-compaction.json')
    assert receipt['session'] == sid and receipt['status'] == ('checkpoint' if problem == 'none' else 'failed')
    if calls:
        backup = work / '.runtime' / receipt['backup']
        assert backup.stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(backup) as conn:
            assert conn.execute('select step_payload from steps').fetchall() == [(b'old saved summary',)]


def test_interrupted_and_concurrent_compaction_never_rerun(conversation, monkeypatch):
    work, sid, db, command, env = conversation
    monkeypatch.setattr(compact.subprocess, 'run', lambda *a, **kw: pytest.fail('another native invocation'))
    folder = work / '.runtime'
    with lock(folder / 'gemini-compaction.lock'):
        assert not compact.compact_once(work, sid, command, env, '7')
        assert not (folder / 'gemini-compaction.json').exists()
    save(folder / 'gemini-compaction.json', {'launch': '7', 'session': sid, 'status': 'started'})
    assert not compact.compact_once(work, sid, command, env, '7')
    assert read(folder / 'gemini-compaction.json')['status'] == 'started'


def test_shorten_only_summary_and_skip_linked_files(tmp_path):
    (tmp_path / 'outside').write_text('unrelated workspace secret')
    (tmp_path / 'PLAN.md').symlink_to(tmp_path / 'outside')
    (tmp_path / 'CONTINUATION.md').write_text('use a bounded pilot')
    directive = 'Provide a summary that will allow continuing the work.'
    body = dict(contents=[{'role': 'model', 'parts': [{'text': 'private thinking', 'thought': True},
                                                      {'functionResponse': {'response': 'R' * 10000}}]},
                          {'role': 'user', 'parts': [{'text': directive}]}], tools=['normal research tools'])
    actual = compact.summary_body(json.dumps(body).encode(), tmp_path)
    assert len(actual) < 1500 and b'use a bounded pilot' in actual
    assert b'private thinking' not in actual and b'unrelated workspace secret' not in actual and b'"tools"' not in actual
    body['contents'][-1]['role'] = 'model'
    with pytest.raises(ValueError):
        compact.summary_body(json.dumps(body).encode(), tmp_path)


@pytest.mark.skipif(not os.environ.get('RSTATION_TEST_ANTIGRAVITY_BIN'), reason='Requires the native agy binary')
def test_real_checkpoint_resume_and_file_read(tmp_path):
    secret = secrets.token_hex(7)
    fixture = tmp_path / 'fixture.txt'
    fixture.write_text(secret)
    prepare_profile(tmp_path, context_tokens=300000)
    (tmp_path / 'CONTINUATION.md').write_text('Run the bounded read test.')
    bodies = []
    phase = {'name': 'seed', 'tool': None}

    class Provider(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            bodies.append(body)
            parts = [{'text': 'Seed conversation.'}]
            if phase['name'] == 'compact':
                parts = [{'text': '<summary>Continue the bounded file read.</summary>'}]
            elif phase['name'] == 'read':
                responses = [part['functionResponse'] for turn in body['contents']
                             for part in turn['parts'] if 'functionResponse' in part]
                if responses:
                    text = secret if secret in json.dumps(responses[-1]) else 'Missing fixture result'
                    parts = [{'text': text}]
                else:
                    names = {f['name'] for group in body['tools'] for f in group.get('functionDeclarations', [])}
                    if 'view_file' in names:
                        tool, args = 'view_file', {'AbsolutePath': str(fixture), 'StartLine': 1, 'EndLine': 1}
                    else:
                        tool, args = 'read_file', {'Path': str(fixture)}
                    phase['tool'] = tool
                    parts = [{'functionCall': {'name': tool, 'args': args}}]
            data = {'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP'}],
                    'usageMetadata': {'promptTokenCount': 2000, 'candidatesTokenCount': 10}}
            streaming = 'streamGenerateContent' in self.path
            payload = (('data: ' + json.dumps(data) + '\n\n') if streaming else json.dumps(data)).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream' if streaming else 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    command = [os.environ['RSTATION_TEST_ANTIGRAVITY_BIN'], '--model', 'gemini-api://gemini-4-carbon',
               '--agent', 'rstation-researcher', '--output-format', 'stream-json', '--print-timeout', '25s',
               '--dangerously-skip-permissions', '-p', 'Prepare a synthetic test.']
    env = dict(HOME=str(tmp_path / '.home'), XDG_CONFIG_HOME=str(tmp_path / '.home/.config'),
               XDG_CACHE_HOME=str(tmp_path / '.cache'), PATH=os.environ['PATH'], NO_PROXY='*',
               GEMINI_API_KEY='fixture-only', GOOGLE_GEMINI_BASE_URL=f'http://127.0.0.1:{server.server_port}')
    def turn():
        result = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, timeout=35, check=True)
        return [json.loads(line) for line in result.stdout.splitlines() if line.startswith(b'{')]

    try:
        sid = next(event['conversation_id'] for event in turn() if event.get('conversation_id'))
        command += ['--conversation', sid]
        phase['name'] = 'compact'
        assert compact.compact_once(tmp_path, sid, command, env, 'one')
        assert any('Run the bounded read test.' in json.dumps(body['contents']) for body in bodies)
        phase['name'] = 'read'
        command[command.index('-p') + 1] = 'Read fixture.txt and give its content.'
        events = turn()
        assert phase['tool']
        assert any(e.get('step_update', {}).get('tool_name') == phase['tool']
                   and e['step_update']['state'] == 'DONE' for e in events)
        assert any(e.get('result', {}).get('response', '').strip() == secret
                   and e['result']['conversation_id'] == sid for e in events)
        settings = read(tmp_path / '.home/.gemini/antigravity-cli/settings.json')
        assert settings['customModelsConfig']['customModels']['gemini-api://gemini-4-carbon']['maxTokens'] == 300000
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

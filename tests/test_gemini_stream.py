import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemini_stream import GeminiStream, SSEComments, native_environment
from antigravity import prepare_profile


@pytest.mark.parametrize('width', [1, 2, 3, 17, 4096])
def test_fragmented_comments_and_line_endings(width):
    wire = (b':\r\n\r\n: PING\n\ndata: {"text": "colon: PING"}\r\n'
            b': middle\r\ndata: second\r\n\r\n: last\r\rdata: [DONE]\n\n')
    normalizer = SSEComments()
    actual = b''.join(normalizer.feed(wire[i:i + width]) for i in range(0, len(wire), width))
    actual += normalizer.feed(b'', final=True)
    assert actual == b'data: {"text": "colon: PING"}\ndata: second\n\ndata: [DONE]\n\n'


def test_incomplete_event_is_not_completed():
    normalizer = SSEComments()
    assert normalizer.feed(b'data: {"truncated"') == b''
    assert normalizer.feed(b'', final=True) == b'data: {"truncated"'


@pytest.fixture
def upstream():
    class API(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers['Content-Length']))
            self.server.calls.append((self.path, body, dict(self.headers)))
            status, headers, chunks = self.server.reply(self.path, body)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(sum(map(len, chunks))))
            self.end_headers()
            for chunk in chunks:
                self.wfile.write(chunk)
                self.wfile.flush()

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), API)
    server.calls = []
    server.reply = lambda path, body: (200, {'Content-Type': 'text/event-stream'}, [b': PING\n\ndata: {}\n\n'])
    server.url = f'http://127.0.0.1:{server.server_port}'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def post(url):
    return urlopen(Request(url, b'{"model":"unchanged"}',
                           {'X-Goog-Api-Key': 'mock-only', 'Content-Type': 'application/json'}))


def test_route_auth_payload_and_error_passthrough(upstream):
    with GeminiStream(upstream.url + '/gateway') as bridge:
        with post(bridge.url + '/v1beta/models/carbon:streamGenerateContent?alt=sse') as response:
            assert response.read() == b'data: {}\n\n'
        path, body, headers = upstream.calls[-1]
        assert path == '/gateway/v1beta/models/carbon:streamGenerateContent?alt=sse'
        assert body == b'{"model":"unchanged"}'
        assert headers['X-Goog-Api-Key'] == 'mock-only'
        assert headers['Accept-Encoding'] == 'identity'
        upstream.reply = lambda p, b: (503, {'Content-Type': 'application/json', 'Retry-After': '4'}, [b'{"error":"unavailable"}'])
        with pytest.raises(HTTPError) as error:
            post(bridge.url + '/v1beta/models/carbon:streamGenerateContent?alt=sse')
        assert error.value.code == 503
        assert error.value.headers['Retry-After'] == '4'
        assert error.value.read() == b'{"error":"unavailable"}'
        upstream.reply = lambda p, b: (200, {'Content-Type': 'application/json'}, [b'{"text":": PING"}'])
        with post(bridge.url + '/v1beta/models/carbon:generateContent') as response:
            assert response.read() == b'{"text":": PING"}'


def test_wrong_token_and_redirect_do_not_forward_credentials(upstream):
    with GeminiStream(upstream.url) as bridge:
        with pytest.raises(HTTPError) as error:
            post(bridge.url.replace(bridge.prefix, '/incorrect') + '/v1beta/models/carbon:streamGenerateContent')
        assert error.value.code == 404
        assert upstream.calls == []
        upstream.reply = lambda p, b: (307, {'Location': upstream.url + '/elsewhere'}, [b''])
        with pytest.raises(HTTPError) as error:
            post(bridge.url + '/v1beta/models/carbon:streamGenerateContent')
        assert error.value.code == 307
        assert error.value.headers.get('Location') is None
        assert len(upstream.calls) == 1


def test_disabled_and_other_providers_unchanged():
    for kind, config in [('gemini', {}), ('codex', {'gemini_sse_compat': True}), ('claude', {'gemini_sse_compat': True})]:
        with native_environment(kind, config, {}) as env:
            assert env is None


def test_live_wrapper_uses_opt_in_without_changing_parent_route(tmp_path, upstream, monkeypatch):
    from live_cli import LiveCLI
    work = tmp_path / 'gemini_1'
    work.mkdir()
    (tmp_path / 'run.json').write_text(json.dumps({
        'workers': [{'name': 'gemini_1', 'kind': 'gemini'}], 'gemini_sse_compat': True}))
    script = tmp_path / 'fake_native.py'
    capture = tmp_path / 'captured.json'
    script.write_text('''import json, os, sys
from urllib.request import Request, urlopen
sys.stdin.readline()
base = os.environ['GOOGLE_GEMINI_BASE_URL']
with urlopen(Request(base + '/v1beta/models/carbon:streamGenerateContent?alt=sse', b'{}')) as r:
    data = r.read().decode()
with open(sys.argv[1], 'w') as f:
    json.dump({'base': base, 'data': data, 'key': os.environ['GEMINI_API_KEY']}, f)
''')
    monkeypatch.setenv('GOOGLE_GEMINI_BASE_URL', upstream.url)
    monkeypatch.setenv('GEMINI_API_KEY', 'mock-only')
    args = SimpleNamespace(workspace=str(work), kind='gemini', model='gemini-4-carbon',
                           prompt='Continue.', conversation='preserved-native-sid',
                           command=[sys.executable, str(script), str(capture), '-p', 'Continue.'])
    assert LiveCLI(args).run() == 1  # Deliberate fake-native exit after one input.
    observed = json.loads(capture.read_text())
    assert observed['base'] != upstream.url
    assert observed['data'] == 'data: {}\n\n'
    assert observed['key'] == 'mock-only'
    assert os.environ['GOOGLE_GEMINI_BASE_URL'] == upstream.url


@pytest.mark.skipif(not os.environ.get('RSTATION_TEST_ANTIGRAVITY_BIN'), reason='Native CLI opt-in')
def test_native_comment_stream_and_same_conversation_resume(tmp_path, upstream):
    marker = tmp_path / 'native-tool.txt'
    def reply(path, body):
        parts = [{'text': 'SSE_OK'}]
        if len(upstream.calls) == 1:
            parts = [{'functionCall': {'name': 'run_command', 'args': {
                'CommandLine': 'printf 42 > native-tool.txt',
                'toolSummary': 'Local compatibility probe', 'WaitMsBeforeAsync': 1000}}}]
        data = {'candidates': [{'content': {'role': 'model', 'parts': parts}, 'finishReason': 'STOP'}],
                'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 2}}
        wire = b': PING\r\n\r\ndata: ' + json.dumps(data).encode() + b'\r\n\r\n'
        return 200, {'Content-Type': 'text/event-stream'}, [wire[:3], wire[3:]]
    upstream.reply = reply
    prepare_profile(tmp_path, True, 'gemini-4-carbon')
    env = dict(os.environ, HOME=str(tmp_path / '.home'), XDG_CONFIG_HOME=str(tmp_path / '.home/.config'),
               GEMINI_API_KEY='mock-only', GOOGLE_GEMINI_BASE_URL=upstream.url, NO_PROXY='*')
    command = [os.environ['RSTATION_TEST_ANTIGRAVITY_BIN'], '--model', 'gemini-api://gemini-4-carbon',
               '--agent', 'rstation-researcher', '--output-format', 'stream-json', '--print-timeout', '0',
               '--dangerously-skip-permissions', '-p', 'Write 42 to native-tool.txt, then reply SSE_OK.']
    conversation = None
    with native_environment('gemini', {'gemini_sse_compat': True}, env) as child_env:
        for turn in range(2):
            result = subprocess.run(command + (['--conversation', conversation] if conversation else []),
                                    cwd=tmp_path, env=child_env, capture_output=True, text=True, timeout=30)
            (tmp_path / f'turn-{turn}.jsonl').write_text(result.stdout)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            terminal = next(e['result'] for e in reversed(events) if e.get('event') == 'result')
            assert result.returncode == 0, result.stderr
            assert terminal['status'] == 'SUCCESS'
            assert terminal['response'].strip() == 'SSE_OK'
            if turn:
                assert terminal['conversation_id'] == conversation
            conversation = terminal['conversation_id']
    bodies = [json.loads(body) for _, body, _ in upstream.calls]
    (tmp_path / 'mock-requests.json').write_text(json.dumps(bodies))
    assert marker.read_text() == '42'
    assert any('functionResponse' in json.dumps(body) for body in bodies)
    assert len(bodies[-1]['contents']) > len(bodies[0]['contents'])

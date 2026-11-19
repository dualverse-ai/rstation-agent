"""Opt-in tests of installed CLIs against a local, deterministic model API.

Set RSTATION_TEST_CODEX_BIN / RSTATION_TEST_CLAUDE_BIN to exercise either CLI.
These tests use dummy credentials and never contact a real model provider.
"""
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from main import parser, build_config
from recovery import ResearchManager
from subagents import check_cli_version


def user_texts(data):
    texts = []
    for item in data.get('input', data.get('messages', [])):
        if item.get('role') != 'user':
            continue
        content = item.get('content', [])
        if isinstance(content, str):
            texts.append(content)
        else:
            texts.extend(v.get('text', '') for v in content if isinstance(v, dict))
    return texts


@pytest.mark.parametrize('kind', ['codex', 'claude'])
@pytest.mark.parametrize('count', [0, 2])
def test_native_delegation_slots_and_reasoning(tmp_path, kind, count):
    binary = os.environ.get(f'RSTATION_TEST_{kind.upper()}_BIN')
    if not binary:
        pytest.skip(f'Set RSTATION_TEST_{kind.upper()}_BIN for native CLI verification')
    requests = []
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                blob = b'{"input_tokens":100}'
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            requests.append(data)
            child = any('CHILD_SLOT_' in text for text in user_texts(data))
            if child:
                release.wait(30)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()

            def event(name, **fields):
                self.wfile.write(('event: '+name+'\ndata: '+json.dumps({'type': name, **fields})+'\n\n').encode())
                self.wfile.flush()

            inputs = data.get('input', data.get('messages', []))
            if kind == 'claude':
                calls = sum(1 for m in inputs for v in m.get('content', [])
                            if isinstance(v, dict) and v.get('type') == 'tool_use' and v.get('name') == 'Agent')
                spawn = bool(count) and not child and calls < count+1
                if not child and calls >= count+1:
                    release.set()
                    time.sleep(.5)
                event('message_start', message={
                    'id': 'msg_test', 'type': 'message', 'role': 'assistant',
                    'model': data['model'], 'content': [], 'stop_reason': None, 'stop_sequence': None,
                    'usage': {'input_tokens': 10, 'output_tokens': 0}})
                if spawn:
                    event('content_block_start', index=0, content_block={
                        'type': 'tool_use', 'id': f'spawn_{calls+1}', 'name': 'Agent', 'input': {}})
                    event('content_block_delta', index=0, delta={
                        'type': 'input_json_delta', 'partial_json': json.dumps({
                            'description': 'Test assistant slot', 'prompt': f'CHILD_SLOT_{calls+1}: Reply Hello only.',
                            'run_in_background': True})})
                else:
                    event('content_block_start', index=0, content_block={'type': 'text', 'text': ''})
                    event('content_block_delta', index=0, delta={'type': 'text_delta', 'text': 'Hello'})
                event('content_block_stop', index=0)
                event('message_delta', delta={'stop_reason': 'tool_use' if spawn else 'end_turn',
                                              'stop_sequence': None}, usage={'output_tokens': 1})
                event('message_stop')
            else:
                item = {'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': 'Hello', 'annotations': []}]}
                calls = sum(1 for i in inputs if i.get('type') == 'function_call' and i.get('name') == 'spawn_agent')
                if count and not child:
                    if not any(i.get('type') == 'tool_search_output' for i in inputs):
                        item = {'id': 'ts_test', 'type': 'tool_search_call', 'call_id': 'search_1',
                                'status': 'completed', 'execution': 'client',
                                'arguments': {'query': 'spawn_agent research_assistant', 'limit': 2}}
                    elif calls < count+1:
                        item = {'id': f'fc_{calls+1}', 'type': 'function_call', 'call_id': f'spawn_{calls+1}',
                                'name': 'spawn_agent', 'namespace': 'multi_agent_v1', 'arguments': json.dumps({
                                    'agent_type': 'research_assistant', 'fork_context': False, 'message': f'CHILD_SLOT_{calls+1}: Reply Hello only.'})}
                    else:
                        release.set()
                        time.sleep(.5)
                event('response.created', response={'id': 'resp_test', 'status': 'in_progress', 'output': []})
                event('response.output_item.added', output_index=0, item=item)
                if item['type'] == 'message':
                    event('response.output_text.delta', item_id='msg_test', output_index=0, content_index=0, delta='Hello')
                event('response.output_item.done', output_index=0, item=item)
                event('response.completed', response={'id': 'resp_test', 'status': 'completed', 'output': [item],
                      'usage': {'input_tokens': 10, 'output_tokens': 1, 'total_tokens': 11}})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    address = f'http://127.0.0.1:{server.server_port}'
    args = parser().parse_args(['start', 'probe', '--gemini', '0', '--codex', str(int(kind == 'codex')),
        '--claude', str(int(kind == 'claude')), '--subagents', str(count), '--disable-web'])
    config = build_config(args, {'category': 'open_ended'})
    model = 'gpt-5.5' if kind == 'codex' else 'claude-opus-4-8'
    config['workers'][0]['model'] = model
    config['model_api_proxy'] = 'direct'
    # No inherited credentials, plugins or API proxy settings.
    env = {'PATH': os.environ['PATH'], 'HOME': str(tmp_path),
           'CODEX_BIN_PATH': binary, 'CLAUDE_BIN_PATH': binary,
           'RSTATION_WORKER_CODEX_API_KEY': 'mock-only', 'RSTATION_WORKER_CODEX_BASE_URL': address+'/v1',
           'RSTATION_WORKER_CLAUDE_API_KEY': 'mock-only', 'RSTATION_WORKER_CLAUDE_BASE_URL': address}
    check_cli_version(kind, binary, env)
    # Assert that removing --bare in favour of --safe-mode doesn't load workspace instructions.
    worker_path = tmp_path/(kind+'_1')
    worker_path.mkdir()
    (worker_path/'CLAUDE.md').write_text('UNTRUSTED_PROJECT_SENTINEL_SHOULD_NOT_LOAD')
    manager = ResearchManager(tmp_path, config, env, probe=True)
    try:
        manager.launch_cli_job(kind+'_1')
        deadline = time.monotonic()+50
        while manager.active_sessions and time.monotonic() < deadline:
            manager.poll_cli_jobs()
            time.sleep(.05)
        assert manager.states[kind+'_1']['status'] == 'probe_ok'
        children = [d for d in requests if any('CHILD_SLOT_' in s for s in user_texts(d))]
        assert len(children) == count
        for request in requests:
            assert request['model'] == model
            assert 'UNTRUSTED_PROJECT_SENTINEL_SHOULD_NOT_LOAD' not in json.dumps(request)
            if kind == 'codex':
                assert request['reasoning']['effort'] == 'xhigh'
            else:
                assert request['output_config']['effort'] == 'high'
                assert request['thinking']['type'] == 'adaptive'
        for request in children:
            text = json.dumps(request)
            assert 'implementation failure is not evidence' in text
            assert 'Do not use external websites' in text
            names = {t.get('name') for t in request.get('tools', [])}
            assert not names.intersection({'Agent', 'WebSearch', 'WebFetch', 'spawn_agent'})
        if count:
            rejection = 'agent thread limit reached' if kind == 'codex' else 'Concurrent subagent limit reached'
            assert rejection in json.dumps(requests)
        else:
            assert 'Multi-agent tools' not in json.dumps(requests[0].get('tools', []))
            assert 'Agent' not in {t.get('name') for t in requests[0].get('tools', [])}
    finally:
        release.set()
        manager.close()
        server.shutdown()
        server.server_close()

import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gemini_stream import native_environment, tool_result_roles


def example():
    calls = [{'functionCall': {'name': 'read_file', 'id': i, 'args': {'Path': i}},
              'thoughtSignature': 'signed'} for i in ('a', 'b')]
    replies = [{'functionResponse': {'name': 'read_file', 'id': i, 'response': response}}
               for i, response in [('a', {'error': 'missing'}), ('b', {'output': 'value'})]]
    return {'contents': [{'role': 'model', 'parts': calls}, {'role': 'model', 'parts': replies}],
            'generationConfig': {'seed': 7}, 'tools': [{'functionDeclarations': [{'name': 'read_file'}]}]}


def test_repair_preserves_native_content_and_error():
    request = example()
    expected = copy.deepcopy(request)
    expected['contents'][1]['role'] = 'user'
    repaired = tool_result_roles(json.dumps(request).encode())
    assert json.loads(repaired) == expected
    assert tool_result_roles(repaired) == repaired


@pytest.mark.parametrize('defect', ['no_prior', 'different_name', 'reversed', 'duplicate', 'mixed',
                                    'unidentified', 'missing', 'already_user', 'extra'])
def test_other_shapes_are_preserved(defect):
    data = example()
    calls, replies = [turn['parts'] for turn in data['contents']]
    if defect == 'no_prior':
        data['contents'].pop(0)
    elif defect == 'different_name':
        replies[0]['functionResponse']['name'] = 'unrelated'
    elif defect == 'reversed':
        replies.reverse()
    elif defect == 'duplicate':
        calls[1]['functionCall']['id'] = replies[1]['functionResponse']['id'] = 'a'
    elif defect == 'mixed':
        replies.append({'text': 'native text'})
    elif defect == 'unidentified':
        replies[1]['functionResponse'].pop('id')
    elif defect == 'missing':
        replies.pop()
    elif defect == 'already_user':
        data['contents'][-1]['role'] = 'user'
    else:
        replies[0]['text'] = 'native text'
    encoded = json.dumps(data).encode()
    assert tool_result_roles(encoded) == encoded


def test_only_hapi_selects_role_correction(monkeypatch):
    import gemini_stream
    selected = []
    class Bridge:
        def __init__(self, upstream, *, transform_body=None):
            self.url = 'http://127.0.0.1:42/fixture'
            selected.append((upstream, transform_body))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(gemini_stream, 'GeminiStream', Bridge)
    for host, compat, expected in [('hapiopen.cc', False, True), ('other.test', False, False),
                                   ('other.test', True, True), ('hapiopen.cc.example.test', False, False)]:
        route = 'https://' + host
        with native_environment('gemini', {'gemini_sse_compat': compat},
                                {'GOOGLE_GEMINI_BASE_URL': route}) as env:
            assert bool(env) == expected
            if env:
                assert selected[-1] == (route, tool_result_roles if host == 'hapiopen.cc' else None)

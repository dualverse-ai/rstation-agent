import json
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from council_files import Files


def test_file_navigation_pagination_search_and_no_execution(tmp_path):
    project=tmp_path/'project';project.mkdir()
    source=project/'method.py'
    source.write_text('raise RuntimeError("must not execute")\n'+''.join(f'line {i}\n' for i in range(350)))
    files=Files(tmp_path)
    assert 'method.py' in files.call('list_directory',{'path':'project'})
    assert 'must not execute' in files.call('read_file',{'path':str(source)})
    assert 'More lines:' in files.call('read_file',{'path':'project/method.py'})
    assert '151: line 149' in files.call('read_file',{'path':str(source),'start_line':151})
    assert 'project/method.py:1:' in files.call('search_text',{'path':'project','query':'must not execute'})
    with pytest.raises(ValueError):files.call('execute',{'path':'project/method.py'})
    with pytest.raises(ValueError):files.call('read_file',{'path':'../outside'})
    (project/'outside').symlink_to(tmp_path.parent)
    with pytest.raises(ValueError):files.call('list_directory',{'path':'project/outside'})


def test_stdio_read_and_error_are_protocol_responses(tmp_path):
    (tmp_path/'note.md').write_text('FILE_ONLY_NONCE')
    requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05'}},
              {'jsonrpc':'2.0','method':'notifications/initialized'},
              {'jsonrpc':'2.0','id':2,'method':'tools/list'},
              {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'read_file','arguments':{'path':'note.md'}}},
              {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'read_file','arguments':{'path':'missing'}}}]
    proc=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'council_files.py'),str(tmp_path)],
                        input='\n'.join(map(json.dumps,requests))+'\n',text=True,capture_output=True,check=True)
    replies=[json.loads(line) for line in proc.stdout.splitlines()]
    assert len(replies)==4
    assert all(t['annotations']['readOnlyHint'] for t in replies[1]['result']['tools'])
    assert 'FILE_ONLY_NONCE' in replies[2]['result']['content'][0]['text']
    assert replies[3]['result']['isError']


def test_peer_roots_are_readable_but_private_and_other_groups_are_not(tmp_path):
    own=tmp_path/'group/claude_1';peer=own.parent/'codex_1'
    own.mkdir(parents=True);peer.mkdir()
    (peer/'PLAN.md').write_text('Shared scientific hypothesis')
    (peer/'.home').mkdir();(peer/'.home/key').write_text('PRIVATE')
    (peer/'credentials.json').write_text('PRIVATE')
    outside=tmp_path/'another_group';outside.mkdir();(outside/'PLAN.md').write_text('OUTSIDE')
    (peer/'escape').symlink_to(outside, target_is_directory=True)
    (peer/'private-alias').symlink_to(peer/'.home/key')
    files=Files(own,peer)
    assert 'Shared scientific hypothesis' in files.call('read_file',{'path':'../codex_1/PLAN.md'})
    assert 'PLAN.md' in files.call('list_directory',{'path':str(peer)})
    assert '../codex_1/PLAN.md:1:' in files.call('search_text',{'path':str(peer),'query':'hypothesis'})
    listing=files.call('list_directory',{'path':str(peer)})
    assert '.home' not in listing and 'escape' not in listing and 'credentials' not in listing and 'private-alias' not in listing
    for target in [peer/'.home/key',peer/'credentials.json',outside/'PLAN.md',peer/'escape/PLAN.md',peer/'private-alias',own.parent/'run.json',own.parent/'.council/store.json']:
        with pytest.raises(ValueError):files.call('read_file',{'path':str(target)})
    assert 'PRIVATE' not in files.call('search_text',{'path':str(peer),'query':'PRIVATE'})

"""Native Claude notices must not stop a multi-worker controller."""
import json
from types import SimpleNamespace
import sys

import pytest

from test_workers import cfg, env, drain
from recovery import ResearchManager, ClaudeResume


def make_manager(tmp_path):
    return ResearchManager(tmp_path, cfg(tmp_path, 'claude'), env(tmp_path, 'claude'))


def test_permission_notice_does_not_stop_other_worker_or_lose_session(tmp_path):
    config=cfg(tmp_path, 'claude')
    config['workers'] += cfg(tmp_path, 'codex')['workers']
    environment=env(tmp_path, 'claude')
    script=tmp_path/'claude-notices'
    # This is the shape emitted by the live CLI, without its private payload.
    events=[
        {'type':'system','subtype':'init','session_id':'retained-claude'},
        {'type':'assistant','message':{'content':[{'type':'tool_use','id':'denied-tool'}]}},
        {'type':'system','subtype':'permission_denied','tool_name':'Agent',
         'tool_use_id':'denied-tool','message':'Tool is not permitted.', 'session_id':'retained-claude'},
        {'type':'assistant','message':{'content':[{'type':'text','text':'Continuing independently.'}]}},
        {'type':'result','session_id':'retained-claude','is_error':False},
    ]
    script.write_text('#!'+sys.executable+'\nimport json\nfor event in '+repr(events)+': print(json.dumps(event),flush=True)\n')
    script.chmod(0o755)
    environment['CLAUDE_BIN_PATH']=str(script)
    # The ordinary mock Codex succeeds on its second launch.
    (tmp_path/'codex_1').mkdir()
    (tmp_path/'codex_1/counter').write_text('1')
    m=ResearchManager(tmp_path,config,environment)
    try:
        for name in ['claude_1','codex_1']:m.launch_cli_job(name)
        drain(m)
        assert m.states['claude_1']['token']=='retained-claude'
        assert m.states['codex_1']['token']=='stable-session'
        assert all(m.states[n]['status']=='continuing' for n in ['claude_1','codex_1'])
        assert not m.pending_tools['claude_1']
        assert m.states['claude_1']['last_response_excerpt']=='Continuing independently.'
    finally:m.close()


@pytest.mark.parametrize('message',[None,'plain message',[],{'content':None},
                                  {'content':'plain chat text'},
                                  {'content':['text',None,7,{'type':'text','text':None}]}])
def test_poll_accepts_non_block_messages_and_continues_reading(tmp_path,message):
    m=make_manager(tmp_path)
    path=tmp_path/'events.jsonl'
    session=SimpleNamespace(transcript_path=str(path),backend='claude')
    m.active_sessions['claude_1']=session
    events=[None,[],17,{'type':'system','message':message},
            {'type':'assistant','message':message},{'type':'user','message':message},
            {'type':'assistant','message':{'content':[{'type':'tool_use','id':'real-tool'}]}},
            {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'real-tool'}]}},
            {'type':'assistant','session_id':'saved-session',
             'message':{'content':[{'type':'text','text':'Research continues.'}]}}]
    path.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
    try:
        m._before_cli_job_poll()
        assert not m.pending_tools['claude_1']
        state=m.states['claude_1']
        assert state['token']=='saved-session'
        assert state['last_response_excerpt']=='Research continues.'
        assert state['last_tool_completed']
        last=state['last_response']
        m._before_cli_job_poll()
        assert state['last_response']==last  # Already consumed, no false progress.
        assert ClaudeResume().extract_resume_token(str(path))=='saved-session'
    finally:
        m.active_sessions.clear();m.close()


def test_notice_does_not_count_as_model_progress_or_clear_unrelated_tools(tmp_path):
    m=make_manager(tmp_path)
    path=tmp_path/'events.jsonl'
    m.active_sessions['claude_1']=SimpleNamespace(transcript_path=str(path),backend='claude')
    m.pending_tools['claude_1']={'denied','still-running'}
    m.states['claude_1'].update(last_response=10,last_tool_completed=11)
    path.write_text(json.dumps({'type':'system','subtype':'permission_denied',
                               'message':'Not permitted.','tool_use_id':'denied'})+'\n')
    try:
        m._before_cli_job_poll()
        assert m.pending_tools['claude_1']=={'still-running'}
        assert m.states['claude_1']['last_response']==10
        assert m.states['claude_1']['last_tool_completed']==11
    finally:
        m.active_sessions.clear();m.close()

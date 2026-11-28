import json
import uuid
from types import SimpleNamespace

import pytest
import claude_compaction as compact
from test_workers import ResearchManager, cfg, env


@pytest.mark.parametrize('mode', ['resume','fresh_attempt'])
def test_schedule_once_and_block_second_overflow(tmp_path,mode):
    manager=ResearchManager(tmp_path,cfg(tmp_path,'claude'),env(tmp_path,'claude'))
    sid=str(uuid.uuid4())
    state=manager.states['claude_1']
    state.update(token=sid,spawn=1,last_response=42)
    p=tmp_path/'events'
    p.write_text(json.dumps(dict(type='result',is_error=True,session_id=sid,result='API Error: 400 上下文超长，请新开对话。')))
    session=SimpleNamespace(backend='claude',transcript_path=str(p),stderr_path=str(tmp_path/'none'),transcript_idle_timeout_reason=None)
    try:
        failure=manager._classify_cli_job_failure('claude_1',session)
        assert failure.category=='claude_context_overflow'
        schedule=getattr(manager,'_schedule_cli_job_'+mode)
        schedule('claude_1',session,failure)
        assert state['claude_compaction']['pending'] and state['token']==sid
        schedule('claude_1',session,failure)
        assert state['blocked_reason']=='claude_compaction_failed' and state['token']==sid
    finally:
        manager.close()


@pytest.mark.parametrize('success', [True,False])
def test_backup_and_atomic_checkpoint(tmp_path,monkeypatch,success):
    sid=str(uuid.uuid4())
    source=tmp_path/'.claude/projects/work'/f'{sid}.jsonl'
    source.parent.mkdir(parents=True)
    def row(**kw):
        return json.dumps(dict(sessionId=sid,**kw))+'\n'
    original=row(type='assistant',message={'content':[{'type':'text','text':'done'}],'stop_reason':'end_turn','usage':{'output_tokens':5}})+row(type='assistant',isApiErrorMessage=True)
    source.write_text(original)
    calls=[]
    def cli(args,env,**kw):
        calls.append(args)
        from pathlib import Path
        trial=Path(env['CLAUDE_CONFIG_DIR'])/'projects/work'/source.name
        with trial.open('a') as f:
            f.write(row(type='system',subtype='compact_boundary',uuid='boundary'))
            if success:f.write(row(type='user',isCompactSummary=True,message={'content':'A summary.'}))
        return SimpleNamespace(returncode=0,stdout=json.dumps(dict(type="result",session_id=sid,is_error=False)).encode())
    monkeypatch.setattr(compact.subprocess,'run',cli)
    environment={'CLAUDE_CONFIG_DIR':str(tmp_path/'.claude')}
    for _ in range(2):
        assert compact.compact(tmp_path,sid,'claude','opus',environment) is success
    assert len(calls)==1
    backups=list((tmp_path/'.runtime/claude-compaction'/sid).glob('*.jsonl'))
    assert len(backups)==1 and backups[0].read_text()==original
    assert ('A summary.' in source.read_text()) is success
    if not success:assert source.read_text()==original


@pytest.mark.parametrize('text,stop,expected', [('API Error: 400 prompt is too long','stop_sequence',True),
    ('API Error: 503 Bad gateway','stop_sequence',False),('API Error: 400 上下文超长','refusal',False)])
def test_typed_context_errors(text,stop,expected):
    assert compact.overflow(dict(type='result',is_error=True,result=text,stop_reason=stop)) is expected

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import owner_history as history
from common import read, save
from messages import Messages
from live_cli import LiveCLI
from main import main


def text(work):
    return (work / 'user_instruction.txt').read_text()


def owner(q, value='Original owner instruction\n第二行\n', reference='desk:6:13328'):
    return q.add(value, owner_reference=reference)


def test_receipt_only_full_text_and_exclusion(tmp_path):
    q = Messages(tmp_path)
    number = owner(q)
    assert 'Original owner instruction' not in text(tmp_path)
    q.mark([number], 'sent')
    assert 'Original owner instruction' not in text(tmp_path)
    for kwargs in ({}, {'source':'Human operator'}, {'priority':'owner'}, {'source':'Research coder'}, {'delivery_key':'supervision:1'}):
        priority = kwargs.pop('priority', None)
        n = q.add('DO_NOT_INCLUDE', **kwargs)
        if priority:
            q._update(lambda data: data['messages'][-1].update(priority=priority))
        q.mark([n], 'received')
    q.mark([number], 'received')
    assert q.list()[0]['text'] == 'Original owner instruction\n第二行\n'
    assert 'Original owner instruction\n第二行\n' in text(tmp_path)
    assert 'DO_NOT_INCLUDE' not in text(tmp_path)
    assert ' UTC / ' in text(tmp_path) and ' HKT' in text(tmp_path)
    assert 'desk:6:13328' in text(tmp_path)
    original = text(tmp_path)
    first = q.list()[0]['first_received_at']
    q.recover(); q.mark([number], 'received'); q.mark([number], 'delivered')
    assert text(tmp_path) == original
    assert q.list()[0]['first_received_at'] == first
    assert number not in {m["id"] for m in q.pending()}


def test_owner_delivery_key_collision(tmp_path):
    q = Messages(tmp_path)
    a = q.add(' exact\n', delivery_key='approved:1', owner_reference='desk:6:1')
    assert q.add(' exact\n', delivery_key='approved:1', owner_reference='desk:6:1') == a
    with pytest.raises(ValueError):
        q.add('different', delivery_key='approved:1', owner_reference='desk:6:1')
    with pytest.raises(ValueError):
        q.add(' exact\n', delivery_key='approved:1', owner_reference='desk:6:2')


def test_timestamp_conversion_and_historical_backfill_no_replay(tmp_path):
    q = Messages(tmp_path)
    number = q.add('Past owner message')
    q.mark([number], 'received'); q.mark([number], 'delivered')
    data = read(q.path); data['messages'][0]['first_received_at'] = '2026-11-27T18:00:00+00:00'; save(q.path, data)
    manifest = [{'message_id': number, 'reference': 'desk:6:2', 'text_sha256': history.sha(b'Past owner message')}]
    history.sync(tmp_path, backfill=manifest)
    before = q.list()
    assert '2026-11-28 02:00:00.000000 HKT' in text(tmp_path)
    assert '(historical backfill)' in text(tmp_path)
    for _ in range(3): history.sync(tmp_path, backfill=manifest)
    assert q.list() == before and not q.pending()
    assert text(tmp_path).count('Past owner message') == 1


@pytest.mark.parametrize('problem', ['hash','missing','duplicate','pending','reference'])
def test_invalid_backfill_is_atomic(tmp_path, problem):
    q=Messages(tmp_path); a=q.add('one'); b=q.add('two');q.mark([a,b],'received')
    manifest=[{'message_id':a,'reference':'desk:6:1','text_sha256':history.sha(b'one')},
              {'message_id':b,'reference':'desk:6:2','text_sha256':history.sha(b'two')}]
    if problem=='hash': manifest[1]['text_sha256']='wrong'
    if problem=='missing':manifest[1]['message_id']=999
    if problem=='duplicate':manifest[1]=manifest[0].copy()
    if problem=='pending':
        data=read(q.path);data['messages'][1].pop('first_received_at');data['messages'][1].pop('received_at');save(q.path,data)
    if problem=='reference':manifest[1]['reference']='bad\nreference'
    before=q.path.read_bytes()
    with pytest.raises(ValueError):history.sync(tmp_path,backfill=manifest)
    assert q.path.read_bytes()==before


@pytest.mark.parametrize('failure', ['before_ledger', 'before_text', 'after_text'])
def test_crash_repairs_without_losing_receipt(tmp_path,monkeypatch,failure):
    q=Messages(tmp_path); number=owner(q)
    original_save=history.save;original_atomic=history.atomic_text
    def fail_save(path,data,**kw):
        if Path(path).name=='owner_history.json' and failure=='before_ledger':raise OSError('DO_NOT_LOG_SECRET')
        original_save(path,data,**kw)
    def fail_atomic(path,content,uid):
        if Path(path).name=='user_instruction.txt':
            if failure=='before_text':raise OSError('DO_NOT_LOG_SECRET')
            original_atomic(path,content,uid)
            if failure=='after_text':raise OSError('DO_NOT_LOG_SECRET')
        else:original_atomic(path,content,uid)
    with monkeypatch.context() as m:
        m.setattr(history,'save',fail_save);m.setattr(history,'atomic_text',fail_atomic)
        q.mark([number],'received')
    assert q.list()[0]['first_received_at']
    status=read(tmp_path/'.runtime/owner_history_status.json')
    assert status['error']=='history_sync_failed' and 'DO_NOT_LOG_SECRET' not in json.dumps(status)
    history.sync(tmp_path);history.sync(tmp_path)
    assert text(tmp_path).count('Original owner instruction')==1
    assert len(read(tmp_path/'.runtime/owner_history.json')['entries'])==1


def append_process(work,offset):
    q=Messages(work)
    for i in range(8):
        n=owner(q,f'UNIQUE_{offset+i:03}',f'desk:6:{offset+i}')
        q.mark([n],'received')
        history.sync(work)
        q.mark([n],'delivered')


def test_real_concurrent_receipts_and_sync(tmp_path):
    ctx=multiprocessing.get_context('fork')
    children=[ctx.Process(target=append_process,args=(tmp_path,10*i)) for i in range(4)]
    for child in children:child.start()
    for child in children:child.join(20);assert child.exitcode==0
    entries=read(tmp_path/'.runtime/owner_history.json')['entries']
    assert len(entries)==32 and len({e['id'] for e in entries})==32
    for e in entries:assert text(tmp_path).count(e['text'])==1


def test_manual_edits_preserved_and_symlink_refused(tmp_path):
    q=Messages(tmp_path);n=owner(q);q.mark([n],'received')
    intended=text(tmp_path)
    (tmp_path/'user_instruction.txt').write_text('Private human edits')
    history.sync(tmp_path)
    assert text(tmp_path)==intended
    backup=list((tmp_path/'.runtime').glob('user_instruction.conflict-*.txt'))
    assert len(backup)==1 and backup[0].read_text()=='Private human edits'
    outside=tmp_path/'outside';outside.write_text('do not touch')
    (tmp_path/'user_instruction.txt').unlink();(tmp_path/'user_instruction.txt').symlink_to(outside)
    with pytest.raises(ValueError):history.sync(tmp_path)
    assert outside.read_text()=='do not touch'


@pytest.mark.parametrize('supervisor',[False,True])
def test_pointer_all_clis_preserves_notes(tmp_path,supervisor):
    from capabilities import install_skills
    work=tmp_path/'codex_1';work.mkdir()
    for n in ('AGENTS.md','CLAUDE.md','GEMINI.md'):(work/n).write_text('Existing research notes.\n')
    config={'role':'supervisor' if supervisor else 'researcher','workers':[{'name':'codex_1','kind':'codex','model':'test','gpus':[]}],
            'problem':'p','url':'https://rstation.ai','storage_gb':50,'council':False,'coder':False,
            'category':'open_ended','disable_web':False,'gpu_per_agent':0,'subagents':0}
    install_skills(tmp_path,config,config['workers'][0])
    originals={n:(work/n).read_text() for n in ('AGENTS.md','CLAUDE.md','GEMINI.md')}
    install_skills(tmp_path,config,config['workers'][0])
    for n,first in originals.items():
        value=(work/n).read_text()
        assert value==first and value.count(history.POINTER)==1
        assert value.startswith('Existing research notes.')


@pytest.mark.parametrize('kind',['codex','claude','gemini'])
def test_native_receipt_batch_hook_and_no_history_reinjection(tmp_path,kind):
    cli=LiveCLI(SimpleNamespace(workspace=str(tmp_path),kind=kind,model='test',prompt='Continue.',conversation='saved'))
    first=owner(cli.messages,'first instruction');second=owner(cli.messages,'second instruction')
    cli.sent={first:'first instruction',second:'second instruction'}
    cli.accepted((first,second))
    assert text(tmp_path).count('first instruction')==1
    assert text(tmp_path).count('second instruction')==1
    cli.finish()
    cli2=LiveCLI(SimpleNamespace(workspace=str(tmp_path),kind=kind,model='test',prompt='Continue.',conversation='saved'))
    assert cli2.messages.pending()==[]
    assert cli2.args.prompt=='Continue.'


def test_cli_explicit_mark_required_and_full_text(tmp_path,monkeypatch):
    monkeypatch.setenv('RSTATION_AGENT_DATA',str(tmp_path))
    save(tmp_path/'run/run.json',{'live_input':True,'workers':[{'name':'codex_1'}]})
    for flags in (['--owner'],['--owner-reference','desk:6:1']):
        with pytest.raises(SystemExit):main(['send','run','codex_1','message',*flags])
    main(['send','run','codex_1',' Owner\n','--owner','--owner-reference','desk:6:1'])
    row=Messages(tmp_path/'run/codex_1').list()[0]
    assert row['owner_message']=={'kind':'owner','reference':'desk:6:1'} and row['text']==' Owner\n'


def test_legacy_wrapper_and_watch_no_worker_restart(tmp_path):
    work=tmp_path/'worker';work.mkdir();q=Messages(work);n=owner(q)
    manifest=tmp_path/'manifest.json';manifest.write_text(json.dumps([str(work)]));status=tmp_path/'watch.json'
    watcher=subprocess.Popen([sys.executable,str(Path(history.__file__)),'watch',str(manifest),'--interval','1','--status',str(status)])
    sibling=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    try:
        # Simulate an old loaded wrapper writing its existing native receipt schema.
        from common import lock
        with lock(q.lock_path):
            data=read(q.path);row=data['messages'][0];row.update(status='received',first_received_at='2026-11-27T00:00:00+00:00');save(q.path,data)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            if status.exists() and read(status)['workers'][0].get('entries')==1:break
            time.sleep(.05)
        else:pytest.fail('Watcher did not observe legacy receipt')
        assert text(work).count('Original owner instruction')==1
        assert sibling.poll() is None and watcher.poll() is None
        assert read(status)['revision']==history.REVISION
        assert q.list()[0]['status']=='received'
    finally:
        watcher.terminate();sibling.terminate();watcher.wait(5);sibling.wait(5)


def test_old_wrapper_replay_keeps_first_observed_receipt(tmp_path):
    q=Messages(tmp_path);number=owner(q)
    data=read(q.path);data['messages'][0].update(status='received',received_at='2026-11-27T00:00:00+00:00');save(q.path,data)
    history.sync(tmp_path);original=text(tmp_path)
    data=read(q.path);data['messages'][0]['received_at']='2026-11-27T01:00:00+00:00';save(q.path,data)
    history.sync(tmp_path)
    assert text(tmp_path)==original
    q.mark([number], 'received')  # First upgraded-hook receipt after legacy replay.
    assert read(tmp_path/'.runtime/owner_history_status.json')['error'] is None
    assert text(tmp_path)==original


def test_history_retains_entries_if_queue_is_pruned(tmp_path):
    q=Messages(tmp_path);number=owner(q);q.mark([number],'received');original=text(tmp_path)
    save(q.path,{'next':2,'messages':[]})
    history.sync(tmp_path)
    assert text(tmp_path)==original


def test_existing_attribution_cannot_be_rewritten(tmp_path):
    q=Messages(tmp_path);number=owner(q);q.mark([number],'received');original=text(tmp_path)
    data=read(q.path);data['messages'][0]['text']='Changed silently';save(q.path,data)
    with pytest.raises(ValueError):history.sync(tmp_path)
    assert text(tmp_path)==original

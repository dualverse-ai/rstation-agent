"""Local supervision contracts, real contention and unchanged default behavior."""
import json
import multiprocessing
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import read, save, initial_prompt
from live_cli import LiveCLI
from mailbox_store import Mailbox, FALLBACK_SECONDS
from messages import Messages, prompt
import supervision
from main import parser, build_config, prepare_workspaces


def mailbox(tmp_path):
    box = Mailbox(tmp_path / 'mail.sqlite3')
    box.create([dict(actor=a, label=a, role='supervisor' if a == 'supervisor' else 'researcher',
                     workspace=str(tmp_path / a)) for a in ['supervisor', 'alice', 'bob']])
    return box


def expire(box, number, seconds=-1):
    with box.transaction() as con:
        con.execute('UPDATE messages SET deadline=? WHERE id=?', (time.time()+seconds, number))


def test_decisions_ack_revisions_and_archival(tmp_path):
    box = mailbox(tmp_path)
    proposal = box.send('alice', 'supervisor', 'A new method', kind='proposal')
    assert proposal['deadline'] - proposal['created'] == FALLBACK_SECONDS
    box.send('supervisor', 'alice', 'Received, thinking.', reply_to=proposal['id'])
    box.read('supervisor', proposal['id'], archive=True)
    assert box.inbox('supervisor') == []
    assert box.read('alice', proposal['id'])['outcome'] == 'pending'
    box.send('supervisor', 'alice', 'Specify the hypothesis.', reply_to=proposal['id'], decision='revise')
    revised = box.send('alice', 'supervisor', 'A revised method', kind='proposal')
    assert revised['id'] > proposal['id'] and revised['created'] >= proposal['created']
    box.send('supervisor', 'alice', 'Independent, novel and valuable.', reply_to=revised['id'], decision='approve')
    assert box.read('alice', revised['id'])['outcome'] == 'approve'
    assert box.read('alice', proposal['id'])['outcome'] == 'revise'
    with pytest.raises(ValueError):
        box.send('supervisor', 'alice', 'Changed my mind.', reply_to=revised['id'], decision='reject')


@pytest.mark.parametrize('kind', ['proposal', 'pivot', 'renewal'])
def test_unavailable_supervisor_automatic_decision_survives_restart(tmp_path, kind):
    box = mailbox(tmp_path)
    row = box.send('alice', 'supervisor', 'Request with replacement when relevant.', kind=kind)
    expire(box, row['id'])
    box = Mailbox(box.path)
    expected = 'autorejected' if kind == 'pivot' else 'autoaccepted'
    assert box.read('alice', row['id'])['outcome'] == expected
    with pytest.raises(ValueError, match='already decided'):
        box.send('supervisor', 'alice', 'Too late', reply_to=row['id'], decision='reject')
    assert box.read('alice', row['id'])['outcome'] == expected
    box.deliver(); box.deliver()
    assert len(Messages(tmp_path/'alice').list()) == 1
    assert len(Messages(tmp_path/'supervisor').list()) == 2
    text = Messages(tmp_path/'alice').list()[0]['text']
    assert ('automatically rejected' if kind == 'pivot' else 'automatically accepted') in text
    assert 'hours without a substantive decision' in text


def test_scope_read_permissions_and_non_destructive_reads(tmp_path):
    box = mailbox(tmp_path)
    row = box.send('alice', 'supervisor', 'Private research')
    for _ in range(2):
        assert box.inbox('supervisor')[0]['id'] == row['id']
        assert box.read('supervisor', row['id'])['text'] == 'Private research'
    with pytest.raises(ValueError): box.read('bob', row['id'])
    with pytest.raises(ValueError): box.send('alice', 'bob', 'Outside relationship')
    with pytest.raises(ValueError): box.send('unknown', 'supervisor', 'No assignment')
    with pytest.raises(ValueError): box.send('alice', 'supervisor', 'self approve', reply_to=row['id'], decision='approve')
    with pytest.raises(ValueError): box.send('supervisor', 'bob', 'wrong recipient', reply_to=row['id'], decision='approve')
    with pytest.raises(ValueError): box.read('alice', row['id'], archive=True)


def concurrent_send(path, index):
    box = Mailbox(path)
    for i in range(10): box.send('alice', 'supervisor', f'worker {index} message {i}')
    box.deliver()


def test_concurrent_writers_and_outbox_deduplication(tmp_path):
    box = mailbox(tmp_path)
    ctx = multiprocessing.get_context('fork')
    children = [ctx.Process(target=concurrent_send, args=(box.path, i)) for i in range(4)]
    for child in children: child.start()
    for child in children:
        child.join(20); assert child.exitcode == 0
    assert len(box.inbox('supervisor')) == 40
    queued = Messages(tmp_path/'supervisor').list()
    assert len(queued) == 40
    assert len({r['delivery_key'] for r in queued}) == 40
    assert all('assigned researcher' in prompt(r) for r in queued)


def compete_decision(path, number, decision, barrier, output):
    barrier.wait()
    try:
        Mailbox(path).send('supervisor', 'alice', decision, reply_to=number, decision=decision)
        output.put(decision)
    except ValueError:
        output.put('lost')


def test_concurrent_decisions_have_one_winner(tmp_path):
    box = mailbox(tmp_path)
    row = box.send('alice', 'supervisor', 'Proposal', kind='proposal')
    ctx = multiprocessing.get_context('fork'); barrier = ctx.Barrier(2); output = ctx.Queue()
    children = [ctx.Process(target=compete_decision, args=(box.path, row['id'], d, barrier, output)) for d in ['approve','reject']]
    for child in children: child.start()
    for child in children: child.join(10); assert child.exitcode == 0
    outcomes = [output.get(timeout=1) for _ in children]
    assert outcomes.count('lost') == 1
    assert box.read('alice', row['id'])['outcome'] in outcomes


def delayed_decision(path, number, started, output):
    started.set()
    try:
        Mailbox(path).send('supervisor','alice','Late approval',reply_to=number,decision='reject')
        output.put('rejected')
    except ValueError:
        output.put('already accepted')


def test_deadline_clock_refreshes_after_waiting_for_writer(tmp_path):
    box = mailbox(tmp_path)
    row = box.send('alice','supervisor','Proposal',kind='proposal')
    ctx=multiprocessing.get_context('spawn'); started=ctx.Event(); output=ctx.Queue()
    # Spawn gives the competing writer no inherited SQLite connection.
    child=ctx.Process(target=delayed_decision,args=(box.path,row['id'],started,output))
    # Hold a writer slot while the fallback deadline expires.
    with box.transaction() as con:
        con.execute('UPDATE messages SET deadline=? WHERE id=?',(time.time()+.2,row['id']))
        child.start(); assert started.wait(15); time.sleep(.4)
    child.join(10); assert child.exitcode==0
    assert output.get(timeout=1)=='already accepted'
    assert box.read('alice',row['id'])['outcome']=='autoaccepted'


def test_delivery_retries_after_partial_outbox_failure(tmp_path, monkeypatch):
    box = mailbox(tmp_path)
    box.send('supervisor', 'alice', 'Strategic guidance')
    original = Messages.add
    def crash_after_queue(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError('simulated failure after native write')
    monkeypatch.setattr(Messages, 'add', crash_after_queue)
    with pytest.raises(OSError): box.deliver()
    monkeypatch.setattr(Messages, 'add', original)
    box.deliver(); box.deliver()
    queued = Messages(tmp_path/'alice').list()
    assert len(queued) == 1 and 'not a human-operator instruction' in prompt(queued[0])


def test_saved_mail_receipt_survives_transient_delivery_database_error(tmp_path, monkeypatch, capsys):
    box = mailbox(tmp_path)
    work = tmp_path/'alice'
    supervision.install(work, box.path, 'alice')
    def unavailable(self):
        raise sqlite3.OperationalError('temporary delivery failure')
    monkeypatch.setattr(Mailbox, 'deliver', unavailable)
    proposal = work/'proposal.md'
    proposal.write_text('Research proposal')
    supervision.main(['mail', '--binding', str(supervision.binding(work)),
                      'send', 'supervisor', '--kind', 'proposal', '--file', str(proposal)])
    result = capsys.readouterr()
    receipt = json.loads(result.out)
    assert receipt['outcome'] == 'pending'
    assert box.read('alice', receipt['id'])['text'] == 'Research proposal'
    assert 'Saved durably' in result.err


def test_reflection_schedule_persistent_draw_and_completion(tmp_path):
    box = mailbox(tmp_path)
    pool = ['Question A', 'Question B']
    assert set(box.reflection('supervisor', pool)) == {'due'}
    with box.transaction() as con: box._set(con, 'reflection', {'due':time.time()-100000})
    drawn = box.reflection('supervisor', pool)
    assert Mailbox(box.path).reflection('supervisor', pool) == drawn
    answer = box.reflection('supervisor', pool, 'Evidence-based serious reflection.')
    next_state = box.reflection('supervisor', pool)
    assert next_state['due'] - answer['completed'] == 24*3600
    with pytest.raises(ValueError): box.reflection('alice', pool)
    with pytest.raises(ValueError): box.reflection('supervisor', pool, 'No outstanding draw')


def test_unsupervised_defaults_have_no_mail_or_wait_and_unchanged_prompt(tmp_path):
    args = parser().parse_args(['start','math_rlvr','--codex','1','--claude','0','--gemini','0'])
    assert args.supervisor is False
    config = build_config(args, {'category':'open_ended'})
    assert 'role' not in config and 'supervision_database' not in config
    worker = config['workers'][0]
    prepare_workspaces(tmp_path, config)
    work = tmp_path / worker['name']
    assert not (work/'mail').exists() and not supervision.binding(work).exists()
    assert 'supervis' not in initial_prompt(config, worker, Path('/research/workspace')).lower()
    # A stray wait record cannot alter a researcher without the supervisor marker.
    save(work/'.runtime/supervisor-wait.json', {'until':time.time()+1800})
    cli = LiveCLI(SimpleNamespace(workspace=str(work),kind='codex',model='test',prompt='initial',conversation=''))
    assert cli.continue_ready()
    assert prompt({'text':'existing instruction'}) == 'Message from your operator:\n\nexisting instruction'


def test_supervisor_sleep_recovery_and_message_wakeup(tmp_path):
    box = mailbox(tmp_path)
    work = tmp_path/'supervisor'
    supervision.install(work, box.path, 'supervisor', supervisor=True)
    supervision.main(['wait','--binding',str(supervision.binding(work))])
    cli = LiveCLI(SimpleNamespace(workspace=str(work),kind='codex',model='test',prompt='Continue.',conversation='old'))
    assert not cli.continue_ready()
    cli.requests[1] = ('thread/resume', None)
    cli.codex_event({'id':1,'result':{'thread':{'id':'old'}}})
    assert cli.startup_prompt == 'Continue.' and not cli.output
    box.send('alice','supervisor','New question'); box.deliver()
    assert cli.messages.pending()
    supervision.wake(work)
    assert cli.continue_ready()
    supervision.main(['wait','--binding',str(supervision.binding(work)),'--minutes','.01'])
    save(work/'.runtime/supervisor-wait.json', {'until':time.time()-1})
    assert cli.continue_ready()


def test_end_supervision_releases_requests_and_retains_history(tmp_path):
    box = mailbox(tmp_path)
    row = box.send('alice','supervisor','Proposal',kind='proposal')
    box.end()
    assert box.read('alice', row['id'])['outcome'] == 'released'
    with pytest.raises(ValueError, match='ended'): box.send('alice','supervisor','Another request')
    box.deliver()
    assert not Messages(tmp_path/'supervisor').list()


def test_create_attach_and_prompts_without_changing_researchers(tmp_path, monkeypatch):
    import main
    import common
    monkeypatch.setattr(common, 'problem_info', lambda *a: {'access_mode':'public'})
    monkeypatch.setenv('RSTATION_AGENT_DATA',str(tmp_path))
    args = parser().parse_args(['start','math_rlvr','--name','research','--codex','1','--claude','1','--gemini','1'])
    config = build_config(args,{'category':'open_ended'})
    run = tmp_path/'research'; save(run/'run.json',config)
    for worker in config['workers']:
        work = run/worker['name']; work.mkdir()
        (work/'CONTINUATION.md').write_text('Current research; keep this.')
    before = (run/'run.json').read_bytes()
    legacy = {k:v for k,v in config.items() if k != 'live_input'}
    save(run/'run.json', legacy)
    with pytest.raises(ValueError, match='native CLI messaging is not enabled'):
        supervision.create_group('legacy', ['research:codex_1'], {}, check=False)
    assert not (tmp_path/'.supervision/legacy').exists()
    save(run/'run.json', config)
    launched=[]
    monkeypatch.setattr(main,'start_controller',lambda *a:launched.append(a))
    monkeypatch.setattr(main,'prepare_workspaces',lambda run,c:(run/'supervisor').mkdir(exist_ok=True))
    result = supervision.create_group('pilot',[f'research:{w["name"]}' for w in config['workers']],{},check=False)
    assert (run/'run.json').read_bytes() == before
    supervisor = read(result/'run.json')
    assert supervisor['role'] == 'supervisor' and supervisor['subagents'] == 0
    assert supervisor['workers'][0]['gpus'] == []
    assert len(launched) == 1
    text = initial_prompt(supervisor,supervisor['workers'][0],result/'supervisor')
    assert 'DIARY.md' not in text and 'lite mode' in text and '24 hours' in text
    assert all(f'research/{w["name"]}' in text for w in config['workers'])
    assert '--kind proposal' in text and '--decision approve' in text
    with pytest.raises(ValueError,match='active supervision'):
        supervision.create_group('duplicate',['research:codex_1'],{},check=False)
    assert (run/'codex_1/CONTINUATION.md').read_text() == 'Current research; keep this.'


def test_supervisor_invitations_are_private_distinct_and_cover_all_problems(tmp_path, monkeypatch):
    import common
    calls = []
    def info(base, problem, *args):
        calls.append(problem)
        return {'access_mode': 'public' if problem == 'general' else 'invite_only'}
    monkeypatch.setattr(common, 'problem_info', info)
    configs = [dict(problem=p, url='https://rstation.ai', workers=[{'invitation_code':'researcher-code'}])
               for p in ['math_rlvr', 'math_rlvr', 'general', 'general_pretraining']]
    path = tmp_path/'invitations.json'
    with pytest.raises(ValueError, match='supervisor-invitations-file'):
        supervision.supervisor_invitations(configs)
    assert calls == ['math_rlvr', 'general', 'general_pretraining']
    codes = {'math_rlvr':'supervisor-rlvr', 'general_pretraining':'supervisor-pretrain'}
    save(path, codes)
    assert supervision.supervisor_invitations(configs, path) == codes
    for invalid in [[], {'math_rlvr':'only-one'}, {**codes, 'general':'unneeded'},
                    {**codes, 'math_rlvr':''}, {**codes, 'math_rlvr':123},
                    {**codes, 'math_rlvr':'researcher-code'}, dict.fromkeys(codes,'shared-code')]:
        save(path, invalid)
        with pytest.raises(ValueError): supervision.supervisor_invitations(configs, path)
    assert supervision.supervisor_invitations([configs[2]]) == {}


def test_invite_only_attachment_delivers_supervisor_codes_without_researcher_changes(tmp_path, monkeypatch):
    import common
    import main
    monkeypatch.setenv('RSTATION_AGENT_DATA', str(tmp_path))
    monkeypatch.setattr(common, 'problem_info', lambda *a: {'access_mode':'invite_only'})
    args = parser().parse_args(['start', 'math_rlvr', '--name','research', '--codex','1', '--claude','0','--gemini','0'])
    config = build_config(args, {'category':'open_ended'})
    config['workers'][0]['invitation_code'] = 'researcher-only'
    run = tmp_path/'research'
    save(run/'run.json', config)
    (run/'codex_1').mkdir()
    before = (run/'run.json').read_bytes()
    with pytest.raises(ValueError, match='supervisor-invitations-file'):
        supervision.create_group('missing', ['research:codex_1'], {}, check=False)
    assert not (tmp_path/'.supervision/missing').exists()
    assert not supervision.binding(run/'codex_1').exists()
    monkeypatch.setattr(main,'prepare_workspaces',lambda run,c:(run/'supervisor').mkdir(exist_ok=True))
    monkeypatch.setattr(main,'start_controller',lambda *a:None)
    path = tmp_path/'operator-invitations.json'
    save(path, {'math_rlvr':'supervisor-only'})
    result = supervision.create_group('pilot', ['research:codex_1'], {}, check=False, invitation_file=path)
    private = result/'supervisor/.rstation/invitations.json'
    assert read(private) == {'math_rlvr':'supervisor-only'}
    assert private.stat().st_mode & 0o777 == 0o600
    assert (run/'run.json').read_bytes() == before
    assert not (run/'codex_1/.rstation/invitations.json').exists()
    supervisor = read(result/'run.json')
    text = initial_prompt(supervisor, supervisor['workers'][0], result/'supervisor')
    assert str(private) in text and 'POST /api/v1/agents/me/invitations' in text
    assert 'researcher-only' not in text and 'supervisor-only' not in text
    assert 'researcher-only' not in json.dumps(supervisor) and 'supervisor-only' not in json.dumps(supervisor)


def test_missing_supervisor_invitation_stops_before_new_workers_start(tmp_path, monkeypatch):
    import main
    monkeypatch.setenv('RSTATION_AGENT_DATA', str(tmp_path))
    monkeypatch.setattr(main,'problem_info',lambda *a: {'category':'open_ended', 'access_mode':'invite_only'})
    monkeypatch.setattr(main,'preflight',lambda *a: pytest.fail('No provider call before invitation validation'))
    monkeypatch.setattr(main,'start_controller',lambda *a: pytest.fail('No worker launch before invitation validation'))
    with pytest.raises(SystemExit) as error:
        main.main(['start','math_rlvr','--name','missing','--supervisor'])
    assert error.value.code == 1
    assert not (tmp_path/'missing').exists()


@pytest.mark.parametrize('kind,expected', [('proposal', 'autoaccepted'), ('renewal', 'autoaccepted'), ('pivot', 'autorejected')])
def test_full_day_from_submission_and_acknowledgement_does_not_reset(tmp_path, monkeypatch, kind, expected):
    box = mailbox(tmp_path)
    now = time.time()
    monkeypatch.setattr('mailbox_store.time.time', lambda: now)
    row = box.send('alice', 'supervisor', 'Pivot Request and replacement Research Proposal' if kind == 'pivot' else 'Request', kind=kind)
    now += 12 * 3600
    box.send('supervisor', 'alice', 'Received, thinking.', reply_to=row['id'])
    assert box.read('alice', row['id'])['outcome'] == 'pending'
    now += 12 * 3600 - .01
    assert box.read('alice', row['id'])['outcome'] == 'pending'
    now += .01
    assert Mailbox(box.path).read('alice', row['id'])['outcome'] == expected


def test_legacy_pending_deadlines_migrate_once_from_submission(tmp_path, monkeypatch):
    box = mailbox(tmp_path)
    proposal = box.send('alice', 'supervisor', 'Proposal', kind='proposal')
    pivot = box.send('bob', 'supervisor', 'Combined pivot and replacement proposal', kind='pivot')
    decided = box.send('alice', 'supervisor', 'Older proposal', kind='proposal')
    box.send('supervisor', 'alice', 'Already approved', reply_to=decided['id'], decision='approve')
    original_due = box.setting('reflection')['due'] - 18 * 3600
    with box.transaction() as con:
        con.execute("DELETE FROM settings WHERE key='protocol_version'")
        con.execute('UPDATE messages SET deadline=created+10800 WHERE deadline IS NOT NULL')
        box._set(con, 'reflection', {'due': original_due})
    monkeypatch.setattr('mailbox_store.time.time', lambda: proposal['created'] + 4 * 3600)
    box = Mailbox(box.path)
    assert box.read('alice', proposal['id'])['outcome'] == 'pending'
    assert box.read('bob', pivot['id'])['deadline'] == pivot['created'] + 24 * 3600
    assert box.read('alice', decided['id'])['outcome'] == 'approve'
    assert box.read('alice', decided['id'])['deadline'] == decided['created'] + 3 * 3600
    assert box.setting('reflection')['due'] == original_due + 18 * 3600
    assert Mailbox(box.path).setting('reflection')['due'] == original_due + 18 * 3600
    monkeypatch.setattr('mailbox_store.time.time', lambda: pivot['created'] + 24 * 3600)
    assert box.read('bob', pivot['id'])['outcome'] == 'autorejected'


def test_supervisor_protocol_isolated_literature_in_both_templates():
    config = {'url': 'http://isolated', 'disable_web': True}
    for name in ('supervisor.md', 'supervisee-announcement.md'):
        text = supervision.protocol_text(name, config)
        assert 'external literature' not in text and 'external human literature' not in text
        assert 'this rStation instance' in text
        assert 'Do not use external websites' in text


def test_end_group_stops_only_supervisor_and_sends_exact_notice(tmp_path, monkeypatch):
    import main
    monkeypatch.setenv('RSTATION_AGENT_DATA', str(tmp_path))
    group = tmp_path/'.supervision/pilot'
    box = mailbox(group)
    supervisor = group/'supervisor'
    save(group/'group.json', {'supervisor_run': str(supervisor.parent)})
    box.send('supervisor', 'alice', 'Old supervision instruction')
    box.deliver()
    for who in ('alice', 'bob'):
        (group/who).mkdir(exist_ok=True)
        (group/who/'CONTINUATION.md').write_text('Keep this research')
    stopped = []
    monkeypatch.setattr(main, 'stop', lambda run: stopped.append(run))
    supervision.end_group('pilot', 'Exact owner retirement notice')
    supervision.end_group('pilot', 'Exact owner retirement notice')
    assert stopped == [supervisor.parent, supervisor.parent]
    assert box.setting('active') is False
    for who in ('alice', 'bob'):
        queued = Messages(group/who).list()
        notices = [m for m in queued if m['text'] == 'Exact owner retirement notice']
        assert len(notices) == 1 and notices[0]['priority'] == 'owner'
        assert not (group/who/'.runtime/PAUSE').exists()
        assert (group/who/'CONTINUATION.md').read_text() == 'Keep this research'
    assert Messages(group/'alice').list()[0]['status'] == 'cancelled'

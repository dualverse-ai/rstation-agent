import copy
import json
import multiprocessing
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from common import pid_token, read, save
from controller_guard import reclaim_idle_clis
from messages import Messages
from recovery import ResearchManager
from test_worker_control import setup
from test_workers import drain
from test_native_messages import wait_for
from worker_control import (fingerprint, process_requests, queue_fresh, queue_request,
                            status)


NAME = 'claude_1'


@pytest.fixture
def slot(tmp_path, monkeypatch):
    # Test the lifecycle with a test-only approval reader. Permission checks
    # use the real reader in test_fresh_approval.py.
    import worker_control
    approvals = tmp_path / 'test-approvals'
    monkeypatch.setattr(worker_control, 'approved', lambda key: read(approvals / key, None))
    wrapper = tmp_path / 'test-live.py'
    from common import ROOT
    wrapper.write_text(f'''import pathlib, runpy, sys
sys.path.insert(0, {str(ROOT)!r})
from common import read
import worker_control
worker_control.approved = lambda key: read(pathlib.Path({str(approvals)!r}) / key, None)
runpy.run_path({str(ROOT / 'live_cli.py')!r}, run_name='__main__')
''')
    prepare = ResearchManager._prepare_cli_job_launch

    def prepare_mock(spec, decision, backend):
        from dataclasses import replace
        prepared = prepare(spec, decision, backend)
        command = [str(wrapper) if arg == str(ROOT / 'live_cli.py') else arg for arg in prepared.command]
        return replace(prepared, command=command)

    monkeypatch.setattr(ResearchManager, '_prepare_cli_job_launch', staticmethod(prepare_mock))
    config, env, manager = setup(tmp_path, 'claude', count=2)
    config.update(live_input=True, replacement_first_instruction='OLD_BOOTSTRAP')
    save(tmp_path / 'run.json', config)
    work = tmp_path / NAME
    runtime = work / '.runtime'
    state = manager.states[NAME]
    state.update(launches=1, blocked_reason='model_refusal', status='blocked', route_index=1)
    manager.persist(NAME)
    transcript = runtime / 'launch_000001/transcript.jsonl'
    transcript.parent.mkdir()
    transcript.write_text(json.dumps({'type': 'system', 'subtype': 'init', 'session_id': state['token']}) + '\n')
    yield manager, config, env, work
    manager.close()


def submit(manager, env, *, approve=True, **kwargs):
    state = manager.states[NAME]
    fields = dict(expected_revision=state.get('worker_control', {}).get('revision', 0),
                  expected_session=state.get('token'), request_id=str(uuid.uuid4()), environment=env)
    request = queue_fresh(manager.run, NAME, **{**fields, **kwargs})
    if approve and request['status'] == 'pending':
        # Concurrent test submitters write the same reservation.
        native = str(uuid.uuid5(uuid.NAMESPACE_OID, request['id']))
        save(manager.run / 'test-approvals' / request['id'], dict(request=request, native_id=native))
    return request


def fake_native(env, script):
    Path(env['CLAUDE_BIN_PATH']).write_text('#!' + sys.executable + '\n' + script)


def finish_mock_turn(manager, *, expected_deliveries=()):
    session = manager.active_sessions[NAME]
    wait_for(manager, lambda: manager.completed(session), seconds=5)
    if expected_deliveries:
        mail = Messages(manager.run / NAME)
        expected = set(expected_deliveries)
        wait_for(manager, lambda: expected <= {
            row['id'] for row in mail.list() if row['status'] == 'delivered'
        }, seconds=5)
    session.process.terminate()
    drain(manager)


def test_explicit_fresh_native_lifecycle_with_gpu_and_mail(slot):
    m, config, env, work = slot
    folder = work / '.runtime'
    prior = m.states[NAME]
    sid = prior['token']
    from worker_control import base_settings
    from provider_control import named
    pool = m.provider_pool(NAME)
    preferred_model = 'claude-opus-5-5[1m]'
    prior['worker_control'] = dict(revision=7, session_id=sid, settings={'model': preferred_model},
        provider_route=2, base_sha256=fingerprint(base_settings(config, NAME)), pool_sha256=fingerprint(pool))
    prior['session_adoption'] = {'session_id': sid, 'failure_category': 'model_refusal'}
    prior['worker_control_hold'] = {'session_id': sid, 'failure_category': 'model_refusal'}
    prior['providers'].update(session_id=sid, revision=3)
    save(m.run / '.provider-control/providers.json', {'revision': 3, 'workers': {
        NAME: {'revision': 3, 'before_revision': 0, 'before': named(pool), 'after': named(pool)}}})
    prior['claude_compaction'] = {'session': sid, 'pending': True}
    m.persist(NAME)
    native_store = work / '.claude/projects/mock' / (sid + '.jsonl')
    native_store.parent.mkdir(parents=True)
    native_store.write_bytes(b'old native conversation\n')
    (work / 'CONTINUATION.md').write_text('OLD_CONTINUATION')
    mail = Messages(work)
    owner_received = mail.add('OLD_OWNER_RECEIVED', owner_reference='test:owner:1')
    operator_received = mail.add('OLD_OPERATOR_RECEIVED')
    operator_requeued = mail.add('OLD_REQUEUED_RECEIPT')
    mail.mark([operator_requeued], 'received')
    mail.recover()
    mail.mark([owner_received, operator_received], 'received')
    queued_owner = mail.add('NEXT_OWNER', owner_reference='test:owner:2')
    queued_operator = mail.add('NEXT_OPERATOR')
    before_mail = {row['id']: row for row in mail.list()}
    old_history = (work / 'user_instruction.txt').read_text()
    sibling = m.run / 'claude_2/.runtime/state.json'
    leftover = folder / 'launch_000003/transcript.jsonl'
    leftover.parent.mkdir()
    leftover.write_bytes(b'leftover transcript\n')
    files = {path: path.read_bytes() for path in (m.run / 'run.json', sibling, native_store,
            folder / 'launch_000001/transcript.jsonl', work / 'CONTINUATION.md', leftover)}
    gpu = Path(config['gpu_coord_file'])
    save(gpu, {'allocations': {'test': {'station_id': 'gpu-owner', 'gpus': [0]}}})
    helper, driver = [subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                       start_new_session=True) for _ in range(2)]
    job = folder / 'gpu-jobs/job.json'
    save(job, dict(owner='gpu-owner', helper_pid=helper.pid, helper_token=pid_token(helper.pid),
                   pid=driver.pid, token=pid_token(driver.pid)))
    files.update({path: path.read_bytes() for path in (job, gpu)})
    fake_native(env, '''import json, os, pathlib, sys
args = sys.argv[1:]
resume = '--resume' in args
sid = args[args.index('--resume' if resume else '--session-id') + 1]
content = json.loads(sys.stdin.readline())['message']['content']
work = pathlib.Path.cwd()
(work / ('resume.json' if resume else 'fresh.json')).write_text(json.dumps(dict(
    args=args, content=content, sid=sid, cwd=str(work), base=os.environ['ANTHROPIC_BASE_URL'],
    config=os.environ['CLAUDE_CONFIG_DIR'], compact=os.environ.get('RSTATION_CLAUDE_COMPACT'))))
print(json.dumps(dict(type='system', subtype='init', session_id=sid)), flush=True)
print(json.dumps(dict(type='user', message=dict(content=content), session_id=sid)), flush=True)
print(json.dumps(dict(type='result', subtype='success', session_id=sid)), flush=True)
sys.stdin.read()
''')
    try:
        assert not m.launch_cli_job(NAME)
        with pytest.raises(ValueError, match='existing worker block'):
            queue_request(m.run, NAME, expected_revision=7, restart=True, environment=env)
        req = submit(m, env)
        assert (folder / 'operator_messages.json').exists()
        process_requests(m)
        archive = read(folder / f"worker-fresh-{req['id']}.json")
        assert archive['state'] == prior and archive['state']['token'] == sid
        assert [row for row in mail.list()] == list(before_mail.values())
        assert m.states[NAME]['token'] is None
        assert m.launch_cli_job(NAME)
        finish_mock_turn(m, expected_deliveries=(queued_owner, queued_operator))
        actual = read(work / 'fresh.json')
        new_sid = actual['sid']
        assert sid != new_sid == m.states[NAME]['token'] == m.states[NAME]['worker_control']['session_id']
        assert m.states[NAME]['providers']['session_id'] == new_sid
        assert m.states[NAME]['launches'] == 4
        assert '--resume' not in actual['args'] and preferred_model in actual['args']
        assert actual['base'] == 'http://127.0.0.1:2' and actual['compact'] is None
        assert actual['cwd'] == str(work) and actual['config'] == str(work / '.claude')
        assert 'First read' in actual['content']
        assert 'NEXT_OWNER' in actual['content'] and 'NEXT_OPERATOR' in actual['content']
        assert 'OLD_' not in actual['content'] and 'previous session' not in actual['content']
        after_mail = {row['id']: row for row in mail.list()}
        for number in (owner_received, operator_received, operator_requeued):
            assert after_mail[number] == before_mail[number]
        assert {after_mail[n]['status'] for n in (queued_owner, queued_operator)} == {'delivered'}
        history = (work / 'user_instruction.txt').read_text()
        assert old_history in history and 'NEXT_OWNER' in history
        recovered = ResearchManager(m.run, config, env)
        try:
            recovered.states[NAME]['next'] = 0
            assert recovered.launch_cli_job(NAME)
            finish_mock_turn(recovered)
            resumed = read(work / 'resume.json')
            assert resumed['sid'] == new_sid and '--session-id' not in resumed['args']
            assert resumed['content'] == 'Continue your research.'
            stale = {'expected_session': sid, 'expected_revision': 7}
            assert submit(recovered, env, **stale, request_id=req['id'])['status'] == 'applied'
            for key, value in stale.items():
                with pytest.raises(ValueError, match='session or revision changed'):
                    submit(recovered, env, **{key: value})
        finally:
            recovered.close()
        assert helper.poll() is None and driver.poll() is None
        assert all(path.read_bytes() == before for path, before in files.items())
    finally:
        for process in (helper, driver):
            process.terminate()
            process.wait(timeout=5)


@pytest.mark.parametrize('crash_point', ['before_commit', 'after_commit'])
def test_crash_replays_request_once_and_ignores_old_transcript(slot, monkeypatch, crash_point):
    import worker_control
    m, config, env, work = slot
    req = submit(m, env)
    write = worker_control.write

    def fail(folder, filename, data):
        if filename == ('state.json' if crash_point == 'before_commit' else worker_control.fresh_receipt(req['id'])):
            raise OSError('simulated crash')
        write(folder, filename, data)

    with monkeypatch.context() as scoped:
        scoped.setattr(worker_control, 'write', fail)
        process_requests(m)
    archive = work / '.runtime' / f"worker-fresh-{req['id']}.json"
    archived = archive.read_bytes()
    recovered = ResearchManager(m.run, config, env)
    try:
        process_requests(recovered)
        assert recovered.states[NAME]['token'] is None
        assert status(m.run, NAME)['revision'] == 1
        assert status(m.run, NAME)['receipt']['status'] == 'applied'
        assert archive.read_bytes() == archived
        recovered_again = ResearchManager(m.run, config, env)
        assert recovered_again.states[NAME]['token'] is None
    finally:
        recovered.close()


@pytest.mark.parametrize('event', ['missing', 'old', 'matching', 'refusal'])
def test_recover_claimed_launch_never_creates_another_session(slot, event):
    m, config, env, work = slot
    submit(m, env)
    process_requests(m)
    s = m.states[NAME]
    decision = m._next_cli_job_launch(NAME, m._load_cli_job_state(NAME))
    launch = 'launch_000002'
    m._claim_cli_job_launch(NAME, launch, decision)
    sid = s['fresh_session']['native_id']
    if event != 'missing':
        transcript = work / '.runtime' / launch / 'transcript.jsonl'
        transcript.parent.mkdir()
        emitted = sid if event in ('matching', 'refusal') else s['fresh_session']['previous_session_id']
        transcript.write_text(json.dumps(dict(type='system', subtype='init', session_id=emitted)) + '\n')
        if event == 'refusal':
            with transcript.open('a') as stream:
                stream.write(json.dumps(dict(type='result', is_error=True, stop_reason='refusal')) + '\n')
    recovered = ResearchManager(m.run, config, env)
    if event == 'matching':
        assert recovered.states[NAME]['worker_control']['session_id'] == sid
        assert recovered._next_cli_job_launch(NAME, recovered._load_cli_job_state(NAME)).is_resume
    elif event == 'refusal':
        assert recovered.states[NAME]['token'] == sid
        assert recovered.states[NAME]['blocked_reason'] == 'model_refusal'
        assert not recovered.launch_cli_job(NAME)
    else:
        assert recovered.states[NAME]['blocked_reason'] == 'fresh_session_unconfirmed'
        assert not recovered.launch_cli_job(NAME)
    recovered.close()


@pytest.mark.parametrize('marker', ['PAUSE', 'STOP'])
def test_pause_and_stop_hold_request_and_prevent_launch(slot, marker):
    m, _, env, work = slot
    req = submit(m, env)
    path = (m.run if marker == 'STOP' else work / '.runtime') / marker
    path.touch()
    process_requests(m)
    assert status(m.run, NAME)['request']['id'] == req['id']
    assert not m.launch_cli_job(NAME)
    path.unlink()
    process_requests(m)
    assert status(m.run, NAME)['receipt']['status'] == 'applied'
    path.touch()
    assert not m.launch_cli_job(NAME)


@pytest.mark.parametrize('record', ['wrapper', 'native'])
@pytest.mark.parametrize('match', [True, False])
def test_running_or_mismatched_pid_refused_without_signals(slot, record, match):
    m, config, env, work = slot
    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    folder = work / '.runtime'
    try:
        token = pid_token(p.pid) if match else 'mismatched'
        if record == 'wrapper':
            m.states[NAME].update(pid=p.pid, pid_token=token)
            m.persist(NAME)
        else:
            save(folder / 'live_input.json', dict(pid=p.pid, token=token))
        with pytest.raises(ValueError, match='Wait for the saved wrapper'):
            submit(m, env)
        assert p.poll() is None
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_late_process_defers_and_stale_config_rejects(slot):
    m, config, env, work = slot
    req = submit(m, env)
    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
        save(work / '.runtime/live_input.json', dict(pid=p.pid, token='mismatched'))
        process_requests(m)
        assert status(m.run, NAME)['request']['id'] == req['id']
        assert not reclaim_idle_clis(m.run, config)
        assert p.poll() is None
    finally:
        p.terminate()
        p.wait(timeout=5)
    changed = copy.deepcopy(config)
    changed['workers'][0]['model'] = 'another-model'
    save(m.run / 'run.json', changed)
    process_requests(m)
    assert status(m.run, NAME)['receipt']['status'] == 'rejected'
    assert m.states[NAME]['blocked_reason'] == 'model_refusal'


def test_exited_native_before_session_id_blocks_without_retry(slot):
    m, config, env, _ = slot
    fake_native(env, "raise SystemExit(1)\n")
    submit(m, env)
    process_requests(m)
    assert m.launch_cli_job(NAME)
    drain(m)
    assert m.states[NAME]['blocked_reason'] == 'fresh_session_unconfirmed'
    recovered = ResearchManager(m.run, config, env)
    assert not recovered.launch_cli_job(NAME)
    assert recovered.states[NAME]['launches'] == 2
    recovered.close()


def test_cli_entry_requests_only_with_matching_controller(slot, monkeypatch, capsys):
    import os
    import common
    import main
    from controller_guard import code_revision

    m, _, env, _ = slot
    monkeypatch.setattr(main, 'run_dir', lambda _: m.run)
    monkeypatch.setattr(common, 'safe_environment', lambda: env)
    args = ['worker', 'run', NAME, 'fresh', '--expect-session', m.states[NAME]['token'],
            '--expect-revision', '0', '--request-id', str(uuid.uuid4())]
    with pytest.raises(SystemExit, match='running controller'):
        main.main(args)
    save(m.run / 'controller.json', dict(pid=os.getpid(), token=pid_token(os.getpid()),
                                        code_revision=code_revision()))
    main.main(args)
    assert json.loads(capsys.readouterr().out)['status'] == 'pending'


@pytest.mark.parametrize('request_type', ['adoption', 'provider'])
def test_fresh_serializes_with_pending_requests(slot, request_type):
    m, _, env, work = slot
    if request_type == 'adoption':
        save(work / '.runtime/session-adoption.json', {'pending': True})
    else:
        save(m.run / '.provider-control/providers.json', {'revision': 1, 'workers': {NAME: {'revision': 1}}})
    with pytest.raises(ValueError, match='pending'):
        submit(m, env)


def test_concurrent_submission_commits_once(slot):
    m, _, env, _ = slot
    ctx = multiprocessing.get_context('fork')
    gate = ctx.Barrier(3)
    results = ctx.Queue()
    request_id = str(uuid.uuid4())

    def submit_at_once():
        gate.wait(timeout=10)
        results.put(submit(m, env, request_id=request_id))

    operators = [ctx.Process(target=submit_at_once) for _ in range(2)]
    try:
        for operator in operators:
            operator.start()
        gate.wait(timeout=10)
        requests = [results.get(timeout=10) for _ in operators]
        assert requests[0] == requests[1]
        process_requests(m)
        process_requests(m)
        assert status(m.run, NAME)['revision'] == 1
    finally:
        for operator in operators:
            operator.join(timeout=5)
            if operator.is_alive():
                operator.terminate()
                operator.join(timeout=5)
        results.close()
    assert [operator.exitcode for operator in operators] == [0, 0]


@pytest.mark.parametrize('init', [False, True])
def test_new_refusal_blocks_across_restart_without_replaying_mail(slot, init):
    m, config, env, work = slot
    mail = Messages(work)
    number = mail.add('queued new turn', owner_reference='mock:owner')
    fake_native(env, f'''import json, sys
sid = sys.argv[sys.argv.index('--session-id') + 1]
text = json.loads(sys.stdin.readline())['message']['content']
if {init!r}:
    print(json.dumps(dict(type='system', subtype='init', session_id=sid)), flush=True)
print(json.dumps(dict(type='user', message=dict(content=text))), flush=True)
print(json.dumps(dict(type='result', is_error=True, stop_reason='refusal')), flush=True)
''')
    submit(m, env)
    process_requests(m)
    assert m.launch_cli_job(NAME)
    drain(m)
    row = mail.list()[0]
    assert row['id'] == number and row['status'] == 'received'
    assert m.states[NAME]['blocked_reason'] == 'model_refusal'
    assert bool(m.states[NAME]['token']) == init
    recovered = ResearchManager(m.run, config, env)
    try:
        assert not recovered.launch_cli_job(NAME)
        assert mail.list() == [row]
    finally:
        recovered.close()


@pytest.mark.parametrize('interrupt', ['pause', 'crash'])
def test_current_conversation_recovers_received_mail_after_interruption(slot, interrupt):
    m, config, env, work = slot
    mail = Messages(work)
    older = mail.add('previously received')
    mail.mark([older], 'received')
    pending = mail.add('current conversation only')
    fake_native(env, '''import json, sys
args = sys.argv
resumed = '--resume' in args
sid = args[args.index('--resume' if resumed else '--session-id') + 1]
message = json.loads(sys.stdin.readline())['message']
print(json.dumps(dict(type='system', subtype='init', session_id=sid)), flush=True)
print(json.dumps(dict(type='user', message=message, session_id=sid)), flush=True)
sys.stdin.read()
''')
    submit(m, env)
    process_requests(m)
    assert m.launch_cli_job(NAME)
    wait_for(m, lambda: mail.list()[1]['status'] == 'received', seconds=5)
    if interrupt == 'pause':
        from station.workers.job_manager import CliJobManager
        with pytest.MonkeyPatch.context() as scoped:
            scoped.setattr(CliJobManager, '_terminate_cli_job_process',
                           lambda *args, **kwargs: pytest.fail('Fresh launch signalled a process group'))
            m.pause_worker(NAME)
        recovered = m
    else:
        from common import alive
        native = read(work / '.runtime/live_input.json')
        session = m.active_sessions.pop(NAME)
        session.process.kill()
        session.process.wait(timeout=5)
        m._close_cli_job_session_handles(session)
        wait_for(m, lambda: not alive(native['pid'], native['token']), seconds=5)
        recovered = ResearchManager(m.run, config, env)
    try:
        assert recovered.states[NAME]['fresh_session']['phase'] == 'bound'
        assert recovered.launch_cli_job(NAME)
        path = Path(recovered.active_sessions[NAME].transcript_path)
        wait_for(recovered, lambda: 'current conversation only' in path.read_text(), seconds=5)
        assert 'previously received' not in path.read_text()
        assert mail.list()[1]['id'] == pending
    finally:
        recovered.close()

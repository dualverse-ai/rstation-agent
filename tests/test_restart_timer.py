import json
import os
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import read, save, initial_prompt
from messages import Messages
from restart_timer import Timers, timer_path, marker, gpu_start_allowed


class FakeHost:
    def __init__(self):
        self.events = []
        self.fail = None
        self.new_alive = False
    def validate(self, *args): pass
    def prepare(self, record):
        self.events.append('prepare')
        if self.fail == 'prepare': raise ValueError('read-only protection unavailable')
    def stop_predecessor(self, record):
        self.events.append('pause')
        (Path(record['workspace']) / '.runtime/PAUSE').write_text(record['id'])
    def transfer(self, record):
        self.events.append('transfer')
        if self.fail == 'transfer': raise ValueError('mailbox unavailable')
    def start(self, record):
        if not self.new_alive:
            self.events.append('start'); self.new_alive = True
    def rollback(self, record):
        self.events.append('rollback'); self.new_alive = False
        pause = Path(record['workspace']) / '.runtime/PAUSE'
        if pause.exists() and pause.read_text() == record['id']: pause.unlink()


@pytest.fixture
def setup(tmp_path):
    run = tmp_path / 'roster'; work = run / 'codex_1'
    (work / '.runtime').mkdir(parents=True)
    (run / 'claude_1/.runtime').mkdir(parents=True)
    (work / 'HANDOVER.md').write_text('A finding and an informative next experiment.')
    (work / '.runtime/conversation').write_text('retained original conversation')
    config = dict(name='roster', problem='problem', url='https://rstation.ai', category='open_ended',
                  storage_gb=10, gpu_per_agent=1, gpu_job_seconds=3600, live_input=True, gpu_pool={}, workers=[
                      dict(name='codex_1', kind='codex', model='gpt-6-astra', gpus=[0]),
                      dict(name='claude_1', kind='claude', model='claude-test', gpus=[1])])
    save(run / 'run.json', config)
    host = FakeHost(); clock = [1_700_000_000.0]
    timers = Timers(host, lambda: clock[0])
    return run, work, host, clock, timers


def arm(setup):
    run, work, host, clock, timers = setup
    result = timers.arm(run, work.name, 'Approved notice.', 'Read the handover and problem first.')
    return timer_path(run, work.name), result


def accept(work, number, stamp):
    from datetime import datetime, timezone
    queue = Messages(work)
    def update(data):
        m = next(m for m in data['messages'] if m['id'] == number)
        m.update(status='received', first_received_at=datetime.fromtimestamp(stamp, timezone.utc).isoformat())
    queue._update(update)


def test_dry_run_has_no_queue_timer_or_process_effect(setup):
    run, work, host, clock, timers = setup
    result = timers.arm(run, work.name, 'notice', 'first instruction', dry_run=True)
    assert result['armed'] is False
    assert not timer_path(run, work.name).exists()
    assert not Messages(work).path.exists()
    assert host.events == []


def test_deadline_counts_only_native_receipt_and_survives_recovery(setup):
    run, work, host, clock, timers = setup
    path, result = arm(setup)
    assert result['received_at'] is None and result['deadline'] is None
    Messages(work).mark([result['message_id']], 'sent')
    clock[0] += 10 * 3600
    timers.tick(path)
    assert read(path)['status'] == 'awaiting_receipt' and not host.events
    accepted = clock[0]
    accept(work, result['message_id'], accepted)
    timers.tick(path)
    assert read(path)['deadline'] == accepted + 8 * 3600
    Messages(work).recover()
    Messages(work).mark([result['message_id']], 'received')
    later = Timers(host, lambda: clock[0])
    clock[0] = accepted + 7 * 3600
    later.tick(path)
    assert read(path)['deadline'] == accepted + 8 * 3600
    assert not gpu_start_allowed(work, clock[0])
    clock[0] += 3600
    later.tick(path)
    assert host.events == ['prepare', 'pause', 'transfer', 'start']
    assert read(path)['status'] == 'complete'
    assert not (run / 'claude_1/.runtime/PAUSE').exists()
    assert (work / '.runtime/conversation').read_text() == 'retained original conversation'
    later.tick(path)
    assert host.events.count('start') == 1


def test_cancel_pending_and_received_releases_gate_and_is_idempotent(setup):
    _, work, host, clock, timers = setup
    path, result = arm(setup)
    accept(work, result['message_id'], clock[0])
    timers.tick(path)
    clock[0] += 7 * 3600
    assert not gpu_start_allowed(work, clock[0])
    timers.cancel(path); timers.cancel(path)
    assert gpu_start_allowed(work, clock[0])
    clock[0] += 3600
    timers.tick(path)
    assert host.events == []
    assert len([m for m in Messages(work).list() if m.get('delivery_key', '').startswith('restart-cancel:')]) == 1


def test_repair_crash_between_persisted_arm_and_queue(setup):
    _, work, _, clock, timers = setup
    path, result = arm(setup)
    queue_path = Messages(work).path
    queue_path.unlink()
    record = read(path); record.pop('message_id'); save(path, record)
    timers.tick(path)
    assert len(Messages(work).list()) == 1
    timers.tick(path)
    assert len(Messages(work).list()) == 1
    assert read(path)['message_id'] == 1


def test_repair_cancel_after_durable_write_before_marker(setup):
    _, work, _, clock, timers = setup
    path, result = arm(setup)
    accept(work, result['message_id'], clock[0]); timers.tick(path)
    clock[0] += 7 * 3600
    record = read(path); record['status'] = 'cancelled'; save(path, record)
    assert not gpu_start_allowed(work, clock[0])
    timers.tick(path)
    assert gpu_start_allowed(work, clock[0])


@pytest.mark.parametrize('stage', ['prepare', 'transfer'])
def test_failed_transition_leaves_old_running_preserves_files(setup, stage):
    _, work, host, clock, timers = setup
    path, result = arm(setup)
    accept(work, result['message_id'], clock[0]); timers.tick(path)
    host.fail = stage; clock[0] += 8 * 3600
    timers.tick(path)
    assert read(path)['status'] == 'blocked'
    assert not (work / '.runtime/PAUSE').exists()
    assert not host.new_alive
    assert (work / 'HANDOVER.md').read_text()
    assert gpu_start_allowed(work, clock[0])


def test_duplicate_arm_and_concurrent_ticks_only_start_once(setup):
    run, work, host, clock, timers = setup
    path, result = arm(setup)
    with pytest.raises(ValueError, match='active timer'):
        timers.arm(run, work.name, 'second notice', 'instruction')
    accept(work, result['message_id'], clock[0]); timers.tick(path)
    clock[0] += 8 * 3600
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: timers.tick(path), range(8)))
    assert host.events.count('start') == 1


def test_cancel_race_with_transition_is_serialized(setup):
    _, work, host, clock, timers = setup
    path, result = arm(setup)
    accept(work, result['message_id'], clock[0]); timers.tick(path)
    clock[0] += 8 * 3600
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(timers.cancel, path); b = pool.submit(timers.tick, path)
        try: a.result()
        except ValueError: pass
        b.result()
    assert read(path)['status'] in {'complete', 'cancelled'}
    assert host.new_alive == (read(path)['status'] == 'complete')


def test_final_hour_guard_does_not_depend_on_timer_service_tick(setup):
    _, work, _, clock, timers = setup
    _, result = arm(setup)
    accept(work, result['message_id'], clock[0])
    assert gpu_start_allowed(work, clock[0] + 7 * 3600 - .01)
    assert not gpu_start_allowed(work, clock[0] + 7 * 3600)


def test_owner_priority_and_first_native_receipt_are_stable(tmp_path, monkeypatch):
    import messages
    clock = ['2026-11-12T10:00:00+00:00']
    monkeypatch.setattr(messages, 'now', lambda: clock[0])
    q = Messages(tmp_path)
    ordinary = q.add('meeting'); urgent = q.add('notice', priority='owner')
    assert [m['id'] for m in q.pending()] == [urgent, ordinary]
    q.mark([urgent], 'received'); q.recover()
    clock[0] = '2026-11-12T11:00:00+00:00'; q.mark([urgent], 'received')
    message = next(m for m in q.list() if m['id'] == urgent)
    assert message['first_received_at'] == '2026-11-12T10:00:00+00:00'


@pytest.mark.parametrize('hours', [0, .5, float('nan'), float('inf')])
def test_invalid_window_never_queues(setup, hours):
    run, work, _, _, timers = setup
    with pytest.raises(ValueError): timers.arm(run, work.name, 'notice', 'instruction', hours=hours)
    assert Messages(work).list() == []


def test_gpu_pool_and_paused_targets_rejected(setup):
    run, work, _, _, timers = setup
    config = read(run / 'run.json'); config['gpu_pool'] = {work.name: {'client': 'remote'}}
    save(run / 'run.json', config)
    with pytest.raises(ValueError, match='gpu-pool'): timers.arm(run, work.name, 'notice', 'instruction')
    config['gpu_pool'] = {}; save(run / 'run.json', config)
    (work / '.runtime/PAUSE').touch()
    with pytest.raises(ValueError, match='Paused'): timers.arm(run, work.name, 'notice', 'instruction')


def test_first_instruction_precedes_research_and_supervisor_protocol(setup, monkeypatch):
    run, work, _, _, _ = setup
    config = read(run / 'run.json'); config['replacement_first_instruction'] = 'READ HANDOVER FIRST'
    assert initial_prompt(config, config['workers'][0], work).startswith('READ HANDOVER FIRST\n\nYou are')
    import supervision
    monkeypatch.setattr(supervision, 'supervisor_prompt', lambda *_: 'Supervisor protocol')
    config['role'] = 'supervisor'
    assert initial_prompt(config, config['workers'][0], work) == 'READ HANDOVER FIRST\n\nSupervisor protocol'


def test_mailbox_handover_preserves_actor_pending_proposal_and_group(setup, monkeypatch):
    import types
    import restart_transition as rt
    from mailbox_store import Mailbox
    from supervision import install, binding
    run, old, _, clock, _ = setup
    new_run = run.parent / 'fresh'; new = new_run / old.name
    new.mkdir(parents=True)
    group = run.parent / '.supervision/group'
    box = Mailbox(group / 'mail.sqlite3')
    supervisor = run.parent / 'supervisor/supervisor'; supervisor.mkdir(parents=True)
    actor = 'roster/codex_1'
    box.create([dict(actor=actor, label='researcher', role='researcher', workspace=str(old)),
                dict(actor='supervisor', label='supervisor', role='supervisor', workspace=str(supervisor))])
    install(old, box.path, actor)
    proposal = box.send(actor, 'supervisor', 'A scientific proposal.', kind='proposal')
    note = box.send('supervisor', actor, 'Follow the main scientific question.')
    box.deliver()
    save(group / 'group.json', dict(name='group', members=['roster:codex_1', 'roster:claude_1'], supervisor_run=str(supervisor.parent)))
    account = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
    monkeypatch.setattr(rt.pwd, 'getpwnam', lambda _: account)
    monkeypatch.setattr(rt.HostTransition, '_mail_access', lambda *args: None)
    record = dict(workspace=str(old), successor_run=str(new_run), worker=old.name, unix_user='fake')
    host = rt.HostTransition()
    host._move_mailbox(record, True)
    host._move_mailbox(record, True)  # Idempotent after a crash.
    assert next(p for p in box.participants() if p['actor'] == actor)['workspace'] == str(new)
    assert read(binding(new))['actor'] == actor
    assert read(group / 'group.json')['members'] == ['fresh:codex_1', 'roster:claude_1']
    with box.transaction() as con:
        rows = [dict(r) for r in con.execute('SELECT * FROM messages ORDER BY id')]
    assert rows[0]['outcome'] == 'pending' and rows[0]['deadline'] > rows[0]['created']
    assert rows[1]['native_id'] is None
    box.deliver()
    assert len(Messages(new).list()) == 1
    host._move_mailbox(record, False)
    assert next(p for p in box.participants() if p['actor'] == actor)['workspace'] == str(old)
    assert read(group / 'group.json')['members'] == ['roster:codex_1', 'roster:claude_1']


def test_waiting_gpu_helper_cannot_launch_after_final_hour(tmp_path, monkeypatch):
    import gpu
    from test_workers import cfg
    config = cfg(tmp_path); config['workers'][0]['gpus'] = [0]
    save(tmp_path / 'run.json', config)
    work = tmp_path / 'codex_1'; (work / '.runtime').mkdir(parents=True)
    save(marker(work), dict(status='armed', deadline=time.time()+4000))
    class Coordinator:
        def __init__(self, *a): pass
        def allocate(self, *a):
            save(marker(work), dict(status='armed', deadline=time.time()+3500))
            return [0]
        def deallocate(self, *a): pass
    monkeypatch.setattr(gpu, 'GPUCoordinator', Coordinator)
    def forbidden(*a, **k): pytest.fail('GPU process launched after cutoff')
    monkeypatch.setattr(gpu.subprocess, 'Popen', forbidden)
    assert gpu.run_gpu(tmp_path, 'codex_1', 1, ['fake-experiment']) == 130


def test_successor_process_launch_is_idempotent_and_rollback_stops_it(tmp_path, monkeypatch):
    import restart_transition as rt
    from common import alive
    root = tmp_path / 'toolkit'; root.mkdir()
    script = root / 'main.py'
    script.write_text('''import os,json,pathlib,sys,time,subprocess,signal
r=pathlib.Path(sys.argv[2]); w=r/'codex_1'/'.runtime';w.mkdir(parents=True,exist_ok=True)
def token(pid):return pathlib.Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]
p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],start_new_session=True)
(r/'controller.json').write_text(json.dumps({'pid':os.getpid(),'token':token(os.getpid())}))
(w/'state.json').write_text(json.dumps({'pid':p.pid,'pid_token':token(p.pid)}))
def stop(*a):p.terminate();p.wait();sys.exit(0)
signal.signal(signal.SIGTERM,stop)
while True:time.sleep(.1)
''')
    old = tmp_path / 'old/codex_1';(old / '.runtime').mkdir(parents=True)
    newrun = tmp_path / 'new';newrun.mkdir()
    record = dict(id='operation', workspace=str(old), successor_run=str(newrun), worker='codex_1', unix_user='fixture')
    (old / '.runtime/PAUSE').write_text('operation')
    monkeypatch.setattr(rt, 'ROOT', root)
    monkeypatch.setattr(rt, 'as_user', lambda _: [])
    monkeypatch.setattr(rt.HostTransition, '_environment', lambda *a: (dict(os.environ), Path(sys.executable)))
    monkeypatch.setattr(rt.HostTransition, '_move_mailbox', lambda *a: None)
    host = rt.HostTransition()
    try:
        host.start(record)
        first = read(newrun / 'controller.json')
        host.start(record)
        assert read(newrun / 'controller.json') == first
        assert alive(first['pid'], first['token'])
        host.rollback(record)
        assert not alive(first['pid'], first['token'])
        assert not (old / '.runtime/PAUSE').exists()
    finally:
        host.rollback(record)


def test_status_persists_first_observed_legacy_receipt(setup):
    _, work, _, clock, timers = setup
    path, result = arm(setup)
    accept(work, result['message_id'], clock[0])
    first = timers.status(path)
    assert first['status'] == 'armed'
    accept(work, result['message_id'], clock[0] + 300)
    assert timers.status(path)['deadline'] == first['deadline']
    assert read(path)['received_at'] == clock[0]


def test_supervisor_waits_for_exact_researcher_replacement(setup):
    run, work, host, clock, timers = setup
    dependency_path, dependency = arm(setup)
    supervisor_run = run.parent / 'supervisor'
    supervisor_work = supervisor_run / 'supervisor'
    (supervisor_work / '.runtime').mkdir(parents=True)
    config = read(run / 'run.json')
    config.update(name='supervisor', role='supervisor', workers=[dict(name='supervisor', kind='codex')])
    save(supervisor_run / 'run.json', config)
    supervisor = timers.arm(supervisor_run, 'supervisor', 'notice', 'read handover', after=[(run, work.name)])
    path = timer_path(supervisor_run, 'supervisor')
    accept(supervisor_work, supervisor['message_id'], clock[0])
    accept(work, dependency['message_id'], clock[0] + 60)
    clock[0] += 8 * 3600
    timers.tick(path)
    assert host.events == []
    assert read(path)['waiting_for'] == ['roster:codex_1']
    # Unrelated continuing researcher is never paused or messaged.
    assert not Messages(run / 'claude_1').list()
    clock[0] += 60
    timers.tick(dependency_path)
    timers.tick(path)
    assert read(path)['status'] == 'complete'
    assert not (run / 'claude_1/.runtime/PAUSE').exists()


def test_cancelled_dependency_keeps_supervisor_working(setup):
    run, work, host, clock, timers = setup
    dependency_path, dependency = arm(setup)
    supervisor_run = run.parent / 'supervisor'
    supervisor_work = supervisor_run / 'supervisor'
    (supervisor_work / '.runtime').mkdir(parents=True)
    config = read(run / 'run.json')
    config.update(name='supervisor', role='supervisor', workers=[dict(name='supervisor', kind='codex')])
    save(supervisor_run / 'run.json', config)
    supervisor = timers.arm(supervisor_run, 'supervisor', 'notice', 'read handover', after=[(run, work.name)])
    path = timer_path(supervisor_run, 'supervisor')
    accept(supervisor_work, supervisor['message_id'], clock[0])
    timers.cancel(dependency_path)
    clock[0] += 9 * 3600
    timers.tick(path)
    assert host.events == [] and read(path)['status'] == 'armed'
    timers.cancel(path)
    assert read(path)['status'] == 'cancelled'


def test_supervisor_notice_can_precede_unarmed_dependency(setup):
    run, work, host, clock, timers = setup
    supervisor_run = run.parent / 'supervisor'
    supervisor_work = supervisor_run / 'supervisor'
    (supervisor_work / '.runtime').mkdir(parents=True)
    config = read(run / 'run.json')
    config.update(name='supervisor', role='supervisor', workers=[dict(name='supervisor', kind='codex')])
    save(supervisor_run / 'run.json', config)
    result = timers.arm(supervisor_run, 'supervisor', 'notice', 'instruction', after=[(run, work.name)])
    path = timer_path(supervisor_run, 'supervisor')
    accept(supervisor_work, result['message_id'], clock[0])
    clock[0] += 8 * 3600
    timers.tick(path)
    assert not host.events
    dependency_path, dependency = arm(setup)
    accept(work, dependency['message_id'], clock[0])
    timers.tick(path)
    assert read(path)['after'][0]['id'] == dependency['id']
    clock[0] += 8 * 3600
    timers.tick(dependency_path); timers.tick(path)
    assert read(path)['status'] == 'complete'


def test_unregistered_is_explicit_and_requires_invitation(setup):
    run, work, _, _, timers = setup
    with pytest.raises(ValueError, match='prepared invitation'):
        timers.arm(run, work.name, 'notice', 'instruction', unregistered=True)
    config=read(run/'run.json');config['workers'][0]['invitation_code']='fixture-invitation'
    save(run/'run.json',config)
    result=timers.arm(run,work.name,'notice','instruction',unregistered=True)
    assert result['account_policy']=='register_new'
    assert read(timer_path(run,work.name))['selected']['invitation_code']=='fixture-invitation'


def test_unregistered_refuses_existing_account(setup):
    run,work,_,_,timers=setup
    save(work/'.rstation/credentials.json',{'handle':'existing','api_key':'fixture'})
    with pytest.raises(ValueError,match='preserve its existing account'):
        timers.arm(run,work.name,'notice','instruction',unregistered=True)


def test_rollback_failure_retries_rollback_never_launch(setup):
    run, work, host, clock, timers = setup
    path, result = arm(setup)
    accept(work, result['message_id'], clock[0]);timers.tick(path)
    host.fail = 'transfer';clock[0] += 8*3600
    original = host.rollback
    def blocked(record):raise ValueError('Successor Pool jobs remain')
    host.rollback = blocked
    timers.tick(path)
    assert read(path)['status'] == 'rolling_back'
    assert (work/'.runtime/PAUSE').exists()
    first_events = list(host.events)
    timers.tick(path)
    assert host.events == first_events
    host.rollback = original
    timers.tick(path)
    assert read(path)['status'] == 'blocked'
    assert not (work/'.runtime/PAUSE').exists()
    assert 'start' not in host.events


def test_pool_cancel_waits_for_confirmed_gate_clear(setup, monkeypatch):
    import restart_pool
    run, work, host, clock, timers = setup
    path, result = arm(setup)
    accept(work, result['message_id'], clock[0]);timers.tick(path)
    def unavailable(record):raise ValueError('Pool unavailable')
    monkeypatch.setattr(restart_pool, 'clear_cutoff', unavailable)
    result = timers.cancel(path)
    assert result['status'] == 'cancelling'
    assert read(marker(work))['status'] == 'armed'
    assert not any(m.get('delivery_key','').startswith('restart-cancel:') for m in Messages(work).list())
    monkeypatch.setattr(restart_pool, 'clear_cutoff', lambda record: None)
    timers.tick(path)
    assert read(path)['status'] == 'cancelled'
    assert read(marker(work))['status'] == 'cancelled'


def test_retry_preserves_receipt_deadline_identity_and_notice(setup):
    run, work, host, clock, timers = setup
    path, result = arm(setup)
    accept(work, result['message_id'], clock[0]);timers.tick(path)
    host.fail = 'prepare';clock[0] += 8*3600;timers.tick(path)
    old = read(path);messages = Messages(work).list()
    host.fail = None;clock[0] += 3*3600
    timers.retry(path)
    retried = read(path)
    for field in ('id','received_at','deadline','message_id','successor_run'):
        assert retried[field] == old[field]
    assert Messages(work).list() == messages
    timers.tick(path)
    assert read(path)['status'] == 'complete'
    assert host.events.count('start') == 1


def test_workspace_alias_is_retained_and_pinned(setup, tmp_path):
    from restart_transition import verify_workspace
    run, work, host, clock, timers = setup
    target = tmp_path / 'retained-workspace'; work.rename(target); work.symlink_to(target, target_is_directory=True)
    path, result = arm(setup)
    record = read(path)
    assert record['workspace'] == str(work) and work.is_symlink()
    assert record['workspace_identity']['path'] == str(target)
    verify_workspace(record)
    work.unlink(); alternative = tmp_path / 'different'; alternative.mkdir();work.symlink_to(alternative, target_is_directory=True)
    with pytest.raises(ValueError, match='changed after arming'):
        verify_workspace(record)


def test_shared_workspace_alias_is_rejected_without_notice(setup, tmp_path):
    run, work, host, clock, timers = setup
    target = tmp_path / 'retained-workspace'; work.rename(target);work.symlink_to(target, target_is_directory=True)
    other = tmp_path / 'other-roster';other.mkdir()
    (other/'claude_1').symlink_to(target,target_is_directory=True)
    save(other/'run.json',dict(workers=[dict(name='claude_1')]))
    with pytest.raises(ValueError,match='shared by another roster'):
        arm(setup)
    assert not timer_path(run,work.name).exists()
    assert not Messages(work).path.exists()


def test_immediate_supervisor_renewal_has_no_fictional_notice_receipt(setup):
    run, work, host, clock, timers = setup
    config = read(run/'run.json')
    config.update(role='supervisor', workers=[dict(name='supervisor', kind='codex', model='gpt-6-astra', gpus=[])])
    save(run/'run.json', config)
    work = run/'supervisor'
    (work/'.runtime').mkdir(parents=True)
    assert timers.renew_supervisor(run, 'Read retained notes; preserve ongoing experiments.', True)['status'] == 'dry_run_ok'
    assert not timer_path(run, 'supervisor').exists()
    result = timers.renew_supervisor(run, 'Read retained notes; preserve ongoing experiments.')
    assert result['received_at'] is None and result['message_id'] is None
    assert Messages(work).list() == []
    path = timer_path(run, 'supervisor')
    timers.tick(path)
    assert read(path)['status'] == 'complete'
    assert host.events == ['prepare', 'pause', 'transfer', 'start']
    with pytest.raises(ValueError):
        timers.renew_supervisor(run, 'Duplicate')


def test_immediate_renewal_rejects_researcher_even_named_supervisor(setup):
    run, work, host, clock, timers = setup
    config = read(run/'run.json')
    config['workers'] = [dict(name='supervisor', kind='codex', model='test', gpus=[])]
    save(run/'run.json', config)
    (run/'supervisor').mkdir()
    with pytest.raises(ValueError, match='supervisor-only'):
        timers.renew_supervisor(run, 'Do not stop research')
    assert host.events == []


def test_immediate_supervisor_failed_launch_can_retry_without_notice(setup):
    run, work, host, clock, timers = setup
    config = read(run/'run.json')
    config.update(role='supervisor', workers=[dict(name='supervisor', kind='codex', model='gpt-6-astra', gpus=[])])
    save(run/'run.json', config)
    (run/'supervisor/.runtime').mkdir(parents=True)
    timers.renew_supervisor(run, 'Read handover first.')
    path = timer_path(run, 'supervisor')
    host.fail = 'transfer'
    timers.tick(path)
    assert read(path)['status'] == 'blocked'
    host.fail = None
    timers.retry(path)
    timers.tick(path)
    assert read(path)['status'] == 'complete'

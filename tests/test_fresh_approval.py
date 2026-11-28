import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from common import ROOT, save
import fresh_approval
from test_fresh_conversation import NAME, slot, submit
from worker_control import process_requests, queue_request, status, validate_fresh_wrapper


@pytest.mark.parametrize('payload', ['{', '[]', 'null', '{}', '{"spec":[]}', '[' * 2000, ' ' * 20000])
def test_malformed_request_does_not_stop_sibling(slot, payload):
    manager, _, env, work = slot
    sibling = 'claude_2'
    (work / '.runtime/worker-control-request.json').write_text(payload)
    queue_request(manager.run, sibling, expected_revision=0, restart=True, environment=env)
    process_requests(manager)
    assert status(manager.run, NAME)['receipt']['reason'] == 'Malformed worker control request.'
    assert manager.states[NAME]['blocked_reason'] == 'model_refusal'
    assert status(manager.run, sibling)['receipt']['status'] == 'applied'


@pytest.mark.parametrize('kind', ['directory', 'fifo', 'symlink'])
def test_special_request_entry_does_not_stop_sibling(slot, kind):
    manager, _, env, work = slot
    path = work / '.runtime/worker-control-request.json'
    sentinel = manager.run / 'sentinel'
    if kind == 'directory':
        path.mkdir()
        sentinel = path / 'keep'
    sentinel.write_text('preserve')
    if kind == 'fifo':
        os.mkfifo(path)
    elif kind == 'symlink':
        path.symlink_to(sentinel)
    queue_request(manager.run, 'claude_2', expected_revision=0, restart=True, environment=env)
    process_requests(manager)
    assert sentinel.read_text() == 'preserve'
    assert os.path.lexists(path) == (kind == 'directory')
    assert status(manager.run, NAME)['receipt']['status'] == 'rejected'
    assert manager.states[NAME]['blocked_reason'] == 'model_refusal'
    assert status(manager.run, 'claude_2')['receipt']['status'] == 'applied'


@pytest.mark.parametrize('failure', ['receipt', 'unlink'])
def test_filesystem_error_and_unwritable_log_do_not_stop_sibling(slot, monkeypatch, failure):
    manager, _, env, work = slot
    folder = work / '.runtime'
    request = folder / 'worker-control-request.json'
    request.write_text('[]')
    (folder / 'console.log').mkdir()
    if failure == 'receipt':
        (folder / 'worker-control-receipt.json').mkdir()
    else:
        unlink = Path.unlink

        def fail_request_unlink(path, *args, **kwargs):
            if path == request:
                raise PermissionError('simulated unlink failure')
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'unlink', fail_request_unlink)
    queue_request(manager.run, 'claude_2', expected_revision=0, restart=True, environment=env)
    process_requests(manager)
    assert request.read_text() == '[]'
    assert manager.states[NAME]['blocked_reason'] == 'model_refusal'
    assert status(manager.run, 'claude_2')['receipt']['status'] == 'applied'


@pytest.mark.parametrize('change', ['traversal', 'nested', 'tampered'])
def test_invalid_or_tampered_proposal(slot, change):
    manager, _, env, work = slot
    request = submit(manager, env)
    if change == 'traversal':
        request['id'] = 'x/../../../outside'
    elif change == 'nested':
        request['identity']['provider_revision'] = []
    else:
        request['identity']['provider_revision'] += 1
    save(work / '.runtime/worker-control-request.json', request)
    process_requests(manager)
    receipt = status(manager.run, NAME)['receipt']
    assert receipt['status'] == 'rejected' and 'id' not in receipt
    assert manager.states[NAME]['blocked_reason'] == 'model_refusal'
    assert not (manager.run / 'outside').exists()


def test_unapproved_proposal_and_forged_ready_state(slot):
    manager, config, env, work = slot
    request = submit(manager, env, approve=False)
    process_requests(manager)
    state = manager.states[NAME]
    assert state['blocked_reason'] == 'model_refusal'
    assert 'fresh_session' not in state
    state.pop('blocked_reason')
    state['token'] = None
    state['fresh_session'] = {'id': request['id'], 'phase': 'ready'}
    manager.persist(NAME)
    assert not manager.launch_cli_job(NAME)
    with pytest.raises(RuntimeError, match='no longer authorized'):
        validate_fresh_wrapper(work, request['id'], 'claude', [], 'claude', '')
    reopened = type(manager)(manager.run, config, env)
    assert not reopened.launch_cli_job(NAME)


def test_forged_legacy_request_cannot_clear_refusal(slot):
    manager, _, env, work = slot
    state = manager.states[NAME]
    state.pop('blocked_reason')
    manager.persist(NAME)
    request = queue_request(manager.run, NAME, expected_revision=0, restart=True, environment=env)
    state['blocked_reason'] = request['expected_block'] = 'model_refusal'
    save(work / '.runtime/worker-control-request.json', request)
    process_requests(manager)
    assert state['blocked_reason'] == 'model_refusal'
    assert status(manager.run, NAME)['receipt']['status'] == 'rejected'


def test_replayed_uuid_keeps_reserved_sid_after_receipt_loss(slot):
    manager, _, env, work = slot
    request = submit(manager, env)
    process_requests(manager)
    before = manager.states[NAME]['fresh_session'].copy()
    folder = work / '.runtime'
    (folder / f"worker-fresh-{request['id']}-receipt.json").unlink()
    (folder / 'worker-control-receipt.json').unlink()
    save(folder / 'worker-control-request.json', request)
    process_requests(manager)
    assert manager.states[NAME]['fresh_session'] == before
    assert status(manager.run, NAME)['revision'] == 1


def test_approved_id_cannot_be_rebound_to_another_native_sid(slot):
    manager, _, env, _ = slot
    request = submit(manager, env)
    process_requests(manager)
    state = manager.states[NAME]
    state['fresh_session']['native_id'] = request['id']
    manager.persist(NAME)
    assert not manager.launch_cli_job(NAME)


@pytest.mark.skipif(os.geteuid() == 0, reason='Run this case as an ordinary researcher')
def test_actual_non_root_issuer_and_untrusted_ancestor(slot, monkeypatch):
    manager, _, env, work = slot
    request = submit(manager, env, approve=False)
    result = subprocess.run([sys.executable, '-I', str(ROOT / 'fresh_approval.py')],
                            input=json.dumps(request), capture_output=True, text=True)
    assert result.returncode and 'Check root privileges' in result.stderr
    store = work / 'forged-store'
    store.mkdir()
    approval = store / (request['id'] + '.json')
    save(approval, {'request': request, 'native_id': request['id']})
    approval.chmod(0o444)
    monkeypatch.setattr(fresh_approval, 'APPROVALS', store)
    assert fresh_approval.approved(request['id']) is None


@pytest.mark.skipif(os.geteuid() != 0, reason='Requires admin/root and a writable /var/lib')
def test_root_store_and_real_researcher_permissions(slot, monkeypatch):
    manager, _, env, _ = slot
    request = submit(manager, env, approve=False)
    with tempfile.TemporaryDirectory(prefix='rstation-approval-test-', dir='/var/lib') as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        store = root / 'approvals'
        store.mkdir(mode=0o755)
        monkeypatch.setattr(fresh_approval, 'APPROVALS', store)
        record = fresh_approval.issue(request)
        assert fresh_approval.issue(request) == fresh_approval.approved(request['id']) == record
        filename = store / (request['id'] + '.json')
        as_researcher = dict(user=65534, group=65534, extra_groups=[], capture_output=True)
        for target in (filename, store / 'new.json'):
            researcher = subprocess.run([sys.executable, '-I', '-c',
                'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text("forged")', str(target)],
                **as_researcher)
            assert researcher.returncode and b'PermissionError' in researcher.stderr
        visible = subprocess.run([sys.executable, '-I', '-c',
            'import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())', str(filename)], **as_researcher)
        assert visible.returncode == 0 and json.loads(visible.stdout) == record
        filename.chmod(0o666)
        assert fresh_approval.approved(request['id']) is None
        filename.chmod(0o444)
        os.chown(filename, 65534, 65534)
        assert fresh_approval.approved(request['id']) is None
        os.chown(filename, 0, 0)
        root.chmod(0o775)
        assert fresh_approval.approved(request['id']) is None
        root.chmod(0o755)
        alias = root / 'alias'
        alias.symlink_to(store)
        monkeypatch.setattr(fresh_approval, 'APPROVALS', alias)
        assert fresh_approval.approved(request['id']) is None

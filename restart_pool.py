"""Operator-only GPU Pool integration for explicitly prepared replacements."""
import hashlib
import json
import os
from pathlib import Path
import urllib.parse
import urllib.error
import urllib.request
from common import read


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def api(plan, path, payload):
    client = read(plan['owner_client'])
    url = urllib.parse.urlsplit(client['url'])
    if url.scheme != 'http' or url.hostname not in {'127.0.0.1', 'localhost', '::1'} or url.username or url.password or url.query or url.fragment:
        raise ValueError('Pool owner client must use its existing loopback connection.')
    token = Path(client['token_file']).read_text().strip()
    req = urllib.request.Request(client['url'].rstrip('/') + path,
        data=json.dumps(payload).encode(), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect()).open(req, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Do not echo request bodies, provider details or credentials.
        raise ValueError('GPU Pool operator request failed: HTTP ' + str(exc.code)) from None


def load_plan(path, run, worker):
    path = Path(path)
    st = path.stat()
    if not path.is_absolute() or path.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
        raise ValueError('Pool replacement plan must be root-owned and not writable by other users.')
    plan = read(path)
    if plan.get('run') != str(run) or plan.get('worker') != worker:
        raise ValueError('Pool plan belongs to another target.')
    if not isinstance(plan.get('id'),str) or len(plan['id']) != 12 or any(c not in '0123456789abcdef' for c in plan['id']):
        raise ValueError('Invalid prepared replacement ID.')
    if plan.get('unix_user') != 'rsr_' + plan['id']:
        raise ValueError('Pool plan must use the dedicated successor Unix account.')
    expected = str(run.parent / (run.name[:48] + '_fresh_' + plan['id']))
    if plan.get('successor_run') != expected:
        raise ValueError('Pool successor path does not match this operation.')
    for name in ('owner_client', 'successor_token_file'):
        item = Path(plan[name])
        if not item.is_absolute() or not item.is_file() or item.is_symlink():
            raise ValueError('Prepared private Pool file is missing or unsafe.')
        info = item.stat()
        if info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('Prepared Pool credentials must be private to root.')
    client = read(plan['owner_client'])
    token_file = Path(client['token_file'])
    if not token_file.is_absolute() or token_file.is_symlink() or token_file.stat().st_uid != 0 or token_file.stat().st_mode & 0o077:
        raise ValueError('Owner token must be private to root.')
    if plan['old_project'] == plan['new_project']:
        raise ValueError('Successor requires a separate Pool project.')
    profile = plan['profile']; old_profile = plan['old_profile']
    newwork = Path(expected) / worker
    if profile['native_slots']['directory'] != str(newwork / '.runtime'):
        raise ValueError('Successor quota must use its own runtime directory.')
    if profile['native_slots']['maximum'] != old_profile['native_slots']['maximum']:
        raise ValueError('Preserve the existing GPU quota.')
    if profile['home_host'] != old_profile['home_host'] or set(profile['hosts']) != set(old_profile['hosts']):
        raise ValueError('Preserve the existing Pool hosts.')
    attachment = plan['client_attachment']
    if attachment['client_config'] != str(newwork / '.runtime/gpu-pool.json'):
        raise ValueError('Pool client config must be in the successor runtime.')
    for host, entry in profile['hosts'].items():
        if entry['user'] != plan['unix_user'] or entry['cwd'] == old_profile['hosts'][host]['cwd']:
            raise ValueError('Successor must use its dedicated user and working directory.')
    if attachment['workdir'] != profile['hosts'][profile['home_host']]['cwd']:
        raise ValueError('Pool working directory differs from the prepared profile.')
    ready = api(plan, '/api/project-cutoff/check', {'project': plan['old_project']})
    if not ready.get('ready'):
        raise ValueError('GPU Pool host launch guards are not ready; leave this worker running.')
    actual = api(plan, '/api/project-profile', {'project': plan['old_project']})['profile']
    if actual != plan['old_profile']:
        raise ValueError('Pool profile changed after preparation.')
    return plan


def sync_cutoff(record):
    plan = record.get('pool_plan')
    if not plan or not record.get('deadline'):
        return
    result = api(plan, '/api/project-cutoff', dict(project=plan['old_project'],operation=record['id'],not_after=record['deadline']-3600))
    if not result.get('enforced'):
        raise ValueError('GPU Pool cutoff has not reached every execution host.')


def clear_cutoff(record):
    plan = record.get('pool_plan')
    if not plan:
        return
    current = api(plan, '/api/project-cutoff/status', {'project': plan['old_project']})
    if current.get('operation') == record['id'] and (current.get('not_after') is not None or not current.get('enforced')):
        result = api(plan, '/api/project-cutoff', dict(project=plan['old_project'],operation=record['id'],clear=True))
        if not result.get('enforced'):
            raise ValueError('Pool cutoff cancellation is still propagating.')


def stop_jobs(record):
    plan = record.get('pool_plan')
    if not plan:
        return
    current = api(plan, '/api/project-cutoff/status', {'project': plan['old_project']})
    if not current.get('enforced') or current.get('operation') != record['id']:
        raise ValueError('Old Pool project has no enforced replacement cutoff.')
    for job in current['active_jobs']:
        api(plan, '/api/cancel', {'id': job['id']})
    # Keep the selected predecessor paused while the durable hub reconciles.
    # Returning false lets the next service tick retry without rolling back.
    current = api(plan, '/api/project-cutoff/status', {'project': plan['old_project']})
    return not current['active_jobs'] and not current['permits_pending']


def activate(record):
    plan = record.get('pool_plan')
    if not plan:
        return
    token = Path(plan['successor_token_file']).read_text().strip()
    return api(plan, '/api/project-successor', dict(old_project=plan['old_project'],new_project=plan['new_project'],
        operation=record['id'],profile=plan['profile'],token_sha256=hashlib.sha256(token.encode()).hexdigest()))


def rollback_pool(record):
    """Fence successor jobs and await released quota before resuming predecessor."""
    plan = record['pool_plan']
    if record.get('pool_activation_requested'):
        # Resolve an ambiguous activation response with the same idempotent call.
        # Never infer "no project" from an HTTP timeout or other failed request.
        activate(record)
        operation = record['id'] + '_rollback'
        result = api(plan, '/api/project-cutoff', dict(project=plan['new_project'],
            operation=operation, not_after=record['deadline']))
        if not result.get('enforced'):
            raise ValueError('Successor Pool cutoff is still propagating; predecessor stays paused.')
        state = api(plan, '/api/project-cutoff/status', {'project': plan['new_project']})
        for job in state['active_jobs']:
            api(plan, '/api/cancel', {'id': job['id']})
        state = api(plan, '/api/project-cutoff/status', {'project': plan['new_project']})
        if state['active_jobs'] or state['permits_pending']:
            raise ValueError('Successor Pool jobs or quota permits remain; predecessor stays paused.')
    clear_cutoff(record)

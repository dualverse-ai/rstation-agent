"""Durable same-conversation failover across the installation's enabled Gemini routes."""
import hashlib
import json
import re
import time
from pathlib import Path

from antigravity import provider_account_error
from common import ROOT, read

HTTP_STATUS = re.compile(r'(?:\bhttp(?:/\d(?:\.\d)?)?\s+|\berror\s*:?\s*|\bstatus(?:_code)?["\s:=]+)([45]\d\d)\b', re.I)
REFUSAL = re.compile(r'\b(refus(?:al|ed)|safety|safeguard|prohibited|permission.denied|policy violation|blocklist)\b', re.I)
CONTEXT = re.compile(r'prompt (?:is )?too long|exceeds (?:the )?model.{0,90}context window|input token count.{0,60}exceeds', re.I)
TRANSPORT = re.compile(r'\b(?:timed? ?out|deadline exceeded|connection (?:refused|reset)|unexpected eof|bad gateway|upstream unavailable|provider unavailable|service unavailable|resource.exhausted)\b', re.I)


def key_id(route):
    identity = route['base_url'].rstrip('/') + '\0' + route['api_key']
    return hashlib.sha256(identity.encode()).hexdigest()


def eligible(manager, name):
    return filter_routes(manager.provider_pool(name), expand=not manager.probe)


def filter_routes(routes, *, expand=False):
    routes = list(routes)
    policy = read(ROOT / 'gemini-routing.json')
    if policy is not None:
        allowed = policy['enabled']
        by_id = {key_id(route): route for route in routes}
        if policy.get('include_installation_profile') and expand and not set(allowed) <= by_id.keys():
            from dotenv import dotenv_values
            from recovery import routes as configured
            for route in configured('gemini', dotenv_values(ROOT / '.env', interpolate=False)):
                by_id.setdefault(key_id(route), route)
        routes = [by_id[key] for key in allowed if key in by_id]
        if expand and len(routes) != len(allowed):
            raise ValueError('Enabled Gemini route missing from the operator profile.')
    if not routes:
        raise ValueError('No enabled Gemini provider in this profile.')
    return routes


def choose(state, routes):
    ids = [key_id(route) for route in routes]
    current = state.get('gemini_rotation', {}).get('next_key')
    return ids.index(current) if current in ids else 0


def begin(state, routes, index, model):
    rotation = state.setdefault('gemini_rotation', {})
    key = key_id(routes[index])
    ids = [key_id(route) for route in routes]
    if rotation.get('model') != model or rotation.get('enabled') != ids:
        rotation.update(failures=0, tried=[], exhausted=False, exhausted_since=None)
    rotation.update(version=1, enabled=ids, key=key, next_key=key, model=model,
                    started=time.time(), launch=state.get('launches'), session=state.get('token'),
                    successful_tool=False, substantive_reply=False)
    rotation.pop('scheduled_launch', None)


def observe(rotation, status, now, http_status=None):
    key, model = rotation['key'], rotation['model']
    record = dict(at=now, attempted_at=rotation['started'], status=status,
                  model=model, http_status=http_status)
    rotation.setdefault('observations', {})[key] = record
    rotation.setdefault('model_observations', {}).setdefault(model, {})[key] = record


def successful_tool(step):
    if step.get('step_type') != 'tool' or step.get('state') != 'DONE':
        return False
    info = step.get('tool_info')
    if not isinstance(info, dict) or not (step.get('tool_name') or info.get('name')):
        return False
    failed = {'ERROR', 'FAILED', 'CANCELLED', 'FAILURE'}
    for data in (step, info):
        if (data.get('error') or data.get('is_error') or data.get('isError')
                or data.get('success') is False or str(data.get('status', '')).upper() in failed):
            return False
    output = info.get('output')
    if isinstance(output, str) and output.lstrip().startswith('{'):
        try:
            parsed = json.loads(output)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            output = parsed
    if isinstance(output, dict):
        if output.get('error') or output.get('isError') or output.get('success') is False:
            return False
        if str(output.get('status', '')).upper() in failed:
            return False
    for data in (info, output if isinstance(output, dict) else {}):
        for key in ('exit_code', 'exitCode', 'returncode'):
            if data.get(key) not in (None, 0, '0'):
                return False
    if isinstance(output, str) and re.search(
            r'^\s*(?:error(?::| executing)|tool (?:call )?failed|traceback \(most recent call|'
            r'file not found|permission denied)|\b(?:exit(?:ed with)? code|return code)[:= ]+(-?[1-9]\d*)\b',
            output, re.I | re.M):
        return False
    return output is not None or 'parameters' in info


def event_evidence(state, event, now):
    rotation = state.get('gemini_rotation')
    if not rotation or rotation.get('launch') != state.get('launches'):
        return
    step = event.get('step_update', {})
    result = event.get('result', {})
    sid = event.get('conversation_id') or step.get('conversation_id') or result.get('conversation_id')
    if not rotation.get('session'):
        rotation['session'] = sid
    if not rotation['session'] or (sid and sid != rotation['session']):
        return
    if state.get('token') and state['token'] != rotation['session']:
        return
    if successful_tool(step):
        rotation['successful_tool'] = True
    if step.get('step_type') == 'agent_response' and step.get('state') == 'DONE':
        if state.get('response_step') == step.get('step_index') and state.get('response_text', '').strip():
            rotation['substantive_reply'] = True
    if result.get('status') == 'SUCCESS' and isinstance(result.get('response'), str) and result['response'].strip():
        rotation['substantive_reply'] = True
    if rotation['substantive_reply'] and rotation['successful_tool']:
        observe(rotation, 'working', now)
        rotation.update(failures=0, tried=[], exhausted=False, exhausted_since=None)
        # A separate later context incident may be compacted after actual recovery.
        rotation.pop('compact_sid', None)
        rotation['successful_tool'] = rotation['substantive_reply'] = False


def native_result(session):
    path = getattr(session, 'transcript_path', None)
    if not path:
        return {}
    try:
        with Path(path).open('rb') as stream:
            stream.seek(max(0, Path(path).stat().st_size - 256 * 1024))
            for line in reversed(stream.readlines()):
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get('type') == 'watchdog' and event.get('action') == 'resume_native':
                    return {'watchdog_interruption': True}
                if event.get('step_update', {}).get('step_type') == 'user_input':
                    return {}
                if event.get('event') == 'result' and isinstance(event.get('result'), dict):
                    return event['result']
    except OSError:
        pass
    return {}


def block(manager, name, reason):
    state = manager.states[name]
    state.update(status='blocked', blocked_reason=reason, pid=None)
    adoption = state.get('session_adoption', {})
    if adoption.get('status') == 'running':
        adoption.update(status='failed', failure_category=reason)
    manager.persist(name)
    return True


def guard(manager, name):
    """Refuse launches on a protected identity, unfinished checkpoint or live job."""
    state = manager.states[name]
    hold = state.get('worker_control_hold', {})
    control = state.get('worker_control', {})
    adoption = state.get('session_adoption', {})
    sid = state.get('token')
    changed = False
    identities = [record.get('session_id') for record in (control, hold) if record]
    if adoption.get('status') in ('pending', 'running', 'failed', 'interrupted'):
        identities.append(adoption.get('session_id'))
    if any(value != sid for value in identities):
        block(manager, name, 'worker_control_conflict')
        return False
    if (state.get('blocked_reason') == 'worker_control_failed' and sid and not state.get('session_unavailable')
            and hold.get('failure_category') in ('provider_unavailable', 'infra_transient', 'resume_limit_exceeded')):
        # Migrate only diagnosed interruptions; inconclusive legacy failures need review.
        last = native_result_from_state(manager, name)
        diagnostic = str(last.get('error') or '')
        if (not REFUSAL.search(diagnostic) and not provider_account_error(last.get('response'))
                and (hold['failure_category'] == 'provider_unavailable' or last.get('watchdog_interruption')
                     or diagnostic.strip().lower() == 'interrupted' or TRANSPORT.search(diagnostic)
                     or CONTEXT.search(diagnostic) or HTTP_STATUS.search(diagnostic))):
            for field in ('blocked_reason', 'worker_control_hold'):
                state.pop(field, None)
            control.pop('failure_category', None)
            state.update(status='retrying', resumes=0, next=0)
            changed = True
    for record in (control, adoption):
        if record.get('provider_route') is not None:
            record.pop('provider_route')
            changed = True
    if changed:
        manager.persist(name)
    if state.get('blocked_reason') or state.get('session_unavailable'):
        return False
    rotation = state.get('gemini_rotation', {})
    compact_launch = rotation.get('compact_launch')
    if compact_launch and name not in manager.active_sessions:
        receipt = read(manager.run/name/'.runtime/gemini-compaction.json', {})
        if (receipt.get('status') != 'checkpoint' or receipt.get('session') != sid
                or str(receipt.get('launch')) != str(compact_launch)):
            block(manager, name, 'gemini_compaction_failed')
            return False
        rotation.pop('compact_launch')
        manager.persist(name)
    if rotation.get('await_idle') and name not in manager.active_sessions:
        from live_watchdog import group_has_work
        from worker_control import has_jobs
        group = state.get('last_process_group')
        if has_jobs(manager.run/name) or (group and group_has_work(group, set())):
            return False
        rotation.pop('await_idle')
        manager.persist(name)
    return True


def native_result_from_state(manager, name):
    from types import SimpleNamespace
    launch = manager.states[name].get('launches', 0)
    path = manager.run/name/'.runtime'/f'launch_{launch:06d}'/'transcript.jsonl'
    return native_result(SimpleNamespace(transcript_path=path))


def schedule(manager, name, session, failure):
    if manager.probe or manager.worker(name)['kind'] != 'gemini':
        return False
    state = manager.states[name]
    result = native_result(session)
    error = str(result.get('error') or '')
    sid = state.get('token') or getattr(failure, 'resume_token', None)
    rotation = state.get('gemini_rotation', {})
    if state.get('blocked_reason'):
        return True
    if (state.get('session_unavailable') or failure.category in ('session_unavailable', 'resume_unavailable')):
        return block(manager, name, 'session_unavailable')
    if (failure.category == 'model_refusal' or REFUSAL.search(error)
            or provider_account_error(result.get('response'))):
        return block(manager, name, 'model_refusal')
    if error == 'native compaction failed':
        return block(manager, name, 'gemini_compaction_failed')
    if (rotation.get('session') and sid != rotation['session']
            or sid and getattr(failure, 'resume_token', None) not in (None, sid)
            or sid and result.get('conversation_id') not in (None, sid)):
        return block(manager, name, 'session_identity_conflict')
    if not rotation.get('key'):
        # A legacy controller can reach its budget check before its first new launch.
        if session is None and failure.category == 'resume_limit_exceeded' and sid:
            state.update(resumes=0, next=time.time()+10, status='retrying')
            manager.persist(name)
            return True
        return False
    if rotation.get('scheduled_launch') is not None and rotation['scheduled_launch'] == state.get('launches'):
        return True
    adoption = state.get('session_adoption', {})
    if adoption.get('status') == 'running':
        if adoption.get('session_id') != sid:
            return block(manager, name, 'session_identity_conflict')
        adoption['status'] = 'pending'
    now = time.time()
    match = HTTP_STATUS.search(error)
    http = int(match[1]) if match else None
    if CONTEXT.search(error):
        if not sid or rotation.get('compact_sid') == sid or not manager.config.get('live_input'):
            return block(manager, name, 'gemini_context_limit')
        rotation.update(compact_sid=sid, compact_pending=True)
        delay = 1
    else:
        credential_failed = bool(http in (400, 401) and re.search(
            r'api.key (?:not valid|invalid|expired)|invalid api.key|unauthenticated', error, re.I))
        outage = (credential_failed or failure.category == 'provider_unavailable' or http in (408, 429)
                  or http is not None and 500 <= http <= 599 or TRANSPORT.search(error))
        interrupted = (failure.category == 'infra_transient' and (
            error.strip().lower() == 'interrupted' or result.get('watchdog_interruption') or
            not error and not http and (getattr(session, 'transcript_idle_timeout_triggered', False)
                            or (getattr(failure, 'returncode', None) or 0) < 0
                            or getattr(failure, 'resume_token', None) and (not result or result.get('status') == 'ERROR'
                                and result.get('response') and not result.get('error')))))
        if not outage and not interrupted:
            return block(manager, name, 'gemini_unclassified_failure')
        ids = [key_id(route) for route in eligible(manager, name)]
        key = rotation['key']
        observe(rotation, 'errors' if outage else 'interrupted', now, http)
        failures = rotation.get('failures', 0) + 1
        tried = [k for k in rotation.get('tried', []) if k in ids]
        if failures == 1 and key in ids:
            following, delay = key, 10
        else:
            if key not in tried:
                tried.append(key)
            remaining = [k for k in ids if k not in tried]
            if remaining:
                following, delay = remaining[0], 10
            else:
                following, delay, tried, failures = ids[0], 300, [], 0
                rotation['exhausted'] = True
                rotation['exhausted_since'] = rotation.get('exhausted_since') or now
        rotation.update(next_key=following, failures=failures, tried=tried)
    rotation.update(scheduled_launch=state.get('launches'), await_idle=True)
    state.update(token=sid, pid=None, resumes=0, status='retrying', next=now+delay)
    state['retry_total'] = state.get('retry_total', 0)+1
    manager.persist(name)
    return True

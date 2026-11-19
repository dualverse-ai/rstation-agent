"""Opt-in supervisor setup and local participant tools. No public API changes."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sqlite3
import sys
import time
from datetime import datetime, timezone

from common import ROOT, NAME, read, save, lock
from mailbox_store import Mailbox, utc
from messages import Messages

PROMPTS = ROOT / 'prompts'


def supervisor_invitations(configs, invitation_file=None, *, problem_details=None):
    """Validate separate supervisor invitations before attaching any researchers."""
    from common import problem_info
    details = dict(problem_details or {})
    for config in configs:
        problem = config['problem']
        if problem not in details:
            details[problem] = problem_info(config['url'], problem, config.get('disable_web', False),
                                            config.get('platform_proxy'))
    required = {name for name, info in details.items() if info.get('access_mode') == 'invite_only'}
    codes = read(invitation_file) if invitation_file else {}
    if not isinstance(codes, dict) or set(codes) != required:
        raise ValueError('Provide --supervisor-invitations-file with one separate supervisor invitation per assigned invite-only problem.')
    if any(not isinstance(code, str) or not code.strip() or len(code) > 100 for code in codes.values()):
        raise ValueError('Each supervisor invitation must be a nonempty code of at most 100 characters.')
    researcher_codes = {w['invitation_code'] for config in configs for w in config['workers'] if w.get('invitation_code')}
    if len(set(codes.values())) != len(codes) or researcher_codes.intersection(codes.values()):
        raise ValueError('Use a distinct supervisor invitation for each problem; do not reuse researcher invitations.')
    return codes


def identity(work):
    for path in (work / '.rstation/credentials.json', work / 'secrets/credentials.json'):
        data = read(path, {})
        for key in ('handle', 'agent_name', 'username', 'name'):
            if data.get(key):
                return data[key]
    return None


def binding(work):
    return Path(work) / '.runtime/supervision.json'


def install(work, db, actor, *, supervisor=False):
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    save(binding(work), dict(database=str(db), actor=actor, supervisor=supervisor))
    commands = {'mail': 'mail'}
    if supervisor:
        commands.update(meta_reflection='reflection', **{'supervisor-wait': 'wait'})
    for name, command in commands.items():
        text = '#!/usr/bin/env bash\n# rStation optional supervision helper\nexec ' + shlex.join([
            str(ROOT / '.venv/bin/python'), str(ROOT / 'supervision.py'), command,
            '--binding', str(binding(work))]) + ' "$@"\n'
        path = work / name
        if path.exists() and 'rStation optional supervision helper' not in path.read_text():
            raise ValueError(f'An unrelated {name} already exists in {work}.')
        path.write_text(text)
        path.chmod(0o700)


def assignment(config, work):
    box = Mailbox(config['supervision_database'])
    people = box.participants()
    sections = [f'''# Runtime Assignment

Workspace: {work}
Toolkit root: {ROOT}
Private account credentials: {work}/.rstation/credentials.json (never print the key).
Existing account: {identity(work) or 'none; choose your distinctive name and register in lite mode'}.
rStation base URL: {config['url']}; API base: {config['url']}/api/v1.
Use the configured shell proxy for rStation; keep model-provider routing unchanged.
Mailbox: `./mail`. Participants: `./mail participants`. Your mailbox name is `supervisor`.
Meta-reflection: `./meta_reflection`; after seriously reflecting, complete with `./meta_reflection --complete --file RESPONSE.md`.
Long wait: `./supervisor-wait` (30 minutes by default). This schedules a wait and returns immediately. End your turn after invoking it; the controller will wait without model calls and wake you for a message or due duty.
First announcement template: {work}/supervisee-announcement.md.
Full protocol and assigned roster: {work}/RESEARCH_INSTRUCTIONS.md. Reread it whenever you need to check your instructions; on notice of a protocol update, reread the updated file.
All dates and deadlines use UTC. Keep AGENT_RECORDS.md, APPROACH_REGISTRY.md and CONTINUATION.md in your workspace.
Do not use subagents. No GPU is assigned to the supervisor. Do not run experiments or verification code.
''']
    invitations = work / '.rstation/invitations.json'
    if invitations.exists():
        sections.append(f'''## Private problem invitations
Your human operator has supplied your own invitation for each assigned invite-only problem in `{invitations}`.
After registering your own account, read that private file and redeem each code with authenticated `POST /api/v1/agents/me/invitations`, body `{{"invitation_code":"<your code>"}}`. Confirm `can_participate: true` for each assigned invite-only problem before public participation. Keep codes private and do not use researcher invitations. Reuse existing grants when resuming; do not redeem a consumed code again.
''')
    sections.append('## Assigned researchers\n')
    for person in people:
        if person['role'] != 'researcher':
            continue
        member = Path(person['workspace'])
        run = read(member.parent / 'run.json')
        label = identity(member) or person['label']
        continuity = ''
        if config.get('replacement_managed') or run.get('replacement_id'):
            continuity = ('Fresh successor; predecessor workspace: ' + run['predecessor_workspace']
                          if run.get('replacement_id') else 'Continuing researcher; not replaced in this restart cycle.')
        sections.append(f'''### {label}
Mailbox recipient: `{person['actor']}` (also accepts `{person['label']}`).
Problem: r/{run['problem']}
Task URL: {run['url']}/problems/{run['problem']}
Run: {run['name']}; worker: {member.name}
Workspace: {member}
{continuity}
Read current research notes, results and relevant community work before first contact. Keep the researcher's current assignment, human-operator instructions and running experiments in force.
''')
    return '\n'.join(sections)


def protocol_text(name, config):
    protocol = (PROMPTS / name).read_text().replace('https://rstation.ai', config['url'])
    if config.get('disable_web'):
        protocol = protocol.replace('on rStation and in the external literature', 'within this rStation instance')
        protocol = protocol.replace('read relevant external literature', 'read relevant papers within this rStation instance')
        protocol = protocol.replace('Check the external human literature as well as rStation:', 'Check the papers within this rStation instance:')
        protocol += '\nThis is an isolated research run. Use only this rStation instance, assigned local workspaces and preinstalled libraries. Do not use external websites, search or downloads.\n'
    return protocol


def supervisor_prompt(config, work):
    protocol = protocol_text('supervisor.md', config)
    return protocol + '\n\n' + assignment(config, work)


def create_group(name, members, env, *, check=True, invitation_file=None, invitations=None):
    """Attach one supervisor to existing workers, or a just-created roster."""
    from main import run_dir, preflight, prepare_workspaces, start_controller
    if not NAME.fullmatch(name):
        raise ValueError('Invalid supervision group name.')
    root = run_dir(name).parent
    group = root / '.supervision' / name
    supervisor_run = run_dir(name + '_supervisor')
    participants, configs = [], []
    for item in members:
        run_name, sep, worker_name = item.partition(':')
        if not sep or not NAME.fullmatch(worker_name):
            raise ValueError('Use --member RUN:WORKER for each assigned researcher.')
        run = run_dir(run_name)
        config = read(run / 'run.json')
        if not config or config.get('role') == 'supervisor' or worker_name not in {w['name'] for w in config['workers']}:
            raise ValueError('Assigned researcher not found.')
        if not config.get('live_input', False):
            raise ValueError('Resume the selected research run with the updated toolkit before assigning a supervisor; native CLI messaging is not enabled.')
        work = run / worker_name
        if not work.is_dir():
            raise ValueError('Assigned researcher workspace does not exist.')
        old = read(binding(work))
        if old and Mailbox(old['database']).setting('active'):
            raise ValueError('A researcher already belongs to an active supervision group.')
        if (work / '.runtime/PAUSE').exists():
            raise ValueError('Resume the selected researcher before assigning a supervisor.')
        participants.append(dict(actor=f'{run_name}/{worker_name}', label=identity(work) or f'{run_name}/{worker_name}',
                                 role='researcher', workspace=str(work)))
        configs.append(config)
    if not participants or len({p['actor'] for p in participants}) != len(participants):
        raise ValueError('Select one or more distinct researchers.')
    if len({c['url'] for c in configs}) != 1:
        raise ValueError('A supervisor group must use one rStation instance.')
    if len({c.get('disable_web', False) for c in configs}) != 1:
        raise ValueError('Do not mix isolated and web-enabled researchers in a supervision group.')
    if invitations is None:
        invitations = supervisor_invitations(configs, invitation_file)
    # Same-user attachment/setup is serialized; no race can assign a worker twice.
    with lock(root / '.supervision/setup.lock'):
        if group.exists() or supervisor_run.exists():
            raise ValueError('This supervision group already exists. Resume its supervisor instead.')
        for person in participants:
            old = read(binding(person['workspace']))
            if old and Mailbox(old['database']).setting('active'):
                raise ValueError('A researcher already belongs to an active supervision group.')
            for helper in ('mail',):
                path = Path(person['workspace']) / helper
                if path.exists() and 'rStation optional supervision helper' not in path.read_text():
                    raise ValueError(f'An unrelated {helper} helper already exists.')
        template = next((c for c in configs if any(w['kind'] == 'codex' for w in c['workers'])), configs[0])
        config = {**template, 'name': supervisor_run.name, 'tmux': 'rs-' + supervisor_run.name,
                  'role': 'supervisor', 'supervision_database': str(group / 'mail.sqlite3'),
                  'supervision_group': name, 'subagents': 0, 'storage_gb': 5,
                  'created': datetime.now(timezone.utc).isoformat(),
                  'idle_seconds': 7200, 'live_input': True, 'instructions': '',
                  'workers': [dict(name='supervisor', kind='codex', model='gpt-6-astra', gpus=[])]}
        # Do not copy researcher invitations, resource notes or account credentials.
        if check:
            preflight(config, env)
        work = supervisor_run / 'supervisor'
        participants.append(dict(actor='supervisor', label='supervisor', role='supervisor', workspace=str(work)))
        box = Mailbox(config['supervision_database'])
        box.create(participants)
        supervisor_run.mkdir(mode=0o700)
        save(supervisor_run / 'run.json', config)
        if invitations:
            save(work / '.rstation/invitations.json', invitations)
        prepare_workspaces(supervisor_run, config)
        for person in participants:
            install(Path(person['workspace']), box.path, person['actor'], supervisor=person['role'] == 'supervisor')
        (work / 'supervisee-announcement.md').write_text(protocol_text('supervisee-announcement.md', config))
        (work / 'RUNTIME_ASSIGNMENT.md').write_text(assignment(config, work))
        save(group / 'group.json', dict(name=name, supervisor_run=str(supervisor_run), members=members, created=time.time()))
        start_controller(supervisor_run, config, env)
    return supervisor_run


def end_group(name, notice=None):
    from main import run_dir, stop
    group = run_dir(name).parent / '.supervision' / name
    info = read(group / 'group.json')
    if not info:
        raise ValueError('Supervision group not found.')
    box = Mailbox(group / 'mail.sqlite3')
    stop(Path(info['supervisor_run']))
    box.end()
    for person in box.participants():
        work = Path(person['workspace'])
        if person['role'] == 'researcher':
            Messages(work).cancel_queued('supervision:' + box.setting('id') + ':')
            Messages(work).add(notice or 'The human operator has ended this supervision assignment. Continue independent research under your existing problem and platform rules. No supervisor proposal or pivot approval is required.',
                               priority='owner', delivery_key='end-supervision:' + box.setting('id'))
        # Preserve correspondence; disable wrappers through the inactive mailbox.
        sleep_path = work / '.runtime/supervisor-wait.json'
        sleep_path.unlink(missing_ok=True)


def tick(config):
    """Only supervisor controllers call this; existing researcher loops are unchanged."""
    box = Mailbox(config['supervision_database'])
    box.refresh_labels({p['actor']: identity(Path(p['workspace'])) for p in box.participants()})
    box.deliver()
    if not box.setting('active'):
        return
    state = box.setting('reflection')
    if time.time() >= state['due']:
        person = next(p for p in box.participants() if p['role'] == 'supervisor')
        Messages(person['workspace']).add('Your daily supervisory reflection is due. Run ./meta_reflection, seriously reflect on its sampled prompt, and complete it with a written response.',
            source='Supervisor schedule', delivery_key=f'supervision-reflection:{box.setting("id")}:{state["due"]}')


def sleeping(work):
    """A worker without a supervisor marker always follows its old continuation path."""
    if not read(binding(work), {}).get('supervisor'):
        return False
    state = read(Path(work) / '.runtime/supervisor-wait.json', {})
    return state.get('until', 0) > time.time()


def wake(work):
    (Path(work) / '.runtime/supervisor-wait.json').unlink(missing_ok=True)


def parser():
    p = argparse.ArgumentParser(description='Private supervision tools')
    sub = p.add_subparsers(dest='tool', required=True)
    mail = sub.add_parser('mail')
    mail.add_argument('--binding', type=Path, required=True)
    actions = mail.add_subparsers(dest='action', required=True)
    s = actions.add_parser('send')
    s.add_argument('recipient'); s.add_argument('text', nargs='?'); s.add_argument('--file', type=Path)
    s.add_argument('--kind', choices=['message', 'proposal', 'pivot', 'renewal'], default='message')
    s.add_argument('--reply-to', type=int); s.add_argument('--decision', choices=['approve', 'reject', 'revise'])
    for command in ('inbox', 'history', 'participants'):
        actions.add_parser(command)
    for command in ('read', 'archive'):
        actions.add_parser(command).add_argument('number', type=int)
    reflection = sub.add_parser('reflection')
    reflection.add_argument('--binding', type=Path, required=True)
    reflection.add_argument('--complete', action='store_true'); reflection.add_argument('--file', type=Path)
    wait = sub.add_parser('wait')
    wait.add_argument('--binding', type=Path, required=True)
    wait.add_argument('--minutes', type=float, default=30)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        spec = read(args.binding)
        if not spec:
            raise ValueError('No supervisor is assigned to this workspace.')
        box, actor = Mailbox(spec['database']), spec['actor']
        work = args.binding.parent.parent
        if args.tool == 'mail':
            if args.action == 'send':
                if (args.text is None) == (args.file is None):
                    raise ValueError('Provide message text or --file, once.')
                result = box.send(actor, args.recipient, args.file.read_text() if args.file else args.text,
                                  kind=args.kind, reply_to=args.reply_to, decision=args.decision)
                # Mail itself works while the supervisor is down. The outbox
                # pump retries independently if native notification fails.
                print(json.dumps(result, indent=2))
                try:
                    box.deliver()
                except (OSError, sqlite3.Error):
                    print('Saved durably; native notification is awaiting delivery.', file=sys.stderr)
            elif args.action in {'inbox', 'history'}:
                print(json.dumps(box.inbox(actor, history=args.action == 'history'), indent=2))
            elif args.action == 'participants':
                print(json.dumps(box.participants(), indent=2))
            else:
                print(json.dumps(box.read(actor, args.number, archive=args.action == 'archive'), indent=2))
        elif args.tool == 'reflection':
            if bool(args.file) != args.complete:
                raise ValueError('Complete a reflection with --complete --file RESPONSE.md.')
            import yaml
            pool = [p['text'] for p in yaml.safe_load((PROMPTS / 'supervisor-meta.yaml').read_text())]
            state = box.reflection(actor, pool, args.file.read_text() if args.file else None)
            if args.complete:
                with (work / 'REFLECTION.md').open('a') as output:
                    output.write('\n\n## Supervisor reflection ' + utc(state['completed']) + '\n\n' + state['prompt'] + '\n\n' + state['answer'] + '\n')
            print(json.dumps(state, indent=2))
        else:
            if not spec['supervisor'] or not box.setting('active'):
                raise ValueError('Only an active supervisor can schedule this wait.')
            if not 0 < args.minutes <= 30:
                raise ValueError('Wait for more than zero and at most 30 minutes.')
            due = box.setting('reflection')['due']
            until = min(time.time() + args.minutes * 60, due)
            save(work / '.runtime/supervisor-wait.json', dict(until=until))
            print('Wait scheduled until ' + utc(until) + ', or an incoming message. End your turn now.')
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc) if isinstance(exc, ValueError) else 'Local tool failed (' + type(exc).__name__ + ').', file=sys.stderr)
        raise SystemExit(1)


if __name__ == '__main__':
    main()

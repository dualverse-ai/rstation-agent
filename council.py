"""Private research consultations: policy, durable requests and the worker CLI."""
import argparse
import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.request

from common import ROOT, NAME, alive, initial_prompt, lock, read, save

MODELS = {'codex': ['gpt-6-astra'], 'claude': ['claude-opus-5-5'], 'gemini': ['gemini-4-carbon', 'gemini-4-argon']}
TERMINAL = {'complete', 'failed', 'cancelled'}
MAX_INPUT_BYTES = 2 * 1024 * 1024


def enabled(config, worker):
    return config.get('role') != 'supervisor' and bool(worker.get('council', config.get('council', False)))


def guidance():
    return (ROOT/'prompts/council_researcher.md').read_text()


def directory(run):
    return Path(run)/'.council'


class Store:
    capability = "council"
    def __init__(self, run):
        self.run = Path(run)
        self.root = Path(run)/("."+self.capability)

    def change(self, operation):
        with lock(self.root/'store.lock'):
            data = read(self.root/'store.json', {'sessions': {}})
            result = operation(data)
            save(self.root/'store.json', data)
            return result

    def snapshot(self):
        return read(self.root/'store.json', {'sessions': {}})

    def policy(self):
        return read(self.root/'policy.json', {})

    def worker(self, name):
        config = read(self.run/'run.json', {})
        if config.get('role') == 'supervisor':
            raise ValueError('Supervisors do not use council.')
        worker = next((w for w in config.get('workers', []) if w['name'] == name), None)
        if not worker:
            raise ValueError('Worker not found.')
        return worker

    def require_enabled(self, worker):
        self.worker(worker)
        policy = self.policy()
        if worker not in policy.get('enabled', []):
            raise ValueError(self.capability.title()+' is disabled. The human operator enables it.')
        return policy

    @staticmethod
    def key(worker, name):
        if not NAME.fullmatch(worker) or not NAME.fullmatch(name):
            raise ValueError('Use simple worker and session names (letters, numbers, underscore or hyphen).')
        return worker+':'+name

    def session_fields(self, worker, prompt):
        return {}

    def submit(self, worker, name, prompt, *, family=None, model=None):
        key = self.key(worker, name)
        if not prompt.strip():
            raise ValueError('The consultation prompt is empty.')
        if len(prompt.encode()) > MAX_INPUT_BYTES:
            raise ValueError('The brief and attachments together must be at most 2 MB.')
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        def add(data):
            policy = self.require_enabled(worker)
            session = data['sessions'].get(key)
            if family:
                if self.capability == 'council' and family == self.worker(worker)['kind']:
                    raise ValueError('Choose an adviser from a different model family.')
                models = policy['models'].get(family, [])
                chosen = model or (models[0] if models else None)
                if chosen not in models:
                    raise ValueError('Model unavailable; use ./'+self.capability+' models.')
                if session and (session['family'], session['model']) != (family, chosen):
                    raise ValueError('That session has a different family or model. Choose a new session name.')
                if not session:
                    session = dict(worker=worker, name=name, family=family, model=chosen, turns=[],
                                   **self.session_fields(worker, prompt))
                    data['sessions'][key] = session
                elif not session['turns'] or session['turns'][0]['digest'] != digest:
                    raise ValueError('Session exists; use resume to ask a follow-up, or choose another name.')
                else:
                    return session['turns'][0]['number']
            elif not session:
                raise ValueError('Session not found. Use run to start one.')
            if session['model'] not in policy['models'].get(session['family'],[]):
                raise ValueError('This adviser model is no longer enabled by the human operator.')
            repeated=next((t for t in session['turns'] if t['digest']==digest),None)
            if repeated:
                return repeated['number']
            if self.capability=='council' and len([t for s in data['sessions'].values() if s['worker']==worker for t in s['turns']
                    if t['status'] not in TERMINAL]) >= policy['max_pending']:
                raise ValueError(self.capability.title()+' queue is full. Read or cancel pending tasks first.')
            number = len(session['turns'])+1
            session['turns'].append(dict(number=number, prompt=prompt, digest=digest,
                                         status='queued', created=time.time()))
            return number
        return self.change(add)

    def resume(self, worker, name):
        key = self.key(worker, name)
        def recover(data):
            policy=self.require_enabled(worker)
            session = data['sessions'].get(key)
            if not session or not session['turns']:
                raise ValueError('Session not found.')
            if session['model'] not in policy['models'].get(session['family'],[]):
                raise ValueError('This adviser model is no longer enabled by the human operator.')
            turn = next((t for t in session['turns'] if t['status']=='failed'), session['turns'][-1])
            if turn['status'] in {'failed','cancelled'}:
                if turn.get('error') == 'session_unavailable':
                    raise ValueError('The native session is unavailable. Start a new named session with selected saved context.')
                turn.update(status='queued', retry_generation=turn.get('retry_generation',0)+1)
                turn.pop('notified', None)
                turn.pop('cancel_requested', None)
            return turn['number']
        return self.change(recover)

    def cancel(self, worker, name):
        self.worker(worker)
        key = self.key(worker, name)
        def stop(data):
            session = data['sessions'].get(key)
            if not session: raise ValueError('Session not found.')
            for turn in session['turns']:
                if turn['status']=='failed':turn.update(status='cancelled',notified=False)
                elif turn['status'] not in TERMINAL: turn['cancel_requested'] = True
        self.change(stop)

    def update_turn(self, key, number, **fields):
        def update(data):
            data['sessions'][key]['turns'][number-1].update(fields)
        self.change(update)


def load_input(work, prompt, prompt_file, attachments):
    work=Path(work).resolve()
    def content(path):
        candidate=Path(path).resolve()
        if not candidate.is_relative_to(work): raise ValueError('Choose a research file within your workspace.')
        parts=candidate.relative_to(work).parts
        if any(p.startswith('.') or any(s in p.lower() for s in ('credential','api-key','api_key','secret')) for p in parts):
            raise ValueError('Private configuration and credentials cannot be attached.')
        if not candidate.is_file() or candidate.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError('Choose a text file no larger than 2 MB.')
        try: return candidate.read_text(encoding='utf-8')
        except UnicodeError: raise ValueError('Attachments must be UTF-8 text; provide selected text or a CSV summary.') from None
    text=content(prompt_file) if prompt_file else prompt
    if text is None:
        if attachments: raise ValueError('Attachments require a new prompt.')
        return None
    for path in attachments:
        text+='\n\nAttached research material: '+Path(path).name+'\n<attachment>\n'+content(path)+'\n</attachment>'
    return text


def worker_cli(run, worker, argv, store_type=Store):
    store=store_type(run)
    capability=store.capability
    p=argparse.ArgumentParser(prog='./'+capability,description='Delegate coding.' if capability=='coder' else 'Consult another model family about research.')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('models');sub.add_parser('status')
    start=sub.add_parser('run')
    if capability=='council':start.add_argument('family',choices=MODELS)
    else:start.add_argument('--path',default='.',help='Code directory within your workspace.')
    start.add_argument('--session',required=True)
    if capability=='council':start.add_argument('--model')
    resume=sub.add_parser('resume');resume.add_argument('session')
    for command in (start,resume):
        group=command.add_mutually_exclusive_group(required=command is start)
        group.add_argument('--prompt');group.add_argument('--prompt-file',type=Path)
        command.add_argument('--attach',type=Path,action='append',default=[])
    show=sub.add_parser('read');show.add_argument('session');show.add_argument('--all',action='store_true')
    cancel=sub.add_parser('cancel');cancel.add_argument('session')
    if capability=='coder':
        for action in ('diff','apply'):sub.add_parser(action).add_argument('session')
    args=p.parse_args(argv);identity=store.worker(worker)
    if args.command=='models':
        policy=store.require_enabled(worker)
        for kind,models in policy['models'].items():
            if capability=='coder' or kind!=identity['kind']:print(kind+': '+', '.join(models))
    elif args.command in {'run','resume'}:
        text=load_input(Path(run)/worker,args.prompt,args.prompt_file,args.attach)
        extra={'path':args.path} if capability=='coder' and args.command=='run' else {}
        number=(store.resume(worker,args.session) if text is None else
                store.submit(worker,args.session,text,family=getattr(args,'family',None),model=getattr(args,'model',None),**extra))
        turn=store.snapshot()['sessions'][store.key(worker,args.session)]['turns'][number-1]
        print(f"{args.session}: {turn['status']}. Read with ./{capability} read {args.session}.")
        if getattr(store,'local_worker',None):
            from coder import launch_task
            launch_task(run,worker,args.session)
        else:
            ctl=read(store.root/'controller.json',{})
            if turn['status'] not in TERMINAL and not alive(ctl.get('pid'),ctl.get('token')):
                print(capability.title()+' service is offline; request saved. Ask the human operator to resume the helper service.')
    elif args.command=='cancel':store.cancel(worker,args.session);print('Cancellation requested; saved conversation retained.')
    elif args.command=='status':
        for s in store.snapshot()['sessions'].values():
            if s['worker']==worker:print(f"{s['name']}: {s['family']} / {s['model']} — {s['turns'][-1]['status']}")
    elif args.command in {'diff','apply'}:
        print(store.changes(worker,args.session,apply=args.command=='apply'))
    else:
        session=store.snapshot()['sessions'].get(store.key(worker,args.session))
        if not session:raise ValueError('Session not found.')
        for turn in session['turns'] if args.all else session['turns'][-1:]:
            print(f"{session['name']} / {session['family']} / turn {turn['number']} ({turn['status']})")
            if args.all:print('\nResearcher:\n'+turn['prompt'])
            print('\n'+turn.get('answer',turn.get('error','Reply pending.'))+'\n')
            if turn.get('answer'):store.update_turn(store.key(worker,args.session),turn['number'],read_at=time.time())


def prepare_worker(run, config, worker):
    work=Path(run)/worker['name']
    if not enabled(config,worker):return
    command=[sys.executable,str(ROOT/'main.py'),'_council',str(Path(run).resolve()),worker['name']]
    helper=work/'council'
    helper.write_text('#!/usr/bin/env bash\nexec '+shlex.join(command)+' "$@"\n');helper.chmod(0o755)
    from capabilities import install_skills
    install_skills(run,config,worker)
    (work/'COUNCIL.md').write_text(guidance())


def task_statement(config):
    proxy=config.get('platform_proxy')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({'http':proxy,'https':proxy} if proxy else {}))
    address=config['url']+'/api/v1/problems/'+config['problem']+'/statement/download'
    with opener.open(address,timeout=30) as response:
        text=response.read(MAX_INPUT_BYTES+1).decode('utf-8')
        if 'text/html' in response.headers.get('Content-Type','') or len(text.encode())>MAX_INPUT_BYTES:
            raise ValueError('Could not retrieve the full task statement.')
    if not text.strip():raise ValueError('The task statement is empty.')
    return text


def configure(run, selected, enable, env, *, models=None, limits=None, statement=None, notify=True):
    from recovery import routes, executable
    from messages import Messages
    store=Store(run);config=read(Path(run)/'run.json',{})
    if config.get('role')=='supervisor':raise ValueError('Supervisors do not use council.')
    workers=[w for w in config.get('workers',[]) if selected in {'all',w['name']}]
    if not workers:raise ValueError('Worker not found.')
    previous=store.policy()
    available=models or previous.get('models') or {
        kind: choices for kind, choices in MODELS.items()
        if env.get('RSTATION_WORKER_'+kind.upper()+'_API_KEY') or env.get(kind.upper()+'_API_KEY')
    }
    if enable:
        if not isinstance(available,dict) or not available or any(k not in MODELS or not isinstance(v,list) or not v or
            any(not isinstance(m,str) or not m.strip() for m in v) for k,v in available.items()):raise ValueError('Provide a nonempty model list for each configured family.')
        for w in workers:
            if not any(k!=w['kind'] for k in available):raise ValueError('Each researcher needs an available adviser from another family.')
        for kind in available:routes(kind,env);executable(kind,env)
        statement=statement if statement is not None else task_statement(config)
    policy={**dict(models=available,enabled=[],max_concurrent=2,max_pending=12,turn_seconds=1800,attempt_seconds=300,max_attempts=16),**previous}
    policy['models']=available
    if limits:policy.update({k:v for k,v in limits.items() if v is not None})
    for key in ('max_concurrent','max_pending','turn_seconds','attempt_seconds','max_attempts'):
        if not isinstance(policy[key],int) or policy[key]<1:raise ValueError('Council limits must be positive integers.')
    with lock(store.root/'store.lock'):
        active=set(store.policy().get('enabled',[]))
        for w in workers:
            if enable:active.add(w['name'])
            else:active.discard(w['name'])
        policy['enabled']=sorted(active)
        if enable:policy['statement']=statement
        save(store.root/'policy.json',policy)
        with lock(Path(run)/'run.lock'):
            current=read(Path(run)/'run.json')
            for w in current['workers']:
                if any(w['name']==target['name'] for target in workers):w['council']=enable
            save(Path(run)/'run.json',current)
    config=current
    from capabilities import install_skills
    for target in workers:
        w=next(v for v in current['workers'] if v['name']==target['name'])
        prepare_worker(run,config,w);install_skills(run,config,w)
        (Path(run)/w['name']/'RESEARCH_INSTRUCTIONS.md').write_text(initial_prompt(config,w,Path(run)/w['name']))
        if w['kind']=='gemini':
            from antigravity import prepare_profile
            prepare_profile(Path(run)/w['name'],config.get('disable_web',False),w['model'],config.get('gemini_context_tokens',500000),council=enable,coder=__import__('coder').enabled(config,w))
        if notify:
            text=('Your human operator has enabled a research council for you. Read .agents/skills/council/SKILL.md. You can choose an adviser from another model family, resume its conversation and ask follow-up questions using ./council. Use it for substantive research ideas and strategy, not coding delegation. Your research and supervisor arrangements continue.' if enable else
                  'Your human operator has disabled new council consultations. Saved replies remain readable. Continue your research independently.')
            Messages(Path(run)/w['name']).add(text,source='Council configuration')
    return policy


def start_service(run, env, *, capability='council'):
    root=Path(run)/('.'+capability);root.mkdir(parents=True,exist_ok=True)
    with lock(root/'start.lock'):
        state=read(root/'controller.json',{})
        if alive(state.get('pid'),state.get('token')):return
        with (root/'service.log').open('a') as log:
            p=subprocess.Popen([sys.executable,str(ROOT/(capability+'_service.py')),'watch',str(Path(run).resolve())],
                env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            state=read(root/'controller.json',{})
            if state.get('pid')==p.pid and alive(p.pid,state.get('token')):return
            if p.poll() is not None:raise ValueError(capability.title()+' service could not start; inspect its private service.log.')
            time.sleep(.05)
        raise ValueError(capability.title()+' service did not become ready; inspect its private service.log.')

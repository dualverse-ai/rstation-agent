"""Fixed-model implementation tasks on the shared durable helper lifecycle."""
import os
from pathlib import Path
import shlex
import sys
import subprocess
import copy

from common import ROOT, alive, initial_prompt, lock, read, save
from council import Store as CouncilStore, worker_cli as helper_cli, TERMINAL

DEFAULT_MODEL = 'gpt-6.2-sol'


def enabled(config, worker):
    return config.get('role') != 'supervisor' and bool(worker.get('coder', config.get('coder', False)))


class Store(CouncilStore):
    capability = 'coder'

    def policy(self):
        policy=super().policy()
        if policy.get('execution')=='local':
            policy['enabled']=policy.get('local_enabled',[])
        return policy

    def session_fields(self, worker, prompt):
        engine=read(self.root/'engine.json',{})
        # A running old engine must finish its tasks before adopting direct work.
        mode='snapshot' if alive(engine.get('pid'),engine.get('token')) and not engine.get('direct_workspace') else 'direct'
        return dict(workspace_mode=mode)

    def submit(self, worker, name, prompt, *, family=None, model=None, path=None):
        # Parents cannot override the operator's model, even through Python.
        if model is not None or family not in (None, 'codex'):
            raise ValueError('The human operator selects the coder model.')
        from coder_workspace import target_path
        existing=self.snapshot()['sessions'].get(self.key(worker,name))
        if path is not None:
            target=target_path(self.run/worker,path)
            prefix='Code directory: '+str(target)+'\n\n'
            prompt=prefix+prompt
            if existing and existing['turns'][0]['prompt']!=prompt:
                raise ValueError('Session exists; use resume or choose another name.')
        return super().submit(worker,name,prompt,family='codex' if not existing or path is not None else None,
                              model=(existing['model'] if existing else self.require_enabled(worker)['model']))

    def changes(self, worker, name, *, apply=False):
        from coder_workspace import changes
        key=self.key(worker,name)
        # Block submit/resume while inspecting or applying a completed task.
        with lock(self.root/'store.lock'):
            session=self.snapshot()['sessions'].get(key)
            if not session or session['turns'][-1]['status']!='complete':
                raise ValueError('Wait for the coding task to complete first.')
            if session.get('workspace_mode')=='direct':
                return 'Coder edited your workspace directly; no apply step is needed.'
            return changes(self.root/worker/name,apply=apply)


def prepare_worker(run, config, worker):
    if not enabled(config,worker):return
    work=Path(run)/worker['name']
    command=[sys.executable,str(ROOT/'main.py'),'_coder',str(Path(run).resolve()),worker['name']]
    helper=work/'coder'
    helper.write_text('#!/usr/bin/env bash\nexec '+shlex.join(command)+' "$@"\n');helper.chmod(0o755)
    from capabilities import install_skills
    install_skills(run,config,worker)


def configure(run, selected, enable, env, *, model=None, limits=None, notify=True):
    from recovery import routes, executable
    from messages import Messages
    store=Store(run);config=read(Path(run)/'run.json',{})
    if config.get('role')=='supervisor':raise ValueError('Supervisors do not use coder.')
    workers=[w for w in config.get('workers',[]) if selected in ('all',w['name'])]
    if not workers:raise ValueError('Worker not found.')
    chosen=model or store.policy().get('model') or config.get('coder_model') or DEFAULT_MODEL
    if not isinstance(chosen,str) or not chosen.startswith('gpt-') or any(c.isspace() for c in chosen):
        raise ValueError('Choose a Codex GPT model.')
    if enable:routes('codex',env);executable('codex',env)
    with lock(store.root/'store.lock'):
        previous=store.policy()
        policy={**dict(turn_seconds=7200,attempt_seconds=900,max_attempts=2**31),**previous}
        if limits:policy.update({k:v for k,v in limits.items() if v is not None})
        for key in ('turn_seconds','attempt_seconds','max_attempts'):
            if not isinstance(policy[key],int) or policy[key]<1:raise ValueError('Coder limits must be positive integers.')
        policy.pop('max_concurrent',None)
        policy.pop('max_pending',None)
        # Existing named conversations retain their model after an operator change.
        models={s['model'] for s in store.snapshot()['sessions'].values()}
        policy.update(model=chosen,models={'codex':sorted(models|{chosen})})
        active=set(previous.get('enabled',[]))
        for w in workers:
            if enable:active.add(w['name'])
            else:active.discard(w['name'])
        policy['enabled']=sorted(active)
        if policy.get('execution')=='local':policy['local_enabled']=sorted(active)
        save(store.root/'policy.json',policy)
        with lock(Path(run)/'run.lock'):
            config=read(Path(run)/'run.json')
            for w in config['workers']:
                if w['name'] in {v['name'] for v in workers}:w['coder']=enable
            config['coder_model']=chosen;save(Path(run)/'run.json',config)
    from capabilities import install_skills
    for w in config['workers']:
        if w['name'] not in {v['name'] for v in workers}:continue
        prepare_worker(run,config,w);install_skills(run,config,w)
        work=Path(run)/w['name']
        (work/'RESEARCH_INSTRUCTIONS.md').write_text(initial_prompt(config,w,work))
        if w['kind']=='gemini':
            from antigravity import prepare_profile
            from council import enabled as council_enabled
            prepare_profile(work,config.get('disable_web',False),w['model'],config.get('gemini_context_tokens',500000),
                            council=council_enabled(config,w),coder=enable)
        if notify:
            Messages(work).add('Coder is enabled. Read .agents/skills/coder/SKILL.md. Delegate coding through ./coder and focus on science.' if enable else
                               'New coder tasks are disabled; saved results remain available.',source='Coder configuration')
    if policy.get('execution')=='local':start_service(run,env)
    return policy


class LocalStore(Store):
    def __init__(self,run,worker):
        super().__init__(run)
        self.local_worker=worker
        self.root=Path(run)/worker/'.runtime/coder'

    def policy(self):
        return read(self.root/'policy.json',{})

    def session_fields(self,worker,prompt):
        return dict(workspace_mode='direct')


def start_service(run,env):
    """Install each shell's dedicated launch settings; start no shared service."""
    store=Store(run);policy=store.policy()
    policy.pop('max_concurrent',None);policy.pop('max_pending',None)
    config=read(Path(run)/'run.json',{})
    launch_env={k:v for k,v in env.items() if k.startswith('RSTATION_WORKER_CODEX_') or k in
                ('CODEX_BIN_PATH','PATH','LANG','LC_ALL','SSL_CERT_FILE','SSL_CERT_DIR','NODE_EXTRA_CA_CERTS')}
    for worker in config.get('workers',[]):
        name=worker['name']
        local=LocalStore(run,name)
        active=name in policy.get('enabled',[])
        if not active and not local.root.exists():continue
        local.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        save(local.root/'policy.json',{**policy,'enabled':[name] if active else []})
        save(local.root/'environment.json',launch_env)
    # Old controllers may still be loaded: prevent them from launching new shared work.
    with lock(store.root/'store.lock'):
        raw=read(store.root/'policy.json',{})
        raw.update(execution='local',local_enabled=policy.get('enabled',[]),enabled=[])
        save(store.root/'policy.json',raw)


def launch_task(run,worker,name):
    """The researcher's command starts one background process for this session."""
    store=LocalStore(run,worker);key=store.key(worker,name)
    session=store.snapshot()['sessions'].get(key)
    if not session or all(t['status'] in TERMINAL for t in session['turns']):return
    task=store.root/worker/name;task.mkdir(parents=True,exist_ok=True)
    with lock(task/'start.lock'):
        state=read(task/'runner.json',{})
        if alive(state.get('pid'),state.get('token')):return
        env=read(store.root/'environment.json',{})
        if not env:raise ValueError('Coder launch settings are missing.')
        with (task/'runner.log').open('ab') as log:
            p=subprocess.Popen([sys.executable,str(ROOT/'coder_service.py'),'task',str(Path(run).resolve()),worker,name],
                cwd=Path(run)/worker,env=env,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
        from common import pid_token
        save(task/'runner.json',dict(pid=p.pid,token=pid_token(p.pid)))


def worker_cli(run,worker,argv):
    local=LocalStore(run,worker)
    if not (local.root/'environment.json').exists():
        return helper_cli(run,worker,argv,Store)
    if argv and argv[0]=='status':
        current=local.snapshot()['sessions']
        for key,session in Store(run).snapshot()['sessions'].items():
            if session['worker']==worker and key not in current:
                print(session['name']+': '+session['family']+' / '+session['model']+' — '+session['turns'][-1]['status'])
    # Retain access to already-running pre-upgrade tasks and their exact stores.
    name=None
    if argv and argv[0] in ('read','diff','apply','cancel','resume') and len(argv)>1:name=argv[1]
    if '--session' in argv:name=argv[argv.index('--session')+1]
    if name and local.key(worker,name) not in local.snapshot()['sessions']:
        legacy=Store(run);session=legacy.snapshot()['sessions'].get(local.key(worker,name))
        if session:
            if argv[0] in ('read','diff','apply','status'):
                return helper_cli(run,worker,argv,Store)
            if any(t['status'] not in TERMINAL for t in session['turns']):
                return helper_cli(run,worker,argv,Store)
            # Alias saved native files in place; never copy a research workspace.
            target=local.root/worker/name;target.parent.mkdir(parents=True,exist_ok=True)
            source=legacy.root/worker/name
            if source.exists() and not target.exists():target.symlink_to(source.resolve(),target_is_directory=True)
            local.change(lambda data:data['sessions'].setdefault(local.key(worker,name),copy.deepcopy(session)))
    return helper_cli(run,worker,argv,lambda _:local)

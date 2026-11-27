"""Concise skill discovery and controller-owned helper availability."""
import importlib
from pathlib import Path
import re
import time

from common import ROOT, alive, read, save

NAMES=('coder','council')
START='<!-- rstation skills -->'
END='<!-- /rstation skills -->'


def selected(config,worker):
    return [name for name in NAMES if importlib.import_module(name).enabled(config,worker)]


def reminder(config,worker,work):
    lines=[]
    for name in selected(config,worker):
        purpose=('Delegate coding, debugging and tests through ./coder; focus your attention on scientific ideas, experiment design and interpretation.' if name=='coder' else
                 'Use ./council for outside scientific ideas and criticism, especially before a pivot or after repeated inconclusive results.')
        lines.append(purpose+' Read '+str(Path(work).resolve()/'.agents/skills'/name/'SKILL.md')+'.')
    return '\n'.join(lines)


def install_skills(run,config,worker):
    work=Path(run)/worker['name'];names=selected(config,worker)
    for name in NAMES:
        canonical=work/'.agents/skills'/name/'SKILL.md'
        aliases=[work/'.codex/skills'/name,work/'.claude/skills'/name,work/'.home/.gemini/skills'/name]
        if name in names:
            canonical.parent.mkdir(parents=True,exist_ok=True)
            canonical.write_text((ROOT/'skills'/name/'SKILL.md').read_text())
            for alias in aliases:
                alias.parent.mkdir(parents=True,exist_ok=True)
                if alias.is_symlink():alias.unlink()
                if not alias.exists():alias.symlink_to(canonical.parent.resolve(),target_is_directory=True)
        else:
            for alias in aliases:
                if alias.is_symlink() and alias.resolve()==canonical.parent.resolve():alias.unlink()
            canonical.unlink(missing_ok=True)
    from owner_history import sync
    sync(work, install_pointer=True)
    if config.get('role')=='supervisor':return
    from common import researcher_instructions
    instruction_start='<!-- rstation research instructions -->'
    instruction_end='<!-- /rstation research instructions -->'
    for filename in ('AGENTS.md','CLAUDE.md','GEMINI.md'):
        path=work/filename;text=path.read_text() if path.exists() else ''
        for start,end in ((START,END),(instruction_start,instruction_end)):
            text=re.sub(re.escape(start)+'.*?'+re.escape(end),'',text,flags=re.S).rstrip()
        content=(researcher_instructions(config,worker,work) if filename=='AGENTS.md' else
                 'Read AGENTS.md for your research assignment, resource rules and available skills.')
        block=instruction_start+'\n'+content.strip()+'\n'+instruction_end
        path.write_text(text+('\n\n' if text else '')+block+'\n')


def health(run,name):
    module=importlib.import_module(name);store=module.Store(run)
    if name=='coder' and store.policy().get('execution')=='local':
        from coder import LocalStore
        names=store.policy().get('enabled',[])
        return dict(enabled=names,execution='local',configured=all(
            (LocalStore(run,w).root/'environment.json').is_file() for w in names))
    ctl=read(store.root/'controller.json',{});eng=read(store.root/'engine.json',{})
    return dict(enabled=store.policy().get('enabled',[]),watcher_alive=alive(ctl.get('pid'),ctl.get('token')),
                engine_alive=alive(eng.get('pid'),eng.get('token')))


def ensure_services(run,env):
    config=read(Path(run)/'run.json',{});errors={}
    if config.get('role')=='supervisor' or (Path(run)/'STOP').exists():return errors
    for name in NAMES:
        module=importlib.import_module(name)
        expected={w['name'] for w in config.get('workers',[]) if module.enabled(config,w)}
        if not expected:continue
        try:
            policy=module.Store(run).policy()
            if not expected<=set(policy.get('enabled',[])):
                raise ValueError('Missing enabled policy; restore the migrated helper state or explicitly enable it.')
            module.start_service(run,env)
        except (OSError,ValueError) as exc:errors[name]=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    save(Path(run)/'helpers-health.json',dict(checked_at=time.time(),errors=errors,**{n:health(run,n) for n in NAMES}))
    return errors


def restore(run,name,source_run):
    """Restore missing migrated helper state without overwriting a new history."""
    import shutil
    import tempfile
    from contextlib import ExitStack
    from common import lock
    module=importlib.import_module(name);target=module.Store(run);source=module.Store(source_run)
    if target.root.resolve()==source.root.resolve():raise ValueError('Source and target must differ.')
    config=read(Path(run)/'run.json');names={w['name'] for w in config['workers']}
    with ExitStack() as locks:
        # Exclude startup, engine writes and concurrent restore/submit operations.
        for store in sorted((source,target),key=lambda s:str(s.root.resolve())):
            for filename in ('start.lock','watch.lock','engine.lock','store.lock'):
                locks.enter_context(lock(store.root/filename,True))
            if any(health(store.run,name)[k] for k in ('watcher_alive','engine_alive')):
                raise ValueError('Stop the helper service before restoring its state.')
        policy=source.policy()
        if not policy.get('enabled') or not names.intersection(policy['enabled']):
            raise ValueError('The source has no matching enabled workers.')
        if target.snapshot()['sessions'] or target.policy().get('enabled'):
            raise ValueError('Target already has helper state; preserve and reconcile it explicitly.')
        with tempfile.TemporaryDirectory(prefix='helper-restore-',dir=Path(run)) as tmp:
            staged=Path(tmp)/'saved';shutil.copytree(source.root,staged,symlinks=True)
            for p in staged.rglob('native.json'):
                state=read(p);state['pid']=None;state['pid_token']=None;save(p,state)
            entries=[p for p in staged.iterdir() if p.name in names]
            if any((target.root/p.name).exists() for p in entries):
                raise ValueError('Target already contains saved helper files.')
            data=source.snapshot();data['sessions']={k:s for k,s in data['sessions'].items() if s['worker'] in names}
            policy['enabled']=sorted(names.intersection(policy['enabled']))
            old_data=target.snapshot();old_policy=target.policy();moved=[]
            try:
                for entry in entries:
                    shutil.move(str(entry),target.root/entry.name);moved.append(entry.name)
                save(target.root/'store.json',data);save(target.root/'policy.json',policy)
            except BaseException:
                for entry in moved:shutil.move(str(target.root/entry),staged/entry)
                save(target.root/'store.json',old_data);save(target.root/'policy.json',old_policy)
                raise
    for worker in config['workers']:module.prepare_worker(run,config,worker)

"""Validated private provider updates, consumed at each researcher's next launch."""
import argparse
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from common import read, save, lock
from worker_control import control_lock, fingerprint, base_settings, effective_worker

LIMIT = 1024 * 1024


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid not in (0, os.geteuid()) or st.st_size > LIMIT:
            raise ValueError('Provider input must be a private regular file owned by this account.')
        with os.fdopen(fd, 'r', closefd=False) as stream: return json.load(stream)
    finally: os.close(fd)


def folder(run): return Path(run) / '.provider-control'
def registry(run):
    path = folder(run) / 'providers.json'
    return read_private(path) if path.exists() else {'revision': 0, 'workers': {}}


def validate(value):
    if not isinstance(value, dict) or set(value) != {'providers'}:
        raise ValueError('Use a providers list containing id, api_key and base_url.')
    entries = value['providers']
    if not isinstance(entries, list) or not 1 <= len(entries) <= 20:
        raise ValueError('Provide between one and twenty providers.')
    result = []
    from common import url
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) - {'id','api_key','base_url','http_proxy','https_proxy'} or
                not {'id','api_key','base_url'} <= set(entry) or
                not isinstance(entry['id'], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', entry['id']) or
                not isinstance(entry['api_key'], str) or not entry['api_key'] or len(entry['api_key']) > 8192 or
                any(c in entry['api_key'] for c in '\r\n\x00;')):
            raise ValueError('Invalid provider record.')
        item = {k: entry.get(k, '') for k in ('id','api_key','base_url','http_proxy','https_proxy')}
        for field in ('base_url','http_proxy','https_proxy'):
            if not isinstance(item[field], str): raise ValueError('Invalid provider address.')
            if item[field] or field == 'base_url': item[field] = url(item[field], 'Provider address')
        result.append(item)
    if len({e['id'] for e in result}) != len(result): raise ValueError('Provider IDs must be unique.')
    return result


def raw(entries): return [{k:v for k,v in e.items() if k != 'id'} for e in entries]
def named(pool): return [dict(r, id='route-'+str(i+1)) for i,r in enumerate(pool)]
def meta(state): return state.get('providers', {})


def active_entries(run, name, state, fallback, data=None):
    record = (registry(run) if data is None else data)['workers'].get(name)
    if record:
        current = meta(state).get('revision', 0)
        if current == record['revision']: return record['after']
        if current == record['before_revision']: return record['before']
        raise ValueError('Provider revision does not match saved state.')
    return named(fallback)


def initial_routes(run, config, environment):
    from recovery import routes
    data = registry(run); result = {}
    for w in config['workers']:
        state = read(Path(run)/w['name']/'.runtime/state.json', {})
        if w['name'] in data['workers']:
            candidate = raw(active_entries(run,w['name'],state,[],data))
        else: candidate = routes(w['kind'],environment)
        if w['name'] not in data['workers']:result[w['kind']]=candidate
        else:result.setdefault(w['kind'],candidate)
    return result


def initialize(manager):
    manager._provider_pools = {}; manager._provider_secrets = set()
    data = registry(manager.run)
    from recovery import routes
    for w in manager.config['workers']:
        name=w['name']; state=manager.states[name]
        fallback = [] if name in data['workers'] else routes(w['kind'],manager.env)
        entries=active_entries(manager.run,name,state,fallback,data)
        pool=raw(entries)
        if meta(state).get('pool_sha256',fingerprint(pool)) != fingerprint(pool):
            raise ValueError('Saved provider identity differs from the active profile.')
        if name in data['workers']:manager._provider_pools[name]=pool
        manager._provider_secrets.update(e['api_key'] for e in entries)
        state['providers']={**meta(state),'revision':meta(state).get('revision',0),
                            'pool_sha256':fingerprint(pool),'ids':[e['id'] for e in entries]}
        manager.persist(name)


def pool_for(manager,name):
    return getattr(manager,'_provider_pools',{}).get(name,manager.route_lists[manager.worker(name)['kind']])


def operator_pool(run,name,state,environment):
    data=registry(run); record=data['workers'].get(name)
    if record and record['revision'] != meta(state).get('revision',0) and meta(state).get('rejected_revision') != record['revision']:
        raise ValueError('Wait for the pending provider update before changing worker settings.')
    from recovery import routes
    worker=base_settings(read(Path(run)/'run.json'),name)['worker']
    entries=active_entries(run,name,state,[] if record else routes(worker['kind'],environment),data)
    return raw(entries)


def selection(state):
    control=state.get('worker_control',{})
    if control.get('provider_route') is not None: return control['provider_route']
    return state.get('session_adoption',{}).get('provider_route',{}).get('number')


def snapshot(config,name,state):
    return dict(base_sha256=fingerprint(base_settings(config,name)), model=effective_worker(base_settings(config,name)['worker'],state)['model'],
                control_revision=state.get('worker_control',{}).get('revision',0),
                session_id=state.get('token'), pin=selection(state),
                provider_revision=meta(state).get('revision',0), pool_sha256=meta(state).get('pool_sha256'))


def require_no_pending(run,name):
    rt=Path(run)/name/'.runtime'
    if (rt/'worker-control-request.json').exists() or (rt/'session-adoption.json').exists():
        raise ValueError('Finish the pending worker or session-adoption request first.')


def probe(config,worker,entries,environment):
    """One exact route/model per disposable native conversation; no saved research input."""
    from recovery import ResearchManager
    if worker['kind'] == 'gemini':
        from gemini_failover import filter_routes
        for entry in entries:
            filter_routes([entry])
    for entry in entries:
        with tempfile.TemporaryDirectory(prefix='rstation-provider-check-') as tmp:
            env=dict(environment)
            for family in ("CODEX","CLAUDE","GEMINI"):
                for backup in ("","BACKUP_"):
                    for field in ("API_KEY","BASE_URL","HTTP_PROXY","HTTPS_PROXY"):
                        key=family+"_"+backup+field
                        if key in env:env["RSTATION_WORKER_"+key]=env.pop(key)
            prefix='RSTATION_WORKER_'+worker['kind'].upper()+'_'
            for field in ('API_KEY','BASE_URL','HTTP_PROXY','HTTPS_PROXY'):
                env[prefix+field]=entry[field.lower()];env[prefix+'BACKUP_'+field]=''
            c={**config,'workers':[{**worker,'gpus':[],'route_seed':0}],'subagents':0,'idle_seconds':60}
            m=ResearchManager(tmp,c,env,probe=True)
            try:
                m.launch_cli_job(worker['name']);deadline=time.monotonic()+90
                while m.active_sessions and time.monotonic()<deadline:
                    m.poll_cli_jobs();time.sleep(.1)
                runtime=Path(tmp)/worker['name']/'.runtime/launch_000001'
                ok=m.states[worker['name']].get('status')=='probe_ok'
                transcript=runtime/'transcript.jsonl'
                if not ok or not transcript.exists() or 'Hello' not in transcript.read_text():
                    raise ValueError('Provider validation failed; current providers were retained.')
            finally:m.close()


def apply(run,entries,*,expected_revision,environment,worker=None,family=None,validator=probe):
    run=Path(run)
    if run.stat().st_uid != os.geteuid():raise ValueError('Run this command as the research controller account.')
    entries=validate({'providers':entries}); config=read(run/'run.json')
    targets=[w for w in config['workers'] if (w['name']==worker if worker else w['kind']==family)]
    if not targets: raise ValueError('No matching researcher.')
    root=folder(run);root.mkdir(mode=0o700,exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077: raise ValueError('Provider storage must be private.')
    # Capture before inference, then compare under the same locks at commit.
    def capture(data):
        result={}
        for w in targets:
            name=w['name'];require_no_pending(run,name)
            state=read(run/name/'.runtime/state.json',{})
            if not meta(state).get('pool_sha256'): raise ValueError('Controller has not loaded provider-update support.')
            old=operator_pool(run,name,state,environment)
            if fingerprint(old)!=meta(state)['pool_sha256']: raise ValueError('Operator profile differs from the worker profile.')
            before=active_entries(run,name,state,old,data)
            pin=selection(state)
            if pin and (not 1 <= pin <= len(before) or before[pin-1]['id'] not in {e['id'] for e in entries}):
                raise ValueError('The pinned provider must remain in the update.')
            result[name]={'expected':snapshot(config,name,state),'before':before,
                          'before_revision':meta(state).get('revision',0),'after':entries}
        return result
    with lock(root/'update.lock'), ExitStack() as stack:
        for w in sorted(targets,key=lambda w:w['name']): stack.enter_context(control_lock(run/w['name']/'.runtime'))
        data=registry(run)
        if type(expected_revision) is not int or data['revision']!=expected_revision: raise ValueError('Provider settings changed; read status first.')
        captured=capture(data)
    try:
        for w in targets:
            effective=effective_worker(w,read(run/w['name']/'.runtime/state.json',{}))
            validator(copy.deepcopy(config),effective,copy.deepcopy(entries),environment)
    except Exception:
        raise ValueError('Provider validation failed; current providers were retained.') from None
    with lock(root/'update.lock'), ExitStack() as stack:
        for w in sorted(targets,key=lambda w:w['name']):stack.enter_context(control_lock(run/w['name']/'.runtime'))
        data=registry(run)
        if data['revision']!=expected_revision or read(run/'run.json')!=config or capture(data)!=captured:
            raise ValueError('Worker or provider settings changed during validation; retry from current status.')
        rev=data['revision']+1
        for name,record in captured.items():data['workers'][name]={**record,'revision':rev,'validated_at':time.time()}
        data['revision']=rev
        save(root/'providers.json',data)
    return {'revision':rev,'workers':[w['name'] for w in targets],'status':'validated; applies at each next client launch'}


def refresh(manager,name):
    if manager.probe or name in manager.active_sessions:return
    rt=manager.run/name/'.runtime'
    with control_lock(rt):
        state=manager.states[name];data=registry(manager.run);record=data['workers'].get(name)
        if not record or record['revision'] in (meta(state).get('revision'),meta(state).get('rejected_revision')):return
        try:
            require_no_pending(manager.run,name)
            if (snapshot(manager.config,name,state)!=record['expected'] or
                    snapshot(read(manager.run/'run.json'),name,state)!=record['expected']):
                raise ValueError('Worker settings changed.')
            old=pool_for(manager,name);new=raw(record['after'])
            if fingerprint(old)!=fingerprint(raw(record['before'])):raise ValueError('Provider identity changed.')
            updated=copy.deepcopy(state)
            control=updated.get('worker_control')
            if control:
                if control['pool_sha256']!=fingerprint(old) or control['session_id']!=state.get('token'):raise ValueError('Worker pin changed.')
                if control.get('provider_route'):
                    identifier=record['before'][control['provider_route']-1]['id']
                    control['provider_route']=[e['id'] for e in record['after']].index(identifier)+1
                control['pool_sha256']=fingerprint(new);control['revision']+=1
            adoption=updated.get('session_adoption',{})
            if 'provider_route' in adoption:
                from session_adoption import validate_route_selection,route_selection
                old_index=validate_route_selection(adoption['provider_route'],manager.config,old)
                if adoption['session_id']!=state.get('token'):raise ValueError('Adopted session changed.')
                identifier=record['before'][old_index]['id'];index=[e['id'] for e in record['after']].index(identifier)+1
                adoption['provider_route']=route_selection(manager.config,new,index)
            updated['providers']={'revision':record['revision'],'pool_sha256':fingerprint(new),
                                  'ids':[e['id'] for e in record['after']],'session_id':state.get('token'),'applied_at':time.time()}
            for key in ('primary_checked_at','primary_healthy','gemini_recovery_route','session_failure_streak'):
                updated.pop(key,None)
            updated.update(route_count=0,route_offset=-manager.worker(name).get('route_seed',0))
            manager.states[name]=updated
            try: manager.persist(name)  # Restart reloads the committed revision.
            except OSError:
                manager.states[name]=state
                raise
            if name in manager.routers:
                retired=getattr(manager,'_provider_retired_routers',[])
                retired.append(manager.routers.pop(name))
                manager._provider_retired_routers=retired
            manager._provider_pools[name]=new
            manager._provider_secrets.update(e['api_key'] for e in record['after'])
            manager._endpoint_resume_counts[name]=-1
        except (ValueError,KeyError,IndexError,TypeError):
            state['providers']={**meta(state),'rejected_revision':record['revision'],'last_error':'worker_or_pin_changed'}
            manager.persist(name)


def guard_session(manager,name):
    state=manager.states[name];saved=meta(state).get('session_id')
    if meta(state).get('revision') and saved and (state.get('token') != saved or state.get('session_unavailable')):
        state.update(token=saved,status='blocked',blocked_reason='provider_session_conflict')
        state['worker_control_hold']={'session_id':saved,'failure_category':'session_unavailable'}
        manager.persist(name)


def failure_control(manager,name):
    state=manager.states[name]
    data=meta(state)
    record=registry(manager.run)['workers'].get(name,{})
    sid=data.get('session_id') if data.get('revision') else record.get('expected',{}).get('session_id')
    if not sid:return None
    state.setdefault('providers',{})['session_id']=sid
    return state['providers']


def status(run):
    run=Path(run);data=registry(run);config=read(run/'run.json');workers=[]
    for w in config['workers']:
        state=read(run/w['name']/'.runtime/state.json',{});record=data['workers'].get(w['name'],{})
        workers.append({'worker':w['name'],'family':w['kind'],'model':effective_worker(w,state)['model'],
                        'active_revision':meta(state).get('revision',0),'requested_revision':record.get('revision',0),
                        'provider_ids':meta(state).get('ids',[]),'rejected_revision':meta(state).get('rejected_revision')})
    return {'revision':data['revision'],'workers':workers}


def cli(argv):
    from main import run_dir
    from common import safe_environment
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('run');p.add_argument('action',choices=['status','apply'])
    who=p.add_mutually_exclusive_group();who.add_argument('--worker');who.add_argument('--family',choices=['codex','claude','gemini'])
    p.add_argument('--file',type=Path);p.add_argument('--expect-revision',type=int);args=p.parse_args(argv)
    run=run_dir(args.run)
    if args.action=='status':result=status(run)
    else:
        if not args.file or args.expect_revision is None or not (args.worker or args.family):
            raise ValueError('Supply --file, --expect-revision and --worker or --family.')
        entries=validate(read_private(args.file))
        result=apply(run,entries,expected_revision=args.expect_revision,worker=args.worker,family=args.family,environment=safe_environment())
    print(json.dumps(result,indent=2))

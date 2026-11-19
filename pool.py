"""Optional GPU Pool client attachment; local GPU helpers stay intact."""
import json
import os
from pathlib import Path
import shlex
import subprocess
from common import ROOT, read, save

def profiles(path, config):
    raw=json.loads(Path(path).read_text())
    workers={w['name']:w for w in config['workers']}
    if not isinstance(raw,dict) or not raw:raise ValueError('Provide worker-to-profile JSON.')
    result={}
    for name,entry in raw.items():
        if name not in workers or not workers[name]['gpus']:raise ValueError('GPU Pool requires a GPU-enabled researcher: '+str(name))
        if not isinstance(entry,dict) or set(entry)!={'client_config','cli','workdir'}:raise ValueError('Each profile needs client_config, cli and workdir paths.')
        for k,v in entry.items():
            if not isinstance(v,str) or not Path(v).is_absolute():raise ValueError('GPU Pool paths must be absolute.')
        client=json.loads(Path(entry['client_config']).read_text())
        if not client.get('project') or not client.get('url') or not client.get('token_file'):raise ValueError('Client needs url, project and token_file.')
        if not Path(client['token_file']).is_absolute() or not Path(client['token_file']).is_file():raise ValueError('Prepared client token file missing.')
        if not os.access(entry['cli'],os.X_OK) or not Path(entry['workdir']).is_dir():raise ValueError('Prepare the GPU Pool CLI and shared working directory first.')
        result[name]=dict(entry)
    return result

def prepare(run,config,worker):
    work=Path(run)/worker['name'];helper=work/'gpu-pool'
    if worker['name'] not in config.get('gpu_pool',{}):return
    (work/"GPU_POOL.md").write_text(guidance(config,worker))
    helper.write_text('#!/bin/sh\nexec '+shlex.join([str(ROOT/'.venv/bin/python'),str(ROOT/'main.py'),'_pool',str(run),worker['name']])+' "$@"\n');helper.chmod(0o755)

def guidance(config,worker):
    entry=config.get('gpu_pool',{}).get(worker['name'])
    if not entry:return ''
    return ('\nCross-host GPU Pool is enabled. Your existing GPU allowance, experiment duration and storage budget still apply. For new portable experiments, put only the required code and inputs in '+entry['workdir']+' and use `./gpu-pool submit --job NAME -- {python} script.py {output}`; inspect with `./gpu-pool job ID`. The prepared working directory is shared by both hosts. The pool tries the home host first, then compatible free capacity, and shares your existing GPU quota. Retry an uncertain submission with the SAME job name. Outputs go to the returned directory; reuse shared inputs instead of copying datasets or checkpoints. The pool releases GPU reservations but does not delete files. Preserve needed results, then remove disposable staged code, logs and outputs yourself. Keep existing local experiments running; use ./gpu-run for commands that depend on local-only paths.\n')

def worker_cli(run,name,argv):
    config=read(Path(run)/'run.json');entry=config.get('gpu_pool',{}).get(name)
    if not entry:raise ValueError('GPU Pool is not enabled for this worker; use ./gpu-run.')
    return subprocess.call([entry['cli'],'--config',entry['client_config'],*argv])

def configure(run,path=None):
    from common import lock
    run=Path(run)
    with lock(run/'gpu-pool-config.lock'):
        config=read(run/'run.json');config['gpu_pool']=profiles(path,config) if path else {}
        save(run/'run.json',config)
        from common import initial_prompt
        for worker in config['workers']:
            prepare(run,config,worker)
            (run/worker['name']/'RESEARCH_INSTRUCTIONS.md').write_text(initial_prompt(config,worker,run/worker['name']))
            from capabilities import install_skills
            install_skills(run,config,worker)
            if worker['name'] not in config['gpu_pool']:
                (run/worker['name']/'GPU_POOL.md').unlink(missing_ok=True)
    return list(config['gpu_pool'])

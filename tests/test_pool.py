import json
import os
from pathlib import Path
import subprocess
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import save,read,researcher_instructions
from main import parser,build_config
import pool

def setup(tmp_path):
    args=parser().parse_args(['start','p','--codex','1','--claude','0','--gemini','0','--gpus','0'])
    c=build_config(args,{'category':'open_ended'});w=tmp_path/'codex_1';w.mkdir();save(tmp_path/'run.json',c)
    token=tmp_path/'token';token.write_text('private-test-token')
    client=tmp_path/'client.json';client.write_text(json.dumps(dict(url='http://localhost:1',project='p',token_file=str(token))))
    cli=tmp_path/'cli';cli.write_text(chr(10).join(['#!'+sys.executable, 'import sys,json;print(json.dumps(sys.argv[1:]));sys.exit(7)', '']));cli.chmod(0o755)
    profile={'codex_1':dict(cli=str(cli),client_config=str(client),workdir=str(tmp_path))}
    p=tmp_path/'profiles.json';p.write_text(json.dumps(profile));return c,p,profile

def test_default_and_cpu_workers_unchanged(tmp_path):
    c,p,profile=setup(tmp_path);assert 'gpu_pool' not in c
    assert pool.guidance(c,c['workers'][0])==''
    pool.prepare(tmp_path,c,c['workers'][0]);assert not (tmp_path/'codex_1/gpu-pool').exists()
    c['workers'][0]['gpus']=[]
    with pytest.raises(ValueError):pool.profiles(p,c)

def test_enable_disable_preserves_config_and_job(tmp_path,capfd):
    c,p,profile=setup(tmp_path);job=tmp_path/'codex_1/live-result';job.write_text('keep')
    assert pool.configure(tmp_path,p)==['codex_1']
    assert 'Cross-host GPU Pool is enabled' in (tmp_path/'codex_1/AGENTS.md').read_text()
    updated=read(tmp_path/'run.json');assert updated['gpu_pool']==profile
    assert 'private-test-token' not in (tmp_path/'run.json').read_text()
    argv=['submit','--job','same-name','--','python','literal $(x); space']
    assert pool.worker_cli(tmp_path,'codex_1',argv)==7
    assert json.loads(capfd.readouterr().out)==['--config',profile['codex_1']['client_config'],*argv]
    assert pool.configure(tmp_path)==[]
    assert not (tmp_path/'codex_1/GPU_POOL.md').exists()
    assert 'Cross-host GPU Pool is enabled' not in (tmp_path/'codex_1/AGENTS.md').read_text()
    with pytest.raises(ValueError):pool.worker_cli(tmp_path,'codex_1',argv)
    assert job.read_text()=='keep'
    assert {k:v for k,v in read(tmp_path/'run.json').items() if k!='gpu_pool'}==c

def test_startup_config_and_prompt(tmp_path):
    c,p,profile=setup(tmp_path)
    args=parser().parse_args(['start','p','--codex','1','--claude','0','--gemini','0','--gpus','0','--gpu-pool-profiles',str(p)])
    c=build_config(args,{'category':'open_ended'})
    prompt=researcher_instructions(c,c['workers'][0],tmp_path)
    assert 'Cross-host GPU Pool is enabled' in prompt
    assert './gpu-run' in prompt and 'SAME job name' in prompt
    assert 'Run every GPU experiment with' not in prompt
    assert 'does not delete files' in prompt
    profile['codex_1']['secret']='do-not-store';p.write_text(json.dumps(profile))
    with pytest.raises(ValueError):pool.profiles(p,c)

"""Native Antigravity event fixtures and subprocess recovery regressions."""
import json
import os
from pathlib import Path
import sys
import time
from dataclasses import replace
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from antigravity import AntigravityResume, prepare_profile, completed_turn
from common import save, read
from main import parser, build_config, prepare_workspaces
from recovery import ResearchManager, routes
from station.workers.job_manager import CliJobLaunchDecision


def config(tmp_path):
    args=parser().parse_args(['start','test_problem','--codex','0','--claude','0','--gemini','1'])
    c=build_config(args,{'category':'open_ended'})
    c['gpu_coord_file']=str(tmp_path/'gpu.json')
    c['live_input']=False  # Legacy single-turn fixtures.
    return c


def fake_binary(tmp_path):
    p=tmp_path/'agy'
    p.write_text('#!'+sys.executable+'\n'+'''import json,sys,pathlib,time
p=pathlib.Path.cwd()
n=int((p/'count').read_text())+1 if (p/'count').exists() else 1
(p/'count').write_text(str(n))
(p/('args'+str(n)+'.json')).write_text(json.dumps(sys.argv))
print(json.dumps({'event':'init','conversation_id':'retained-gemini'}),flush=True)
print(json.dumps({'event':'step_update','step_update':{'conversation_id':'retained-gemini','step_index':3,'step_type':'tool','state':'DONE','tool_name':'run_command','tool_info':{'output':'Hello'}}}),flush=True)
print(json.dumps({'event':'step_update','step_update':{'step_index':4,'step_type':'agent_response','state':'DONE','text_delta':'Hello from Gemini'}}),flush=True)
if (p/'wait').exists():time.sleep(60)
print(json.dumps({'event':'result','result':{'conversation_id':'retained-gemini','status':'ERROR' if n==1 else 'SUCCESS','response':'Hello' if n>1 else '', 'error':'provider unavailable' if n==1 else ''}}),flush=True)
''')
    p.chmod(0o755)
    return p


def environment(tmp_path):
    return dict(os.environ,ANTIGRAVITY_BIN_PATH=str(fake_binary(tmp_path)),
                RSTATION_WORKER_GEMINI_API_KEY='gemini-test-key',
                RSTATION_WORKER_GEMINI_BASE_URL='http://127.0.0.1:1')


def drain(m):
    end=time.monotonic()+5
    while m.active_sessions and time.monotonic()<end:m.poll_cli_jobs();time.sleep(.02)
    assert not m.active_sessions


def test_account_error_text_is_not_success_or_research_progress(tmp_path):
    c=config(tmp_path);e=environment(tmp_path)
    e.update(RSTATION_WORKER_GEMINI_BACKUP_API_KEY='backup',RSTATION_WORKER_GEMINI_BACKUP_BASE_URL='http://127.0.0.1:2')
    m=ResearchManager(tmp_path,c,e);name='gemini_1'
    notice='Request is blocked due to the account restrictions for Prohibited Use Policy (PUP) violations.'
    events=[{'step_update':{'step_type':'agent_response','step_index':4,'state':'ACTIVE','text_delta':notice[:7]}},
            {'step_update':{'step_type':'agent_response','step_index':4,'state':'DONE','text_delta':notice[7:]}},
            {'event':'result','result':{'status':'SUCCESS','response':notice}}]
    try:
        m.states[name].update(last_response=1,last_response_excerpt='Research answer',route_index=0)
        for event in events:m._record_gemini_event(name,event,100)
        assert not completed_turn(events)
        assert m.states[name]['last_response']==1
        assert m.states[name]['last_response_excerpt']=='Research answer'
        assert m.states[name]['gemini_recovery_route']==1
        assert completed_turn([{'event':'result','result':{'status':'SUCCESS','response':'The calibration result is ready.'}}])
    finally:m.close()


def test_native_error_with_zero_exit_resumes_same_session(tmp_path):
    c=config(tmp_path);e=environment(tmp_path)
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    m=ResearchManager(tmp_path,c,e);name='gemini_1'
    try:
        assert m.launch_cli_job(name);drain(m)
        assert m.states[name]['status']=='retrying'
        assert m.states[name]['token']=='retained-gemini'
        assert m.states[name]['last_tool_completed'] and m.states[name]['last_response']
        m.states[name]['next']=0
        assert m.launch_cli_job(name);drain(m)
        assert m.states[name]['status']=='continuing'
        args=read(tmp_path/name/'args2.json')
        assert args[args.index('--conversation')+1]=='retained-gemini'
        assert args[args.index('-p')+1]=='Continue your research.'
        assert 'gemini-test-key' not in json.dumps(args)
        assert (tmp_path/name/'.home/.gemini/antigravity-cli/settings.json').exists()
    finally:m.close()
    m=ResearchManager(tmp_path,c,e)
    try:
        assert m.states[name]['token']=='retained-gemini'
        m.states[name]['next']=0
        assert m.launch_cli_job(name);drain(m)
        assert read(tmp_path/name/'args3.json')[-1]=='retained-gemini'
    finally:m.close()


def test_interruption_and_pause_preserve_workspace(tmp_path):
    c=config(tmp_path);e=environment(tmp_path)
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    work=tmp_path/'gemini_1';(work/'wait').touch();(work/'important').write_text('science')
    m=ResearchManager(tmp_path,c,e)
    try:
        assert m.launch_cli_job('gemini_1')
        deadline=time.monotonic()+5
        while not m.states['gemini_1'].get('token') and time.monotonic()<deadline:m.poll_cli_jobs();time.sleep(.02)
        m.pause_worker('gemini_1')
        assert not m.active_sessions
        assert m.states['gemini_1']['status']=='paused'
        assert m.states['gemini_1']['token']=='retained-gemini'
        assert (work/'important').read_text()=='science'
    finally:m.close()


def test_dedicated_routes_rotate_without_station_fallback(tmp_path):
    with pytest.raises(ValueError):routes('gemini',{'GEMINI_API_KEY':'station','GOOGLE_GEMINI_BASE_URL':'http://station'})
    c=config(tmp_path);e=environment(tmp_path)
    e.update(RSTATION_WORKER_GEMINI_BACKUP_API_KEY='backup',RSTATION_WORKER_GEMINI_BACKUP_BASE_URL='http://127.0.0.1:2')
    m=ResearchManager(tmp_path,c,e)
    try:
        for count in [0,1,2,3,4]:
            d=CliJobLaunchDecision('resume',1,count,'saved',count)
            spec=m._build_cli_job_launch_spec('gemini_1','launch_test',d,m.states['gemini_1'])
            assert spec.env['GEMINI_API_KEY']==('gemini-test-key' if count//2%2==0 else 'backup')
            assert 'ANTHROPIC_API_KEY' not in spec.env and 'CODEX_API_KEY' not in spec.env
    finally:m.close()


def test_profile_tools_and_no_timeout(tmp_path):
    prepare_profile(tmp_path,False)
    settings=read(tmp_path/'.home/.gemini/antigravity-cli/settings.json')
    assert settings['customModelsConfig']['customModels']['gemini-api://gemini-4-carbon']=={
        'modelName':'gemini-api://gemini-4-carbon','maxTokens':500000}
    text=(tmp_path/'.home/.gemini/config/agents/rstation-researcher.md').read_text()
    assert 'search_web' in text and 'invoke_subagent' not in text and 'browser_subagent' not in text
    prepare_profile(tmp_path,True)
    text=(tmp_path/'.home/.gemini/config/agents/rstation-researcher.md').read_text()
    assert 'search_web' not in text and 'read_url_content' not in text
    b=AntigravityResume()
    launch=b.prepare_launch(executable='agy',run_dir=str(tmp_path),model_name='gemini-4-argon',prompt='test')
    assert launch.command[launch.command.index('--print-timeout')+1]=='0'
    assert launch.command[launch.command.index('--model')+1]=='gemini-api://gemini-4-argon'
    for status in ['ERROR','CANCELED','INTERRUPTED','WAITING','RUNNING','SUCCESS']:
        p=tmp_path/'stream';p.write_text(json.dumps({'event':'result','result':{'status':status,'conversation_id':'abc'}})+'\n')
        assert ResearchManager.completed(type('S',(),{'transcript_path':str(p)})())==(status=='SUCCESS')
        assert b.extract_resume_token(p)=='abc'


def test_context_budget_is_configurable_and_defaults_for_existing_runs(tmp_path):
    args=parser().parse_args(['start','p','--gemini','1','--gemini-context-tokens','400000'])
    assert build_config(args,{'category':'open_ended'})['gemini_context_tokens']==400000
    with pytest.raises(SystemExit):parser().parse_args(['start','p','--gemini-context-tokens','65536'])
    c=config(tmp_path);c.pop('gemini_context_tokens',None)
    m=ResearchManager(tmp_path,c,environment(tmp_path))
    try:
        d=CliJobLaunchDecision('resume',1,1,'saved',1)
        m._build_cli_job_launch_spec('gemini_1','test',d,m.states['gemini_1'])
        p=tmp_path/'gemini_1/.home/.gemini/antigravity-cli/settings.json'
        assert read(p)['customModelsConfig']['customModels']['gemini-api://gemini-4-carbon']['maxTokens']==500000
    finally:m.close()


def test_running_manager_reloads_context_budget_on_same_session_resume(tmp_path):
    c=config(tmp_path);c['gemini_context_tokens']=131072
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    m=ResearchManager(tmp_path,c,environment(tmp_path));name='gemini_1'
    settings=tmp_path/name/'.home/.gemini/antigravity-cli/settings.json'
    model='gemini-api://gemini-4-carbon'
    try:
        assert m.launch_cli_job(name);drain(m)
        assert m.states[name]['status']=='retrying'
        token=m.states[name]['token']
        assert token=='retained-gemini'
        assert read(settings)['customModelsConfig']['customModels'][model]['maxTokens']==131072
        save(tmp_path/'run.json',{**c,'gemini_context_tokens':100000})
        assert m.config['gemini_context_tokens']==131072
        m.states[name]['next']=0
        assert m.launch_cli_job(name);drain(m)
        assert m.states[name]['status']=='continuing'
        assert read(settings)['customModelsConfig']['customModels'][model]['maxTokens']==100000
        assert m.states[name]['token']==token
        args=read(tmp_path/name/'args2.json')
        assert args[args.index('--conversation')+1]==token
    finally:m.close()


@pytest.mark.parametrize('context_tokens',[0,65536])
def test_reloaded_context_budget_preserves_cli_output_reserve(tmp_path,context_tokens):
    c=config(tmp_path);save(tmp_path/'run.json',{**c,'gemini_context_tokens':context_tokens})
    m=ResearchManager(tmp_path,c,environment(tmp_path))
    try:
        d=CliJobLaunchDecision('resume',1,1,'saved',1)
        with pytest.raises(ValueError,match='65,536-token output reserve'):
            m._build_cli_job_launch_spec('gemini_1','test',d,m.states['gemini_1'])
    finally:m.close()


def test_native_context_refresh_rejects_output_reserve_without_changing_profile(tmp_path):
    from antigravity import refresh_context_budget
    work=tmp_path/'gemini_1';prepare_profile(work,context_tokens=131072)
    path=work/'.home/.gemini/antigravity-cli/settings.json';before=read(path)
    save(tmp_path/'run.json',{'gemini_context_tokens':65536})
    with pytest.raises(ValueError,match='65,536-token output reserve'):
        refresh_context_budget(work,'gemini-4-argon')
    assert read(path)==before


def test_gemini_has_no_delegation_mode(tmp_path):
    a=parser().parse_args(['start','p','--gemini','1','--subagents','1'])
    with pytest.raises(ValueError,match='without subagents'):build_config(a,{'category':'open_ended'})


def test_tool_only_model_turn_counts_as_response(tmp_path):
    m=ResearchManager(tmp_path,config(tmp_path),environment(tmp_path))
    try:
        event={'step_update':{'step_type':'agent_response','state':'DONE',
                              'usage':{'input_tokens':100,'output_tokens':20}}}
        m._record_gemini_event('gemini_1',event,123)
        assert m.states['gemini_1']['last_response']==123
        m._record_gemini_event('gemini_1',{'step_update':{'step_type':'agent_response','state':'ACTIVE'}},124)
        assert m.states['gemini_1']['last_response']==123
        m._record_gemini_event('gemini_1',{'step_update':{'step_type':'agent_response','state':'DONE',
            'usage':{'input_tokens':500000,'output_tokens':2000,'thinking_tokens':2000}}},125)
        assert m.states['gemini_1']['last_response']==123
    finally:m.close()


@pytest.mark.parametrize('fault',[None,'fresh_error','partial','tool_after','mismatch','no_user','nonzero_exit'])
def test_stale_native_error_requires_completed_current_response(tmp_path,fault):
    events=[{'step_update':{'step_type':'user_input','step_index':5,'state':'DONE'}},
            {'step_update':{'step_type':'agent_response','step_index':6,'state':'ACTIVE','text_delta':'Current '}},
            {'step_update':{'step_type':'agent_response','step_index':6,'state':'DONE','text_delta':'answer.'}}]
    if fault=='fresh_error':events.append({'step_update':{'step_type':'error_message','state':'DONE'}})
    if fault=='partial':events[-1]['step_update']['state']='ACTIVE'
    if fault=='tool_after':events.append({'step_update':{'step_type':'tool','state':'DONE'}})
    if fault=='no_user':events=events[1:]
    events.append({'event':'result','result':{'status':'ERROR','error':'old provider failure',
                   'response':'old answer' if fault=='mismatch' else 'Current answer.'}})
    p=tmp_path/'stream';p.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
    session=type('S',(),{'transcript_path':str(p),'process':type('P',(),{'returncode':3 if fault=='nonzero_exit' else 0})()})()
    m=ResearchManager(tmp_path,config(tmp_path),environment(tmp_path))
    try:assert m._cli_job_completion_ready('gemini_1',session)==(fault is None)
    finally:m.close()


@pytest.mark.parametrize('fault',[None,'fresh_error','partial','tool_after','new_user',
                                  'no_finish','empty_response','missing_result','nonzero_exit'])
def test_current_native_finish_supersedes_stale_error(tmp_path,fault):
    events=[{'step_update':{'step_type':'user_input','state':'DONE'}},
            {'step_update':{'step_type':'error_message','state':'DONE'}},
            {'step_update':{'step_type':'user_input','state':'DONE'}},
            {'step_update':{'step_type':'agent_response','step_index':7,'state':'DONE','text_delta':'{}\n'}},
            {'step_update':{'step_type':'finish','state':'DONE'}},
            {'step_update':{'step_type':'agent_response','step_index':9,'state':'DONE','text_delta':'Hello\n'}}]
    if fault=='fresh_error':events.append({'step_update':{'step_type':'error_message','state':'DONE'}})
    if fault=='partial':events[4]['step_update']['state']='ACTIVE'
    if fault=='no_finish':events.pop(4)
    if fault in {'tool_after','new_user'}:
        events.append({'step_update':{'step_type':'tool' if fault=='tool_after' else 'user_input','state':'DONE'}})
    if fault!='missing_result':
        events.append({'event':'result','result':{'status':'ERROR','error':'historical provider failure',
                       'response':'' if fault=='empty_response' else '{}\nHello\n'}})
    p=tmp_path/'stream';p.write_text('\n'.join(json.dumps(e) for e in events)+'\n')
    session=type('S',(),{'transcript_path':str(p),'process':type('P',(),{'returncode':3 if fault=='nonzero_exit' else 0})()})()
    m=ResearchManager(tmp_path,config(tmp_path),environment(tmp_path))
    try:assert m._cli_job_completion_ready('gemini_1',session)==(fault in {None, 'no_finish'})
    finally:m.close()


def test_current_native_error_rejects_success_and_old_result_does_not_complete_new_turn():
    user={'step_update':{'step_type':'user_input','state':'DONE'}}
    finish={'step_update':{'step_type':'finish','state':'DONE'}}
    error={'step_update':{'step_type':'error_message','state':'DONE'}}
    success={'event':'result','result':{'status':'SUCCESS','response':'Done.'}}
    assert not completed_turn([user,error,finish,success])
    assert not completed_turn([user,finish,success,user])


def test_platform_proxy_is_explicit_and_separate(tmp_path):
    from proxy import Router
    r=Router({'model.example'},'http://research:1','http://model:2', ['rstation.example'],
             host_proxies={'rstation.example':'http://platform:3'})
    assert r.upstream('rstation.example')=='http://platform:3'
    assert r.upstream('model.example')=='http://model:2'
    assert r.upstream('other.example')=='http://research:1'
    assert r.upstream('127.0.0.1') is None
    c=config(tmp_path);c['url']='https://rstation.example';c['platform_proxy']='http://platform:3'
    m=ResearchManager(tmp_path,c,environment(tmp_path))
    try:
        d=CliJobLaunchDecision('fresh',1,0,None,0)
        spec=m._build_cli_job_launch_spec('gemini_1','launch_test',d,m.states['gemini_1'])
        assert 'rstation.example' not in spec.env['NO_PROXY']
        assert m.routers['gemini_1'].upstream('rstation.example')=='http://platform:3'
    finally:m.close()

@pytest.mark.parametrize('model_proxy', [None, 'direct', 'http://model-proxy:8118'])
def test_direct_model_bypasses_router_with_platform_proxy(tmp_path,model_proxy):
    c=config(tmp_path);c.update(url='https://rstation.example',platform_proxy='http://platform:3',
                              research_proxy='http://research:4',model_api_proxy=model_proxy)
    e=environment(tmp_path);e['RSTATION_WORKER_GEMINI_BASE_URL']='https://model.example'
    m=ResearchManager(tmp_path,c,e)
    try:
        d=CliJobLaunchDecision('fresh',1,0,None,0)
        spec=m._build_cli_job_launch_spec('gemini_1','launch_test',d,m.states['gemini_1'])
        assert ('model.example' in spec.env['NO_PROXY'].split(','))==(model_proxy in (None, 'direct'))
        assert spec.env['no_proxy']==spec.env['NO_PROXY']
        assert 'rstation.example' not in spec.env['NO_PROXY']
        assert spec.env['HTTPS_PROXY']==m.routers['gemini_1'].url
        assert m.routers['gemini_1'].upstream('rstation.example')=='http://platform:3'
    finally:m.close()


@pytest.mark.parametrize('live_input',[False,True])
def test_stalled_gemini_rotates_and_resumes_same_conversation(tmp_path,monkeypatch,live_input):
    c=config(tmp_path);c.update(idle_seconds=1,live_input=live_input)
    e=environment(tmp_path)
    e.update(RSTATION_WORKER_GEMINI_BACKUP_API_KEY='backup',
             RSTATION_WORKER_GEMINI_BACKUP_BASE_URL='http://127.0.0.1:2')
    binary=Path(e['ANTIGRAVITY_BIN_PATH'])
    binary.write_text('#!'+sys.executable+'\n'+'''import os,sys,json,time,pathlib
p=pathlib.Path.cwd();n=int((p/'count').read_text())+1 if (p/'count').exists() else 1
(p/'count').write_text(str(n));(p/('args'+str(n)+'.json')).write_text(json.dumps(sys.argv))
(p/('route'+str(n))).write_text(os.environ['GOOGLE_GEMINI_BASE_URL'])
print(json.dumps({'event':'init','conversation_id':'retained-gemini'}),flush=True)
if '--input-format' in sys.argv:
 sys.stdin.readline()
 print(json.dumps({'event':'step_update','step_update':{'step_type':'user_input','state':'DONE'}}),flush=True)
print(json.dumps({'event':'step_update','step_update':{'step_index':1,'step_type':'tool','tool_name':'replace_file_content','state':'ACTIVE'}}),flush=True)
if n==1:time.sleep(60)
print(json.dumps({'event':'step_update','step_update':{'step_index':1,'step_type':'tool','state':'DONE'}}),flush=True)
print(json.dumps({'event':'result','result':{'conversation_id':'retained-gemini','status':'SUCCESS','response':'Recovered'}}),flush=True)
''')
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    work=tmp_path/'gemini_1';(work/'research.txt').write_text('preserved')
    m=ResearchManager(tmp_path,c,e)
    monkeypatch.setattr(m,'_probe_primary',lambda worker:False)
    try:
        assert m.launch_cli_job('gemini_1');drain(m)
        assert m.states['gemini_1']['status']=='retrying'
        assert m.states['gemini_1']['token']=='retained-gemini'
        assert m.states['gemini_1']['gemini_recovery_route']==1
        m.states['gemini_1']['next']=0
        assert m.launch_cli_job('gemini_1');drain(m)
        args=read(work/'args2.json')
        assert args[args.index('--conversation')+1]=='retained-gemini'
        assert (work/'route2').read_text()=='http://127.0.0.1:2'
        assert m.states['gemini_1'].get('last_tool_completed')
        assert (work/'research.txt').read_text()=='preserved'
    finally:m.close()


def test_gemini_live_tool_is_not_killed_by_inactivity(tmp_path):
    import subprocess
    from types import SimpleNamespace
    from common import pid_token
    c=config(tmp_path);m=ResearchManager(tmp_path,c,environment(tmp_path))
    child=subprocess.Popen([sys.executable,'-c','import subprocess;subprocess.run(["sleep","60"])'],start_new_session=True)
    runtime=tmp_path/'gemini_1/.runtime'
    try:
        save(runtime/'live_input.json',dict(pid=child.pid,token=pid_token(child.pid)))
        session=SimpleNamespace(process=child)
        deadline=time.monotonic()+3
        while not m._gemini_has_live_work('gemini_1',session) and time.monotonic()<deadline:time.sleep(.02)
        assert m._gemini_has_live_work('gemini_1',session)
        transcript=tmp_path/'silent.jsonl';transcript.touch()
        session.backend='gemini';session.transcript_path=str(transcript)
        session.last_transcript_size=0;session.last_transcript_growth_timestamp=time.time()-3600
        session.transcript_idle_timeout_triggered=False
        m.active_sessions['gemini_1']=session
        m._check_cli_job_transcript_idle_timeouts()
        assert not session.transcript_idle_timeout_triggered
        assert child.poll() is None
        assert time.time()-session.last_transcript_growth_timestamp<2
    finally:
        m.active_sessions.clear()
        import signal
        os.killpg(child.pid,signal.SIGTERM);child.wait();m.close()


def test_gemini_reserved_experiment_protects_silence_but_stale_record_does_not(tmp_path):
    from types import SimpleNamespace
    from common import pid_token
    c=config(tmp_path);m=ResearchManager(tmp_path,c,environment(tmp_path))
    try:
        # An unrelated native PID is not needed when a valid job owns the GPU.
        job=tmp_path/'gemini_1/.runtime/gpu-jobs/test.json'
        save(job,dict(pid=os.getpid(),token=pid_token(os.getpid())))
        assert m._gemini_has_live_work('gemini_1',SimpleNamespace(process=SimpleNamespace(pid=999999999)))
        save(job,dict(pid=os.getpid(),token='stale'))
        assert not m._gemini_has_live_work('gemini_1',SimpleNamespace(process=SimpleNamespace(pid=999999999)))
    finally:m.close()

@pytest.mark.parametrize('protection',[None,'gpu','pending_tool','child','paused'])
def test_gemini_retry_chatter_recovers_only_without_live_work(tmp_path,protection):
    from common import pid_token
    c=config(tmp_path);c['idle_seconds']=.3
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    e=environment(tmp_path)
    e.update(RSTATION_WORKER_GEMINI_BACKUP_API_KEY='backup',
             RSTATION_WORKER_GEMINI_BACKUP_BASE_URL='http://127.0.0.1:2')
    Path(e['ANTIGRAVITY_BIN_PATH']).write_text('#!'+sys.executable+'\n'+'''
import json,time,subprocess,sys
print(json.dumps({'event':'init','conversation_id':'retry-gemini'}),flush=True)
'''+('''
print(json.dumps({'step_update':{'step_type':'tool','step_index':7,'state':'ACTIVE'}}),flush=True)
''' if protection=='pending_tool' else '')+('''
subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
''' if protection=='child' else '')+'''
while True:
 print(json.dumps({'step_update':{'step_type':'error_message','state':'DONE'}}),flush=True)
 time.sleep(.025)
''')
    m=ResearchManager(tmp_path,c,e);name='gemini_1';work=tmp_path/name
    try:
        if protection=='gpu':
            save(work/'.runtime/gpu-jobs/active.json',dict(pid=os.getpid(),token=pid_token(os.getpid())))
        if protection=='paused':(work/'.runtime/PAUSE').touch()
        assert m.launch_cli_job(name)
        deadline=time.monotonic()+1.5
        while m.active_sessions and time.monotonic()<deadline:
            m.poll_cli_jobs();time.sleep(.02)
        assert m.states[name]['api_retries']>0
        assert m.states[name].get('last_response') is None
        if protection:
            assert m.active_sessions
            assert not m.states[name].get('gemini_recovery_route')
        else:
            assert not m.active_sessions
            assert m.states[name]['status']=='retrying'
            assert m.states[name]['token']=='retry-gemini'
            assert m.states[name]['gemini_recovery_route']==1
            # Bypass backoff only in this fixture; verify next real launch
            # selects the other provider without replacing the conversation.
            m.states[name]['next']=0
            assert m.launch_cli_job(name)
            assert m.states[name]['route_index']==1
            assert m.states[name]['token']=='retry-gemini'
            assert m.states[name]['api_retries']==0
    finally:m.close()


@pytest.mark.parametrize('event',[
    {'step_update':{'step_type':'agent_response','state':'ACTIVE','text_delta':'Research continues'}},
    {'step_update':{'step_type':'agent_response','state':'DONE','usage':{'output_tokens':2}}},
    {'step_update':{'step_type':'tool','state':'DONE','tool_info':{'output':'completed'}}},
    {'event':'result','result':{'status':'SUCCESS','response':'Finished'}}])
def test_gemini_real_progress_clears_retry_evidence(tmp_path,event):
    m=ResearchManager(tmp_path,config(tmp_path),environment(tmp_path));name='gemini_1'
    try:
        m._record_gemini_event(name,{'step_update':{'step_type':'error_message','state':'DONE'}},100)
        assert m.states[name]['api_retries']==1
        assert not m._research_progress(m.states[name])
        m._record_gemini_event(name,event,101)
        assert m.states[name]['api_retries']==0
        assert m._research_progress(m.states[name])==101
    finally:m.close()


def test_gemini_retry_watchdog_respects_threshold_and_failed_fence(tmp_path,monkeypatch):
    from types import SimpleNamespace
    m=ResearchManager(tmp_path,config(tmp_path),environment(tmp_path));name='gemini_1'
    calls=[]
    monkeypatch.setattr('recovery.time.time',lambda:2000)
    monkeypatch.setattr('controller_guard.reclaim_idle_clis',lambda *args:calls.append(args) or False)
    session=SimpleNamespace(backend='gemini',process=SimpleNamespace(poll=lambda:None),
                            transcript_idle_timeout_triggered=False)
    try:
        m.active_sessions[name]=session
        m.states[name].update(started=201,api_retries=20)
        m._check_provider_retry_stalls()
        assert not calls  # Still inside the unchanged 1800-second timeout.
        m.states[name]['started']=199
        m._check_provider_retry_stalls()
        assert len(calls)==1
        assert not session.transcript_idle_timeout_triggered
        assert 'gemini_recovery_route' not in m.states[name]
    finally:
        m.active_sessions.clear();m.close()


@pytest.mark.parametrize('status',['ERROR','SUCCESS'])
def test_final_progress_written_between_poll_and_exit_is_recorded(tmp_path,monkeypatch,status):
    c=config(tmp_path);e=environment(tmp_path)
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    work=tmp_path/'gemini_1'
    script=Path(e['ANTIGRAVITY_BIN_PATH'])
    script.write_text('#!'+sys.executable+'\n'+'''
import json,pathlib,time
while not pathlib.Path('release').exists():time.sleep(.01)
print(json.dumps({'event':'init','conversation_id':'last-moment'}),flush=True)
print(json.dumps({'step_update':{'step_type':'tool','state':'DONE','tool_info':{'output':'experiment complete'}}}),flush=True)
print(json.dumps({'step_update':{'step_type':'agent_response','state':'DONE','text_delta':'Result saved'}}),flush=True)
print(json.dumps({'event':'result','result':{'status':STATUS,'response':'Result saved'}}),flush=True)
'''.replace('STATUS',repr(status)))
    m=ResearchManager(tmp_path,c,e);name='gemini_1'
    try:
        assert m.launch_cli_job(name)
        session=m.active_sessions[name]
        def exit_after_initial_read():
            # Deterministically put all final events after _before_cli_job_poll
            # and before process.poll observes exit; a sleep in the fixture
            # would only hide the race.
            assert not m.states[name].get('last_response')
            (work/'release').touch()
            session.process.wait(timeout=5)
        monkeypatch.setattr(m,'_check_cli_job_transcript_idle_timeouts',exit_after_initial_read)
        m.poll_cli_jobs()
        assert not m.active_sessions
        state=m.states[name]
        assert state['last_tool_completed']>=state['started']
        assert state['last_response']>=state['started']
        assert state['last_response_excerpt']=='Result saved'
        assert state['token']=='last-moment'
        assert state['status']==('retrying' if status=='ERROR' else 'continuing')
        assert (work/'.runtime/console.log').read_text().count('experiment complete')==1
    finally:m.close()


@pytest.mark.parametrize('platform_proxy',[None,'http://platform:8120'])
def test_model_host_proxy_reloads_without_proxying_other_routes(tmp_path,platform_proxy):
    c=config(tmp_path);c.update(model_api_proxy='direct',platform_proxy=platform_proxy)
    e=environment(tmp_path)
    e.update(RSTATION_WORKER_GEMINI_BASE_URL='https://blocked.example',
             RSTATION_WORKER_GEMINI_BACKUP_API_KEY='backup;third',
             RSTATION_WORKER_GEMINI_BACKUP_BASE_URL='https://backup.example;https://third.example')
    save(tmp_path/'run.json',c)
    m=ResearchManager(tmp_path,c,e);name='gemini_1'
    try:
        d=CliJobLaunchDecision('resume',1,0,'retained',0)
        before=m._build_cli_job_launch_spec(name,'before',d,m.states[name])
        assert 'blocked.example' in before.env['NO_PROXY'].split(',')
        save(tmp_path/'run.json',{**c,'model_api_host_proxies':{'blocked.example':'http://127.0.0.1:8119'}})
        spec=m._build_cli_job_launch_spec(name,'mapped',d,m.states[name])
        router=m.routers[name]
        assert spec.env['HTTPS_PROXY']==router.url
        assert router.upstream('blocked.example')=='http://127.0.0.1:8119'
        for host in ['backup.example','third.example']:
            assert router.upstream(host) is None
            assert host in spec.env['NO_PROXY'].split(',')
        assert 'blocked.example' not in spec.env['NO_PROXY'].split(',')
        assert spec.env['NO_PROXY']==spec.env['no_proxy']
        from urllib.parse import urlsplit
        assert router.upstream(urlsplit(c['url']).hostname)==platform_proxy
        assert spec.env['GEMINI_API_KEY']==before.env['GEMINI_API_KEY']
        assert spec.env['GOOGLE_GEMINI_BASE_URL']==before.env['GOOGLE_GEMINI_BASE_URL']
        assert spec.model_name==before.model_name
        # Removing the map is also picked up on the next launch. Platform
        # routing, if configured, remains active in the existing router.
        save(tmp_path/'run.json',c)
        after=m._build_cli_job_launch_spec(name,'removed',d,m.states[name])
        assert 'blocked.example' in after.env['NO_PROXY'].split(',')
        if platform_proxy:
            assert router.upstream('blocked.example') is None
            assert router.upstream(urlsplit(c['url']).hostname)==platform_proxy
        else:assert 'HTTPS_PROXY' not in after.env
    finally:m.close()


def test_model_host_proxy_overrides_global_proxy_only_for_named_host(tmp_path):
    from proxy import Router
    router=Router({'named.example','other.example'},model_proxy='http://global:1',
                  host_proxies={'named.example':'http://specific:2'})
    assert router.upstream('named.example')=='http://specific:2'
    assert router.upstream('other.example')=='http://global:1'


@pytest.mark.parametrize('mapping',[
    ['not-a-map'],{'127.0.0.1':123},{'127.0.0.1':'socks5://localhost:8119'},
    {'127.0.0.1':'http://user:secret@localhost:8119'}])
def test_model_host_proxy_rejects_invalid_config(tmp_path,mapping):
    c=config(tmp_path);save(tmp_path/'run.json',{**c,'model_api_host_proxies':mapping})
    m=ResearchManager(tmp_path,c,environment(tmp_path))
    try:
        with pytest.raises(ValueError,match='Model host prox'):
            m._build_cli_job_launch_spec('gemini_1','test',CliJobLaunchDecision('fresh',1,0,None,0),m.states['gemini_1'])
    finally:m.close()


def test_primary_probe_uses_current_host_proxy_map(tmp_path):
    c=config(tmp_path);e=environment(tmp_path)
    e['RSTATION_WORKER_GEMINI_BASE_URL']='https://blocked.example'
    Path(e['ANTIGRAVITY_BIN_PATH']).write_text('#!'+sys.executable+'\n'+'''
import json,os
assert 'HTTPS_PROXY' in os.environ
assert 'blocked.example' not in os.environ['NO_PROXY'].split(',')
print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':'Hello'}}),flush=True)
''')
    m=ResearchManager(tmp_path,c,e)
    try:
        save(tmp_path/'run.json',{**c,'model_api_host_proxies':{'blocked.example':'http://127.0.0.1:8119'}})
        assert m._probe_primary(m.worker('gemini_1'))
        assert not m.active_sessions
    finally:m.close()


@pytest.mark.parametrize('fault', [None, 'fresh_error', 'trailing_tool', 'partial',
                                  'unrelated_result', 'reversed_result', 'new_user',
                                  'duplicate_step', 'reused_step', 'missing_step', 'no_user'])
def test_stale_error_matches_distinct_completed_replies(fault):
    def step(kind, **fields):
        return {'step_update': {'step_type': kind, 'state': 'DONE', **fields}}
    events = [step('user_input'),
              step('agent_response', step_index=3, state='ACTIVE', text_delta='Run '),
              step('agent_response', step_index=3, text_delta='finished.'),
              step('tool'),
              step('agent_response', step_index=5, text_delta='Measured 12 units.')]
    response = 'Run finished.\nMeasured 12 units.'
    if fault == 'fresh_error': events.append(step('error_message'))
    if fault == 'trailing_tool': events.append(step('tool'))
    if fault == 'partial': events[-1]['step_update']['state'] = 'ACTIVE'
    if fault == 'unrelated_result': response = 'An unrelated answer.'
    if fault == 'reversed_result': response = 'Measured 12 units.\nRun finished.'
    if fault == 'new_user': events.append(step('user_input'))
    if fault == 'duplicate_step':
        events.append(step('agent_response', step_index=5, text_delta=''))
        response += '\nMeasured 12 units.'
    if fault == 'reused_step':
        events.append(step('agent_response', step_index=3, text_delta='Another reply.'))
        response += '\nAnother reply.'
    if fault == 'missing_step':
        for e in events: e['step_update'].pop('step_index', None)
    if fault == 'no_user': events.pop(0)
    events.append({'event': 'result', 'result': {
        'status': 'ERROR', 'error': 'earlier stream error', 'response': response}})
    assert completed_turn(events) == (fault is None)


def test_joined_replies_are_reset_at_new_user_boundary():
    def reply(index, text):
        return {'step_update': {'step_type': 'agent_response', 'state': 'DONE',
                                'step_index': index, 'text_delta': text}}
    user = {'step_update': {'step_type': 'user_input', 'state': 'DONE'}}
    events = [user, reply(1, 'Old reply.'), user,
              reply(2, 'First current reply.'), reply(3, 'Second current reply.')]
    terminal = {'event': 'result', 'result': {'status': 'ERROR',
                'response': 'First current reply.\nSecond current reply.'}}
    assert completed_turn(events + [terminal])
    terminal['result']['response'] = 'Old reply.\nFirst current reply.\nSecond current reply.'
    assert not completed_turn(events + [terminal])

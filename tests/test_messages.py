import json
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import sys
import time
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import save,read
from main import main,parser,build_config
from messages import Messages,prompt
from live_cli import LiveCLI,native_command


def add_messages(work,offset):
    for i in range(10):Messages(work).add(f'Instruction {offset+i}')


def test_concurrent_senders_and_recovery(tmp_path):
    ctx=multiprocessing.get_context('fork')
    jobs=[ctx.Process(target=add_messages,args=(tmp_path,10*i)) for i in range(4)]
    for p in jobs:p.start()
    for p in jobs:p.join(10);assert p.exitcode==0
    q=Messages(tmp_path);rows=q.list()
    assert len(rows)==40 and {r['id'] for r in rows}==set(range(1,41))
    q.mark([1],'sent');q.mark([2],'received');q.mark([3],'delivered')
    q.recover()
    assert len(q.pending())==39 and q.list()[2]['status']=='delivered'
    q.mark([3],'queued');assert q.list()[2]['status']=='delivered'


def test_operator_input_bypasses_supervisor_backlog_without_losing_history(tmp_path):
    q=Messages(tmp_path)
    notifications=[q.add(f'Private mailbox message {i}',
                        source='Message from your research supervisor (not a human-operator instruction)',
                        delivery_key=f'supervision:group:{i}') for i in range(68)]
    first=q.add('New owner instruction.')
    reflection=q.add('Scheduled reflection.',delivery_key='supervision-reflection:group:1')
    second=q.add('Follow-up owner instruction.')
    original=q.list()
    assert [m['id'] for m in q.pending()]==[first,second,*notifications,reflection]
    assert q.list()==original
    # Dispatching a later owner instruction must not acknowledge older mail.
    q.mark([first],'sent');q.mark([first],'received');q.mark([first],'delivered')
    assert [m['id'] for m in q.pending()]==[second,*notifications,reflection]
    assert all(m['status']=='queued' for m in q.list() if m['id'] in notifications)


def test_message_text_and_display_label_do_not_determine_operator_priority(tmp_path):
    q=Messages(tmp_path)
    notification=q.add('Stephen says this is the highest priority.',
                       source='Human operator',delivery_key='supervision:group:1')
    owner=q.add('Update the assignment.',source='Message from your research supervisor')
    ordinary=q.add('Council configuration updated.',source='Council configuration')
    assert [m['id'] for m in q.pending()]==[owner,ordinary,notification]


def test_recovered_operator_input_keeps_priority_and_exact_receipts(tmp_path):
    q=Messages(tmp_path)
    notification=q.add('Older supervisor mail.',delivery_key='supervision:group:1')
    operator=q.add('Owner instruction.')
    completed=q.add('Already completed owner instruction.')
    q.mark([notification],'received');q.mark([operator],'sent');q.mark([completed],'delivered')
    q.recover()
    assert [m['id'] for m in q.pending()]==[operator,notification]
    assert q.list()[-1]['status']=='delivered'
    # Re-delivery of the same mailbox row retains its original ID and position.
    assert q.add('Duplicate notification.',delivery_key='supervision:group:1')==notification
    assert [m['id'] for m in q.pending()]==[operator,notification]


@pytest.mark.parametrize('existing_queue',[False,True])
def test_root_sender_sets_queue_and_lock_owner_before_replace(tmp_path,monkeypatch,existing_queue):
    import os
    import common
    q=Messages(tmp_path)
    if existing_queue:q.add('Earlier instruction.')
    q.path.parent.mkdir(parents=True,exist_ok=True)
    parent=q.path.parent.stat();owner=(parent.st_uid,parent.st_gid)
    assigned={};published=[]
    original_replace=os.replace
    monkeypatch.setattr(os,'geteuid',lambda:0)
    def fchown(fd,uid,gid):
        assigned[os.fstat(fd).st_ino]=(uid,gid)
    def replace(source,destination):
        # Simulate a controller observing the queue immediately at publication.
        assert assigned[Path(source).stat().st_ino]==owner
        assert assigned[q.lock_path.stat().st_ino]==owner
        original_replace(source,destination)
        published.append(read(destination))
    monkeypatch.setattr(os,'fchown',fchown)
    monkeypatch.setattr(common.os,'replace',replace)
    q.add('New instruction.')
    assert published[0]['messages'][-1]['text']=='New instruction.'
    assert len(q.list())==1+existing_queue
    assert not list(q.path.parent.glob('.write-*'))


def test_nonroot_sender_does_not_change_ownership(tmp_path,monkeypatch):
    import os
    monkeypatch.setattr(os,'geteuid',lambda:1000)
    def unexpected_chown(*args):raise AssertionError('Non-root sender must not change ownership')
    monkeypatch.setattr(os,'fchown',unexpected_chown)
    monkeypatch.setattr(os,'chown',unexpected_chown)
    q=Messages(tmp_path);q.add('Ordinary instruction.')
    assert q.list()[0]['status']=='queued'


def test_send_all_without_credentials_and_worker_validation(tmp_path,monkeypatch,capsys):
    monkeypatch.setenv('RSTATION_AGENT_DATA',str(tmp_path))
    args=parser().parse_args(['start','p','--name','trial','--codex','1','--claude','1','--gemini','1'])
    config=build_config(args,{'category':'open_ended'});save(tmp_path/'trial/run.json',config)
    f=tmp_path/'instructions.txt';f.write_text('Study the laser cycle.\nKeep the experiment running.')
    main(['send','trial','all','--file',str(f)])
    for w in config['workers']:
        assert Messages(tmp_path/'trial'/w['name']).list()[0]['text']==f.read_text()
    main(['messages','trial']);out=capsys.readouterr().out
    assert 'gemini_1' in out and 'queued' in out
    for argv in [['send','trial','../escape','bad'],['send','trial','all',''],['send','trial','all','a','--file',str(f)]]:
        with pytest.raises(SystemExit):main(argv)
    for w in config['workers']:assert len(Messages(tmp_path/'trial'/w['name']).list())==1


def worker(tmp_path,kind):
    return LiveCLI(SimpleNamespace(workspace=str(tmp_path),kind=kind,model='test',prompt='initial',conversation=''))


@pytest.mark.parametrize('kind,conversation,queued',[
    ('gemini','saved',True),('gemini','',True),('gemini','saved',False),
    ('claude','saved',True),('claude','',True),('claude','saved',False)])
def test_native_startup_includes_mail_without_losing_fresh_assignment(tmp_path,kind,conversation,queued):
    script=tmp_path/'native.py';capture=tmp_path/'first-input.json'
    script.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).write_text(sys.stdin.readline())\n")
    w=worker(tmp_path,kind);w.args.conversation=conversation
    w.args.prompt='Continue.' if conversation else 'Initial assignment.'
    w.args.command=[sys.executable,str(script),str(capture),'--model','test']
    w.args.command+=['-p',w.args.prompt] if kind=='gemini' else [w.args.prompt]
    number=w.messages.add('Resume with the queued meeting.') if queued else None
    assert w.run()==1  # The fixture intentionally exits after reading one input.
    first=read(capture)['message']['content']
    if queued:
        expected=prompt(w.messages.list()[0])
        assert first==(expected if conversation else w.args.prompt+'\n\n'+expected)
        assert number in w.sent
    else:
        assert first==w.args.prompt


@pytest.mark.parametrize('kind,configured',[('gemini',True),('gemini',False),('claude',True)])
def test_native_child_refreshes_only_configured_gemini_model_budget(tmp_path,kind,configured):
    from antigravity import prepare_profile
    work=tmp_path/'worker';w=worker(work,kind)
    w.args.model='gemini-test';w.args.conversation='saved-conversation';w.args.prompt='Continue.'
    prepare_profile(work,model_name=w.args.model,context_tokens=131072)
    profile=work/'.home/.gemini/antigravity-cli/settings.json'
    settings=read(profile);settings['telemetryEnabled']=False
    models=settings['customModelsConfig']['customModels']
    models['gemini-api://another-model']={'modelName':'gemini-api://another-model','maxTokens':222222}
    save(profile,settings)
    if configured:save(tmp_path/'run.json',{'gemini_context_tokens':300000})
    script=tmp_path/'native.py';capture=tmp_path/'native-start.json'
    script.write_text("import json,pathlib,sys\nsettings=json.loads(pathlib.Path(sys.argv[2]).read_text())\n"
                      "pathlib.Path(sys.argv[1]).write_text(json.dumps({'settings':settings,'args':sys.argv}))\nsys.stdin.readline()\n")
    w.args.command=[sys.executable,str(script),str(capture),str(profile),'--model',w.args.model]
    w.args.command+=['-p',w.args.prompt,'--conversation',w.args.conversation] if kind=='gemini' else [w.args.prompt,'--resume',w.args.conversation]
    assert w.run()==1
    if kind=='gemini' and configured:models['gemini-api://gemini-test']['maxTokens']=300000
    observed=read(capture)
    assert observed['settings']==settings
    flag='--conversation' if kind=='gemini' else '--resume'
    assert observed['args'][observed['args'].index(flag)+1]=='saved-conversation'


def test_native_input_envelopes_and_nonblocking_large_message(tmp_path):
    for kind in ['claude','gemini']:
        w=worker(tmp_path/kind,kind)
        text='long operator message '*20000
        n=w.messages.add(text);w.send(prompt(w.messages.list()[0]),n)
        packet=json.loads(w.output)
        assert packet['type' if kind=='claude' else 'event']=='user'
        assert packet['message']['content'].endswith(text.strip())
        assert w.messages.list()[0]['status']=='sent'
        # Merely completing an earlier turn must not acknowledge a queued input.
        w.finish();assert w.messages.list()[0]['status']=='sent'
        w.accepted(n);w.finish();assert w.messages.list()[0]['status']=='delivered'


def test_closed_native_input_drains_output_and_preserves_unacknowledged_mail(tmp_path,monkeypatch):
    import live_cli
    script=tmp_path/'native.py'
    script.write_text('import os,json\nos.close(0)\nprint(json.dumps({"event":"result","result":{"status":"SUCCESS","response":"Finished prior turn."}}))\n')
    w=worker(tmp_path,'gemini');w.args.conversation='saved'
    w.args.command=[sys.executable,str(script),'--model','test','-p','Continue.']
    w.messages.add('Council reply available.')
    def closed(*args):raise BrokenPipeError()
    monkeypatch.setattr(live_cli.os,'write',closed)
    assert w.run()==1
    assert w.messages.list()[0]['status']=='sent'
    w.messages.recover()
    assert w.messages.list()[0]['status']=='queued'


def test_codex_steering_race_requeues_without_losing_message(tmp_path):
    w=worker(tmp_path,'codex');w.thread='thread';w.turn='old-turn'
    n=w.messages.add('Prioritize the main task.');w.send(prompt(w.messages.list()[0]),n)
    packet=json.loads(w.output)
    assert packet['method']=='turn/steer' and packet['params']['expectedTurnId']=='old-turn'
    w.codex_event({'id':packet['id'],'error':{'message':'No active turn'}})
    assert w.messages.list()[0]['status']=='queued' and not w.busy
    w.output.clear();w.send(prompt(w.messages.list()[0]),n)
    packet=json.loads(w.output);assert packet['method']=='turn/start'
    w.codex_event({'id':packet['id'],'result':{'turn':{'id':'new-turn'}}})
    assert w.messages.list()[0]['status']=='received'
    w.codex_event({'method':'turn/completed','params':{'turn':{'id':'new-turn','status':'completed'}}})
    assert w.messages.list()[0]['status']=='delivered'


def test_codex_reloads_main_researcher_model_at_same_session_turn_boundary(tmp_path):
    work=tmp_path/'codex_1';work.mkdir()
    save(tmp_path/'run.json',{'workers':[{'name':'codex_1','kind':'codex','model':'gpt-6-astra'}]})
    w=worker(work,'codex')
    assert w.codex_model()=='gpt-6-astra'
    w.thread='saved-thread'
    w.send('first turn')
    first=json.loads(w.output);assert first['method']=='turn/start'
    assert first['params']['threadId']=='saved-thread'
    assert first['params']['model']=='gpt-6-astra' and first['params']['effort']=='max'
    w.codex_event({'id':first['id'],'result':{'turn':{'id':'turn-1'}}})
    w.codex_event({'method':'turn/completed','params':{'turn':{'id':'turn-1','status':'completed'}}})
    save(tmp_path/'run.json',{'workers':[{'name':'codex_1','kind':'codex','model':'gpt-6.2-astra'}]})
    w.output.clear();w.send('next turn')
    second=json.loads(w.output);assert second['method']=='turn/start'
    assert second['params']['threadId']=='saved-thread'
    assert second['params']['model']=='gpt-6.2-astra' and second['params']['effort']=='max'
    assert not second['params'].get('parentTurnId')


@pytest.mark.parametrize('alias_root',['earth_moon','septuple'])
def test_codex_model_lookup_uses_registered_physical_workspace(tmp_path,monkeypatch,alias_root):
    run=tmp_path/'agent_data'/(alias_root+'_public')
    logical_name='codex_2' if alias_root=='earth_moon' else 'codex_1'
    physical_name='codex_2';work=tmp_path/'tmp'/alias_root/physical_name
    work.mkdir(parents=True);run.mkdir(parents=True)
    (run/logical_name).symlink_to(work,target_is_directory=True)
    save(run/'run.json',{'workers':[{'name':logical_name,'kind':'codex','model':'gpt-6.2-astra'}]})
    monkeypatch.setenv('RSTATION_AGENT_DATA',str(tmp_path/'agent_data'))
    w=worker(work,'codex')
    w.thread='saved-thread';w.send('Continue researching.')
    request=json.loads(w.output)
    assert request['method']=='turn/start'
    assert request['params']['threadId']=='saved-thread'
    assert request['params']['model']=='gpt-6.2-astra'
    assert request['params']['effort']=='max'
    assert w.run_worker['name']==logical_name


def test_codex_model_lookup_fails_closed_when_workspace_is_shared(tmp_path,monkeypatch):
    data=tmp_path/'agent_data';work=tmp_path/'tmp'/'shared'/'codex_2';work.mkdir(parents=True)
    for run_name,worker_name,model in [('first','codex_1','gpt-6.2-astra'),('second','codex_2','gpt-6-astra')]:
        run=data/run_name;run.mkdir(parents=True)
        (run/worker_name).symlink_to(work,target_is_directory=True)
        save(run/'run.json',{'workers':[{'name':worker_name,'kind':'codex','model':model}]})
    monkeypatch.setenv('RSTATION_AGENT_DATA',str(data))
    w=worker(work,'codex');w.args.model='launch-model'
    assert w.codex_model()=='launch-model'
    assert w.run_worker is None


def test_codex_model_lookup_skips_unreadable_unrelated_roster(tmp_path,monkeypatch):
    data=tmp_path/'agent_data';run=data/'active';work=tmp_path/'tmp'/'alias'/'codex_2'
    run.mkdir(parents=True);work.mkdir(parents=True)
    (run/'codex_1').symlink_to(work,target_is_directory=True)
    save(run/'run.json',{'workers':[{'name':'codex_1','kind':'codex','model':'gpt-6.2-astra'}]})
    restricted=data/'restricted';restricted.mkdir()
    save(restricted/'run.json',{'workers':[]})
    broken=data/'broken';broken.mkdir()
    (broken/'codex_9').symlink_to(broken/'codex_9')
    save(broken/'run.json',{'workers':[{'name':'codex_9','kind':'codex','model':'wrong'}]})
    monkeypatch.setenv('RSTATION_AGENT_DATA',str(data))
    original=Path.read_text
    def read_text(path,*args,**kwargs):
        if path==restricted/'run.json':
            raise PermissionError('restricted roster')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',read_text)
    w=worker(work,'codex');w.thread='saved-thread';w.send('Continue researching.')
    request=json.loads(w.output)
    assert request['params']['model']=='gpt-6.2-astra'
    assert w.run_worker['name']=='codex_1'


def test_codex_resume_uses_current_run_model_and_preserves_saved_thread(tmp_path):
    work=tmp_path/'codex_1';work.mkdir()
    save(tmp_path/'run.json',{'workers':[{'name':'codex_1','kind':'codex','model':'gpt-6.2-astra'}]})
    w=worker(work,'codex');w.args.conversation='saved-thread';w.args.prompt='Continue your research.'
    w.requests[1]=('initialize',None)
    w.codex_event({'id':1,'result':{}})
    resume=json.loads(w.output.splitlines()[-1])
    assert resume['method']=='thread/resume'
    assert resume['params']['threadId']=='saved-thread'
    assert resume['params']['model']=='gpt-6.2-astra'
    assert resume['params']['cwd']==str(work)


def test_codex_steering_does_not_change_model_mid_turn(tmp_path):
    work=tmp_path/'codex_1';work.mkdir()
    save(tmp_path/'run.json',{'workers':[{'name':'codex_1','kind':'codex','model':'gpt-6.2-astra'}]})
    w=worker(work,'codex');w.thread='saved-thread';w.turn='active-turn'
    w.send('steer current turn')
    request=json.loads(w.output)
    assert request['method']=='turn/steer'
    assert 'model' not in request['params'] and 'effort' not in request['params']


@pytest.mark.parametrize('kind',['codex','gemini'])
@pytest.mark.parametrize('delivery_key',[None,'supervision:group'])
def test_followup_does_not_wait_for_research_turn(tmp_path,kind,delivery_key):
    w=worker(tmp_path,kind);w.thread='thread';w.turn='research-turn'
    first=w.messages.add('Supervisor introduction.',delivery_key=delivery_key and delivery_key+':1')
    w.send(prompt(w.messages.list()[0]),first)
    second=w.messages.add('Proposal revision.',delivery_key=delivery_key and delivery_key+':2')
    followup=w.messages.pending()[0]
    assert not w.can_send_message(followup)
    w.accepted(first)
    assert w.can_send_message(followup)
    # Operator instructions must not queue behind an acknowledged research turn.
    assert w.can_send_message({'text':'Operator guidance.'})
    w.output.clear();w.send(prompt(followup),second)
    assert w.messages.list()[0]['status']=='received'
    assert w.messages.list()[1]['status']=='sent'
    third=w.messages.add('Next meeting.',delivery_key=delivery_key and delivery_key+':3')
    assert not w.can_send_message(w.messages.pending()[0])
    # An earlier turn's completion does not claim the unreceived second input.
    w.finish()
    assert w.messages.list()[0]['status']=='delivered'
    assert w.messages.list()[1]['status']=='sent'
    assert not w.can_send_message(w.messages.pending()[0])
    w.accepted(second);w.finish()
    assert w.messages.list()[1]['status']=='delivered'
    assert w.can_send_message(w.messages.pending()[0])
    assert third==3


def test_claude_queues_followup_until_native_result(tmp_path):
    w = worker(tmp_path, 'claude')
    first = w.messages.add('Current research instruction.')
    w.send_pending(w.messages.pending())
    w.stream_event({'type': 'user', 'message': {
        'content': prompt(w.messages.list()[0])}})
    second = w.messages.add('Next research instruction.')
    assert w.messages.list()[0]['status'] == 'received'
    assert not w.can_send_message(w.messages.pending()[0])
    assert w.messages.list()[1]['status'] == 'queued'
    w.stream_event({'type': 'result', 'subtype': 'success', 'is_error': False})
    assert w.messages.list()[0]['status'] == 'delivered'
    assert w.can_send_message(w.messages.pending()[0])
    w.send_pending(w.messages.pending())
    assert w.messages.list()[1]['status'] == 'sent'
    w.stream_event({'type': 'user', 'message': {
        'content': prompt(w.messages.list()[1])}})
    w.stream_event({'type': 'result', 'subtype': 'success', 'is_error': False})
    assert w.messages.list()[1]['status'] == 'delivered'
    assert not w.sent and not w.busy
    assert (first, second) == (1, 2)


def test_failed_turn_does_not_claim_delivery(tmp_path):
    w=worker(tmp_path,'gemini');n=w.messages.add('Investigate uncertainty.')
    w.send(prompt(w.messages.list()[0]),n)
    w.stream_event({'event':'step_update','step_update':{'step_type':'user_input','state':'DONE'}})
    assert w.messages.list()[0]['status']=='received'
    with pytest.raises(RuntimeError):w.stream_event({'event':'result','result':{'status':'ERROR'}})
    w.messages.recover();assert w.messages.list()[0]['status']=='queued'


def test_native_command_preserves_provider_and_isolation():
    source=['codex','--config','model_provider="alt"','-c','web_search="disabled"','-c','agents.enabled=false','--cd','/tmp/w','--sandbox','danger-full-access','exec','resume','--model','gpt-6-astra','thread','-']
    result=native_command('codex',source)
    assert result==['codex','--config','model_provider="alt"','-c','web_search="disabled"','-c','agents.enabled=false','app-server','--listen','stdio://']
    source=['claude','--print','--model','opus','PROMPT','--resume','thread','--disallowedTools','WebSearch','WebFetch']
    result=native_command('claude',source)
    assert 'PROMPT' not in result and '--resume' in result and 'thread' in result
    assert '--disallowedTools' in result and 'WebFetch' in result and '--replay-user-messages' in result


def test_keep_experiments_recovery_option():
    args=parser().parse_args(['resume','trial','--keep-experiments'])
    assert args.keep_experiments


def test_gemini_stale_error_result_with_completed_current_response(tmp_path):
    w=worker(tmp_path,'gemini');n=w.messages.add('New instruction.')
    w.send(prompt(w.messages.list()[0]),n)
    w.stream_event({'event':'step_update','step_update':{'step_type':'user_input','state':'DONE'}})
    w.stream_event({'event':'step_update','step_update':{'step_type':'agent_response','state':'DONE','step_index':2,'text_delta':'Received.'}})
    w.stream_event({'event':'result','result':{'status':'ERROR','response':'Received.'}})
    assert w.messages.list()[0]['status']=='delivered'


def test_controller_recovery_keeps_live_experiment_until_explicit_stop(tmp_path,monkeypatch):
    import importlib,subprocess,os
    from common import pid_token
    app=importlib.import_module('main')
    args=parser().parse_args(['start','p','--codex','1','--claude','0','--gemini','0'])
    config=build_config(args,{'category':'open_ended'})
    save(tmp_path/'run.json',config)
    helper=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    experiment=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],start_new_session=True)
    save(tmp_path/'codex_1/.runtime/gpu-jobs/live.json',dict(
        helper_pid=helper.pid,helper_token=pid_token(helper.pid),pid=experiment.pid,token=pid_token(experiment.pid)))
    observed=[]
    class Manager:
        active_sessions={}
        states={'codex_1':{'status':'paused'}}
        def __init__(self,*a):
            observed.append((helper.poll(),experiment.poll()))
        def poll_cli_jobs(self):(tmp_path/'STOP').touch()
        def check_storage(self):pass
        def log(self,*args):pass
        def close(self):pass
        def launch_cli_job(self,*a):pass
    monkeypatch.setattr(app,'ResearchManager',Manager)
    monkeypatch.setattr(app.signal,'signal',lambda *a:None)
    import common
    monkeypatch.setattr(common,'fetch',lambda *a:{'category':'open_ended','status':'open'})
    try:
        app.controller(tmp_path,keep_experiments=True)
        assert observed==[(None,None)]
        # The subsequent explicit stop still terminates the experiment.
        experiment.wait(timeout=5);helper.wait(timeout=5)
        assert experiment.returncode is not None
    finally:
        for process in [helper,experiment]:
            if process.poll() is None:os.killpg(process.pid,9)
            process.wait()


@pytest.mark.parametrize('kind',['claude','gemini'])
def test_pending_batch_acknowledges_exact_messages(tmp_path,kind):
    w=worker(tmp_path,kind)
    first=w.messages.add('Earlier operational guidance.')
    second=w.messages.add('New scientific priorities from Stephen.')
    w.send_pending(w.messages.pending())
    packet=json.loads(w.output)
    assert 'Earlier operational guidance.' in packet['message']['content']
    assert 'New scientific priorities from Stephen.' in packet['message']['content']
    assert [m['status'] for m in w.messages.list()]==['sent','sent']
    third=w.messages.add('Not part of the current native input.')
    assert not w.can_send_message(w.messages.pending()[0])
    if kind=='gemini':
        w.stream_event({'event':'step_update','step_update':{'step_type':'user_input','state':'DONE'}})
    else:
        # Claude echoes the entire user frame, not individual batch members.
        w.stream_event({'type':'user','message':{'content':packet['message']['content']}})
    assert [m['status'] for m in w.messages.list()]==['received','received','queued']
    w.finish()
    assert [m['status'] for m in w.messages.list()]==['delivered','delivered','queued']
    assert w.can_send_message(w.messages.pending()[0])
    assert (first,second,third)==(1,2,3)


@pytest.mark.parametrize('kind',['claude','gemini'])
def test_pending_batch_bounds_input_without_truncating(tmp_path,kind):
    w=worker(tmp_path,kind)
    first=w.messages.add('a'*40000);second=w.messages.add('b'*40000)
    w.send_pending(w.messages.pending())
    assert [m['status'] for m in w.messages.list()]==['sent','queued']
    assert json.loads(w.output)['message']['content'].endswith('a'*40000)


def test_gemini_resume_sends_all_pending_priorities_first(tmp_path):
    script=tmp_path/'native.py';capture=tmp_path/'input.json'
    script.write_text("import pathlib,sys\npathlib.Path(sys.argv[1]).write_text(sys.stdin.readline())\n")
    w=worker(tmp_path,'gemini');w.args.conversation='saved';w.args.prompt='Continue.'
    w.args.command=[sys.executable,str(script),str(capture),'--model','test','-p','Continue.']
    w.messages.add('Earlier guidance.');w.messages.add('Newest priorities.')
    assert w.run()==1
    content=read(capture)['message']['content']
    assert 'Earlier guidance.' in content and 'Newest priorities.' in content
    assert [m['status'] for m in w.messages.list()]==['sent','sent']


def test_claude_late_replay_cancels_missing_input_recovery(tmp_path):
    w = worker(tmp_path, 'claude')
    w.messages.add('Instruction whose replay is delayed.')
    w.send_pending(w.messages.pending())
    assert not w.missing_replay_after_result
    w.stream_event({'type': 'result', 'subtype': 'success', 'is_error': False})
    assert w.missing_replay_after_result
    assert w.messages.list()[0]['status'] == 'sent'
    w.stream_event({'type': 'user', 'isReplay': True, 'message': {
        'content': prompt(w.messages.list()[0])}})
    assert not w.missing_replay_after_result
    assert w.messages.list()[0]['status'] == 'received'
    w.stream_event({'type': 'result', 'subtype': 'success', 'is_error': False})
    assert w.messages.list()[0]['status'] == 'delivered'
    assert not w.missing_replay_after_result


@pytest.mark.parametrize('kind',['claude','gemini'])
def test_failed_native_batch_preserves_all_input_for_recovery(tmp_path,kind):
    w=worker(tmp_path,kind)
    w.messages.add('First pending instruction.')
    w.messages.add('Second pending instruction.')
    w.send_pending(w.messages.pending(), 'Full fresh-session assignment.')
    content=json.loads(w.output)['message']['content']
    assert content.startswith('Full fresh-session assignment.\n\n')
    if kind=='claude':
        w.stream_event({'type':'user','message':{'content':content}})
        failure={'type':'result','subtype':'error_during_execution','is_error':True}
    else:
        w.stream_event({'event':'step_update','step_update':{'step_type':'user_input','state':'DONE'}})
        failure={'event':'result','result':{'status':'ERROR'}}
    assert [m['status'] for m in w.messages.list()]==['received','received']
    with pytest.raises(RuntimeError):w.stream_event(failure)
    w.messages.recover()
    assert [m['status'] for m in w.messages.list()]==['queued','queued']

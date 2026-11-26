"""A public solved label must not terminate or pause a live research controller."""
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import main, common, controller_guard, gpu, capabilities
from common import save,read

@pytest.mark.parametrize('status',['open','solved_ai','solved_ai_human'])
def test_controller_continues_until_explicit_operator_stop(tmp_path,monkeypatch,status):
    run=tmp_path/'run';run.mkdir()
    save(run/'run.json',{'workers':[{'name':'claude_1'}],'category':'closed_ended',
                        'problem':'test','url':'https://example.invalid','replacement_managed':True})
    calls={'poll':0,'launch':0,'closed':0,'reap':[]}
    class Manager:
        def __init__(self,*args):
            self.states={'claude_1':{'status':'running'}};self.active_sessions={}
        def poll_cli_jobs(self):
            calls['poll']+=1
            if calls['poll']==3:(run/'STOP').touch()
        def check_storage(self):pass
        def launch_cli_job(self,name):
            calls['launch']+=1;self.active_sessions[name]=object()
        def log(self,*args):pass
        def close(self):calls['closed']+=1
    monkeypatch.setattr(main,'ResearchManager',Manager)
    monkeypatch.setattr(common,'fetch',lambda *args:{'status':status})
    monkeypatch.setattr(controller_guard,'reclaim_idle_clis',lambda *args:True)
    monkeypatch.setattr(controller_guard,'code_revision',lambda:'test')
    monkeypatch.setattr(controller_guard,'ensure_guardian',lambda *args:None)
    monkeypatch.setattr(capabilities,'ensure_services',lambda *args:{})
    monkeypatch.setattr(gpu,'reap_orphan_gpu_jobs',lambda *args,**kw:calls['reap'].append(kw.get('stop_all',False)))
    monkeypatch.setattr(main.signal,'signal',lambda *args:None)
    monkeypatch.setattr(main.time,'sleep',lambda *args:None)
    main.controller(run,keep_experiments=True)
    assert calls['poll']==3 and calls['launch']==1
    assert read(run/'controller.json')['reason']=='operator'
    assert calls['closed']==1 and calls['reap'].count(True)==1

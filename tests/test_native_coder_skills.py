"""Opt-in real Antigravity file/command access, with a credential-free model stub."""
import json
import os
from pathlib import Path
import shlex
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import save
from coder import Store,configure
from coder_workspace import prepare
from recovery import ResearchManager
from test_council import setup_run

BINARY=os.environ.get('RSTATION_TEST_ANTIGRAVITY_BIN')
pytestmark=pytest.mark.skipif(not BINARY,reason='Set RSTATION_TEST_ANTIGRAVITY_BIN for the native skill access check.')


def test_gemini_reads_installed_skills_and_applies_coder_result(tmp_path):
    run,config,env=setup_run(tmp_path);configure(run,'all',True,env,notify=False)
    worker='gemini_1';work=run/worker
    (work/'code.py').write_text('value = 1\n')
    store=Store(run);store.submit(worker,'fixture','Implement value 2.',path='.')
    root=store.root/worker/'fixture';root.mkdir(parents=True)
    prepare(root,work,'Implement value 2.');(root/'workspace/code.py').write_text('value = 2\n')
    store.update_turn(worker+':fixture',1,status='complete',answer='Changed value to 2.')
    requests=[];issued=[]
    class API(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            research='gemini-4-carbon:' in self.path
            if research:requests.append(body)
            tools={t['name']:t for group in body.get('tools',[]) for t in group.get('functionDeclarations',[])}
            parts=[{'text':'Integration check finished.'}]
            if research and len(issued)<4:
                if len(issued)<3:
                    path=([work/'AGENTS.md']+[work/'.agents/skills'/s/'SKILL.md' for s in ('coder','council')])[len(issued)]
                    name,field=('read_file','Path') if 'read_file' in tools else ('view_file','AbsolutePath')
                    args={field:str(path)}
                else:
                    name='run_command';assert name in tools
                    command='cd '+shlex.quote(str(work))+" && ./coder read fixture && ./coder diff fixture && ./coder apply fixture && python3 -c 'from code import value; assert value == 2' && printf applied > integration.txt"
                    props=tools[name].get('parametersJsonSchema',tools[name].get('parameters',{})).get('properties',{})
                    args={'CommandLine':command}
                    for field,value in {'Cwd':str(work),'Blocking':True,'WaitMsBeforeAsync':10000,'toolSummary':'Coder integration check'}.items():
                        if field in props:args[field]=value
                parts=[{'functionCall':{'name':name,'args':args}}];issued.append(name)
            data={'candidates':[{'content':{'role':'model','parts':parts},'finishReason':'STOP'}],
                  'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':20,'totalTokenCount':120}}
            streaming='streamGenerateContent' in self.path
            blob=(('data: '+json.dumps(data)+'\n\n') if streaming else json.dumps(data)).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream' if streaming else 'application/json')
            self.send_header('Content-Length',str(len(blob)));self.end_headers();self.wfile.write(blob)
    server=ThreadingHTTPServer(('127.0.0.1',0),API)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    config['workers']=[w for w in config['workers'] if w['name']==worker];config['live_input']=False
    save(run/'run.json',config)
    env.update(ANTIGRAVITY_BIN_PATH=BINARY,RSTATION_WORKER_GEMINI_BASE_URL=f'http://127.0.0.1:{server.server_port}')
    manager=ResearchManager(run,config,env,probe=True)
    try:
        manager.launch_cli_job(worker);deadline=time.monotonic()+60
        while manager.active_sessions and time.monotonic()<deadline:manager.poll_cli_jobs();time.sleep(.05)
        assert not manager.active_sessions
        assert manager.states[worker]['status']=='probe_ok'
        returned=[part['functionResponse'] for req in requests for msg in req.get('contents',[]) for part in msg.get('parts',[]) if 'functionResponse' in part]
        text=json.dumps(returned)
        assert 'Work toward the main research goal' in text
        assert 'Delegate implementation, debugging and tests' in text
        assert 'Consult another model family' in text
        assert (work/'integration.txt').read_text()=='applied'
        assert (work/'code.py').read_text()=='value = 2\n'
        save(tmp_path/'native-skill-evidence.json',{'tools':issued,'native_skill_reads':True,'applied':True})
    finally:manager.close();server.shutdown();server.server_close();thread.join(timeout=5)

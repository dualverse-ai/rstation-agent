"""Opt-in native CLI contract check against a local, credential-free Gemini server."""
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import time
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import save
from main import parser,build_config,prepare_workspaces
from recovery import ResearchManager

BINARY=os.environ.get('RSTATION_TEST_ANTIGRAVITY_BIN')
pytestmark=pytest.mark.skipif(not BINARY,reason='Set RSTATION_TEST_ANTIGRAVITY_BIN for native mock-provider tests.')

@pytest.mark.parametrize('disabled',[False,True])
@pytest.mark.parametrize('fail_first',[False,True])
def test_native_tools_auth_and_resume(tmp_path,disabled,fail_first):
    calls=[];research_calls=[]
    class API(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path,body,dict(self.headers)))
            if 'gemini-4-argon:' in self.path:research_calls.append(body)
            if fail_first and 'gemini-4-argon:' in self.path and len(research_calls)==1:
                payload=json.dumps({'error':{'code':400,'message':'mock request failure','status':'INVALID_ARGUMENT'}}).encode()
                self.send_response(400);self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload);return
            data={'candidates':[{'content':{'role':'model','parts':[{'text':'Hello'}]},'finishReason':'STOP'}],
                  'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':1,'totalTokenCount':101}}
            streaming='streamGenerateContent' in self.path
            payload=(('data: '+json.dumps(data)+'\n\n') if streaming else json.dumps(data)).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream' if streaming else 'application/json')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),API)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    args=parser().parse_args(['start','p','--codex','0','--claude','0','--gemini','1','--gemini-model','gemini-4-argon']+(['--disable-web'] if disabled else []))
    c=build_config(args,{'category':'open_ended'});c['model_api_proxy']='direct'
    e=dict(os.environ,ANTIGRAVITY_BIN_PATH=BINARY,RSTATION_WORKER_GEMINI_API_KEY='native-test-only',
           RSTATION_WORKER_GEMINI_BASE_URL=f'http://127.0.0.1:{server.server_port}')
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    m=ResearchManager(tmp_path,c,e,probe=True)
    try:
        for attempt in range(3 if fail_first else 2):
            m.states['gemini_1']['next']=0
            assert m.launch_cli_job('gemini_1')
            deadline=time.monotonic()+40
            while m.active_sessions and time.monotonic()<deadline:m.poll_cli_jobs();time.sleep(.05)
            assert not m.active_sessions
            assert m.states['gemini_1']['status']==('retrying' if fail_first and attempt==0 else 'probe_ok')
            token=m.states['gemini_1']['token']
            if attempt==0:first_token=token
            else:assert token==first_token
        (tmp_path/'request-contract.json').write_text(json.dumps(calls,indent=2))
        research=[x for x in calls if 'gemini-4-argon:' in x[0]]
        assert len(research)>=2
        for path,body,headers in research:
            tools={f['name'] for group in body.get('tools',[]) for f in group.get('functionDeclarations',[])}
            assert 'run_command' in tools and tools.intersection({'view_file','read_file'})
            assert not tools.intersection({'invoke_subagent','define_subagent','browser_subagent','manage_subagents','send_message'})
            assert ('search_web' in tools) is not disabled
            if disabled:assert not tools.intersection({'read_url_content','web_fetch'})
            assert headers.get('X-Goog-Api-Key')=='native-test-only' or headers.get('x-goog-api-key')=='native-test-only'
        assert len(research[-1][1]['contents'])>len(research[0][1]['contents'])
    finally:
        m.close();server.shutdown();server.server_close();thread.join(timeout=5)


def test_native_500k_context_compacts_and_keeps_conversation(tmp_path):
    calls=[]
    class API(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append(body)
            # Synthetic large history exercises the real native compactor; no
            # provider credentials or large paid inference requests are used.
            text='alpha beta gamma delta '*120000 if len(calls)==2 else '<summary>Remember the research assignment.</summary>'
            time.sleep(1)
            data={'candidates':[{'content':{'role':'model','parts':[{'text':text}]},'finishReason':'STOP'}],
                  'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':480000 if len(calls)==2 else 10}}
            payload=('data: '+json.dumps(data)+'\n\n').encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),API)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    args=parser().parse_args(['start','p','--codex','0','--claude','0','--gemini','1','--gemini-model','gemini-4-argon'])
    c=build_config(args,{'category':'open_ended'});c['model_api_proxy']='direct'
    e=dict(os.environ,ANTIGRAVITY_BIN_PATH=BINARY,RSTATION_WORKER_GEMINI_API_KEY='native-test-only',
           RSTATION_WORKER_GEMINI_BASE_URL=f'http://127.0.0.1:{server.server_port}')
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    m=ResearchManager(tmp_path,c,e,probe=True)
    try:
        for attempt in range(3):
            m.states['gemini_1']['next']=0
            assert m.launch_cli_job('gemini_1')
            deadline=time.monotonic()+45
            while m.active_sessions and time.monotonic()<deadline:m.poll_cli_jobs();time.sleep(.05)
            assert not m.active_sessions
            if attempt==0:token=m.states['gemini_1']['token']
            else:assert m.states['gemini_1']['token']==token
        assert m.states['gemini_1']['status']=='probe_ok'
        assert any('summary that will allow' in json.dumps(body) for body in calls)
        resumed=[b for b in calls if 'Previous Session Summary' in json.dumps(b)]
        assert resumed and len(json.dumps(resumed[-1]))<100000
        assert 'Remember the research assignment.' in json.dumps(resumed[-1])
        logs=list((tmp_path/'gemini_1/.home/.gemini/antigravity-cli/log').glob('*.log'))
        assert any('max_tokens:500000' in p.read_text() or 'max_tokens: 500000' in p.read_text() for p in logs)
    finally:
        m.close();server.shutdown();server.server_close();thread.join(timeout=5)


def test_native_distinct_errors_then_finish_keep_conversation(tmp_path):
    calls=[]
    errors=['mock first provider failure',
            'The input token count (150000) exceeds the maximum number of tokens allowed (131072).']
    class API(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            research='gemini-4-argon:' in self.path
            if research:calls.append(body)
            if research and len(calls)<=2:
                payload=json.dumps({'error':{'code':400,'message':errors[len(calls)-1],'status':'INVALID_ARGUMENT'}}).encode()
                self.send_response(400);self.send_header('Content-Type','application/json')
            else:
                parts=[{'functionCall':{'name':'finish','args':{}}}] if research and len(calls)==3 else [{'text':'Hello'}]
                data={'candidates':[{'content':{'role':'model','parts':parts},'finishReason':'STOP'}],
                      'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':10}}
                streaming='streamGenerateContent' in self.path
                payload=(('data: '+json.dumps(data)+'\n\n') if streaming else json.dumps(data)).encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream' if streaming else 'application/json')
            self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),API)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    args=parser().parse_args(['start','p','--codex','0','--claude','0','--gemini','1','--gemini-model','gemini-4-argon','--gemini-context-tokens','300000'])
    c=build_config(args,{'category':'open_ended'});c['model_api_proxy']='direct'
    e=dict(os.environ,ANTIGRAVITY_BIN_PATH=BINARY,RSTATION_WORKER_GEMINI_API_KEY='native-test-only',
           RSTATION_WORKER_GEMINI_BASE_URL=f'http://127.0.0.1:{server.server_port}')
    save(tmp_path/'run.json',c);prepare_workspaces(tmp_path,c)
    m=ResearchManager(tmp_path,c,e,probe=True)
    try:
        for attempt in range(3):
            m.states['gemini_1']['next']=0
            assert m.launch_cli_job('gemini_1')
            deadline=time.monotonic()+40
            while m.active_sessions and time.monotonic()<deadline:m.poll_cli_jobs();time.sleep(.05)
            assert not m.active_sessions
            if attempt==0:token=m.states['gemini_1']['token']
            else:assert m.states['gemini_1']['token']==token
            path=tmp_path/'gemini_1/.runtime'/f'launch_{attempt+1:06d}'/'transcript.jsonl'
            events=[json.loads(line) for line in path.read_text().splitlines()]
            terminal=next(e['result'] for e in reversed(events) if e.get('event')=='result')
            fresh_errors=[e for e in events if e.get('step_update',{}).get('step_type')=='error_message']
            if attempt<2:
                assert m.states['gemini_1']['status']=='retrying'
                assert fresh_errors and errors[attempt] in terminal['error']
                if attempt==1:assert errors[0] not in terminal['error']
            else:
                assert not fresh_errors
                assert any(e.get('step_update',{}).get('step_type')=='finish' and e['step_update']['state']=='DONE' for e in events)
                assert terminal['status']=='ERROR' and errors[1] in terminal['error']
                assert m.states['gemini_1']['status']=='probe_ok'
        settings=json.loads((tmp_path/'gemini_1/.home/.gemini/antigravity-cli/settings.json').read_text())
        assert settings['customModelsConfig']['customModels']['gemini-api://gemini-4-argon']['maxTokens']==300000
    finally:
        m.close();server.shutdown();server.server_close();thread.join(timeout=5)

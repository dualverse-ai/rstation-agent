"""Exercise the official stream-JSON model control against a local mock only."""
import json
import os
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest


def test_native_model_control_changes_next_request_only(tmp_path):
    binary=os.environ.get('RSTATION_TEST_CLAUDE_BIN')
    if not binary:pytest.skip('Set RSTATION_TEST_CLAUDE_BIN')
    calls=[]
    class API(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                payload=b'{"input_tokens":10}';mime='application/json'
            else:
                calls.append(body['model'])
                events=[('message_start',{'message':{'id':'msg_local','type':'message','role':'assistant','model':body['model'],'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':10,'output_tokens':0}}}),('content_block_start',{'index':0,'content_block':{'type':'text','text':''}}),('content_block_delta',{'index':0,'delta':{'type':'text_delta','text':'Done'}}),('content_block_stop',{'index':0}),('message_delta',{'delta':{'stop_reason':'end_turn','stop_sequence':None},'usage':{'output_tokens':1}}),('message_stop',{})]
                payload=''.join('event: '+k+'\ndata: '+json.dumps({'type':k,**v})+'\n\n' for k,v in events).encode();mime='text/event-stream'
            self.send_response(200);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    api=ThreadingHTTPServer(('127.0.0.1',0),API)
    threading.Thread(target=api.serve_forever,daemon=True).start()
    env={'PATH':os.environ['PATH'],'HOME':str(tmp_path),'CLAUDE_CONFIG_DIR':str(tmp_path/'claude'),'ANTHROPIC_API_KEY':'dummy-local-test','ANTHROPIC_BASE_URL':f'http://127.0.0.1:{api.server_port}','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_TELEMETRY':'1'}
    proc=subprocess.Popen([binary,'-p','--verbose','--input-format','stream-json','--output-format','stream-json','--model','claude-opus-5','--settings','{"disableAllHooks":true}'],cwd=tmp_path,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    q=queue.Queue()
    def reader():
        for line in proc.stdout:
            try:q.put(json.loads(line))
            except ValueError:pass
    threading.Thread(target=reader,daemon=True).start()
    def send(obj):proc.stdin.write(json.dumps(obj)+'\n');proc.stdin.flush()
    def wait(predicate):
        end=time.monotonic()+30
        while time.monotonic()<end:
            try:event=q.get(timeout=.2)
            except queue.Empty:continue
            if predicate(event):return event
        raise AssertionError('Native response timeout; requests='+repr(calls))
    try:
        send({'type':'user','message':{'role':'user','content':'Say done.'}})
        first=wait(lambda e:e.get('type')=='result')
        assert not first.get('is_error')
        count=len(calls)
        send({'type':'control_request','request_id':'preference-test','request':{'subtype':'set_model','model':'claude-opus-5-5'}})
        ack=wait(lambda e:e.get('type')=='control_response' and e.get('response',{}).get('request_id')=='preference-test')
        assert ack['response']['subtype']=='success'
        assert len(calls)==count
        send({'type':'user','message':{'role':'user','content':'Say done again.'}})
        second=wait(lambda e:e.get('type')=='result')
        assert not second.get('is_error')
        assert second['session_id']==first['session_id']
        assert calls[-1]=='claude-opus-5-5'
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        proc.stdin.close();proc.stdout.close();api.shutdown();api.server_close()

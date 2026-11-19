"""Real native CLIs against a local mock: tool restrictions and persistent follow-ups."""
import json
import os
from pathlib import Path
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import read
from council import Store
from council_service import Engine
from test_council import setup_run, activate, drain, turn


@pytest.mark.parametrize('kind,worker',[('codex','claude_1'),('claude','gemini_1'),('gemini','codex_1')])
@pytest.mark.parametrize('mode',['text','read','peer','execution'])
def test_native_consultation_has_no_execution_tools_and_remembers_context(tmp_path,kind,worker,mode):
    binary=os.environ.get('RSTATION_TEST_'+kind.upper()+'_BIN')
    if not binary:pytest.skip('Set RSTATION_TEST_'+kind.upper()+'_BIN for native checks.')
    calls=[];issued=[]
    marker=tmp_path/'forbidden-execution'
    class API(BaseHTTPRequestHandler):
        def log_message(self,*_):pass
        def do_GET(self):
            blob=json.dumps({'object':'list','data':[]}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(blob)));self.end_headers();self.wfile.write(blob)
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                blob=b'{"input_tokens":100,"totalTokens":100}'
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(blob)));self.end_headers();self.wfile.write(blob);return
            calls.append(body)
            answer='ORCHID. A useful control holds the novelty signal constant while changing planning depth.'
            eligible=bool(body.get('tools')) or kind=='codex'
            attack=mode=='execution' and eligible and not issued
            reading=mode in {'read','peer'} and eligible and not issued
            if attack or reading: issued.append(True)
            if kind=='gemini':
                parts=[{'functionCall':{'name':'run_command','args':{'CommandLine':'touch '+str(marker),'Cwd':str(tmp_path),'Blocking':True}}}] if attack else [{'text':answer}]
                if reading:
                    advertised={f['name'] for group in body.get('tools',[]) for f in group.get('functionDeclarations',[])}
                    name,field=('read_file','Path') if 'read_file' in advertised else ('view_file','AbsolutePath')
                    parts=[{'functionCall':{'name':name,'args':{field:str(evidence)}}}]
                data={'candidates':[{'content':{'role':'model','parts':parts},'finishReason':'STOP'}],
                      'usageMetadata':{'promptTokenCount':100,'candidatesTokenCount':20,'totalTokenCount':120}}
                streaming='streamGenerateContent' in self.path
                blob=(('data: '+json.dumps(data)+'\n\n') if streaming else json.dumps(data)).encode()
                self.send_response(200);self.send_header('Content-Type','text/event-stream' if streaming else 'application/json');self.send_header('Content-Length',str(len(blob)));self.end_headers();self.wfile.write(blob);return
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            def event(name,**fields):
                self.wfile.write(('event: '+name+'\ndata: '+json.dumps({'type':name,**fields})+'\n\n').encode());self.wfile.flush()
            if kind=='claude':
                event('message_start',message={'id':'msg_test','type':'message','role':'assistant','model':body['model'],
                    'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':100,'output_tokens':0}})
                if reading:
                    event('content_block_start',index=0,content_block={'type':'tool_use','id':'read_call','name':'Read','input':{}})
                    event('content_block_delta',index=0,delta={'type':'input_json_delta','partial_json':json.dumps({'file_path':str(evidence)})})
                elif attack:
                    event('content_block_start',index=0,content_block={'type':'tool_use','id':'forbidden_call','name':'Bash','input':{}})
                    event('content_block_delta',index=0,delta={'type':'input_json_delta','partial_json':json.dumps({'command':'touch '+str(marker)})})
                else:
                    event('content_block_start',index=0,content_block={'type':'text','text':''})
                    event('content_block_delta',index=0,delta={'type':'text_delta','text':answer})
                event('content_block_stop',index=0)
                event('message_delta',delta={'stop_reason':'tool_use' if attack or reading else 'end_turn','stop_sequence':None},usage={'output_tokens':20})
                event('message_stop')
            else:
                item={'id':'msg_test','type':'message','role':'assistant','status':'completed',
                      'content':[{'type':'output_text','text':answer,'annotations':[]}]}
                if attack:item={'id':'fc_forbidden','type':'custom_tool_call','call_id':'forbidden_call','name':'exec','namespace':'functions',
                                'input':'text(await tools.exec_command('+json.dumps({'cmd':'touch '+str(marker)})+'));'}
                if reading:item={'id':'fc_read','type':'custom_tool_call','call_id':'read_call','name':'exec','namespace':'functions',
                                 'input':'text(await tools.mcp__research_files__read_file('+json.dumps({'path':str(evidence)})+'));'}
                event('response.created',response={'id':'resp_test','status':'in_progress','output':[]})
                event('response.output_item.added',output_index=0,item=item)
                if not attack and not reading:event('response.output_text.delta',item_id='msg_test',output_index=0,content_index=0,delta=answer)
                event('response.output_item.done',output_index=0,item=item)
                event('response.completed',response={'id':'resp_test','status':'completed','output':[item],
                    'usage':{'input_tokens':100,'output_tokens':20,'total_tokens':120}})
    server=ThreadingHTTPServer(('127.0.0.1',0),API)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    run,c,env=setup_run(tmp_path)
    peer=next(w['name'] for w in c['workers'] if w['name']!=worker)
    evidence=run/(peer if mode=='peer' else worker)/'method.txt'; evidence.write_text('FILE_ONLY_NONCE_91837: learned planning uses ten imagined steps.\n')
    env[{'codex':'CODEX_BIN_PATH','claude':'CLAUDE_BIN_PATH','gemini':'ANTIGRAVITY_BIN_PATH'}[kind]]=binary
    env['RSTATION_WORKER_'+kind.upper()+'_BASE_URL']=f'http://127.0.0.1:{server.server_port}'+('/v1' if kind=='codex' else '')
    activate(run,env,limits={'max_attempts':1,'turn_seconds':60})
    store=Store(run);store.submit(worker,'native','Remember the research label ORCHID. Suggest one control.',family=kind)
    engine=Engine(run,env);key=worker+':native'
    try:
        drain(engine,lambda:turn(store,key)['status'] in {'complete','failed'},seconds=55)
        assert not marker.exists(), 'A council adviser executed a forbidden command.'
        if mode=='execution' and turn(store,key)['status']=='failed':return
        if mode in {'read','peer'}:
            (tmp_path/'read-requests.json').write_text(json.dumps(calls,indent=2))
            assert any('FILE_ONLY_NONCE_91837' in json.dumps(request) for request in calls[1:]), calls
            assert evidence.read_text().startswith('FILE_ONLY_NONCE_91837')
        assert turn(store,key)['status']=='complete',turn(store,key)
        native=store.root/worker/'native/native.json';token=read(native)['token'];assert token
        first_count=len(calls)
        store.submit(worker,'native','What research label did I give you? Explain the control further.')
        drain(engine,lambda:turn(store,key,2)['status'] in {'complete','failed'},seconds=55)
        assert turn(store,key,2)['status']=='complete',turn(store,key,2)
        assert read(native)['token']==token
        assert any('ORCHID' in json.dumps(request) for request in calls[first_count:])
        def tool_names(value):
            names=[]
            if isinstance(value,dict):
                if 'name' in value:names.append(value['name'])
                for nested in value.values():names.extend(tool_names(nested))
            elif isinstance(value,list):
                for nested in value:names.extend(tool_names(nested))
            return names
        all_names={n for call in calls for n in tool_names(call.get('tools',[]))}
        if kind=='codex':
            advertised='\n'.join(json.dumps(item) for call in calls for item in call.get('input',[]) if item.get('type')=='additional_tools')
            assert 'exec_command(args:' not in advertised
            assert 'mcp__research_files__read_file' in json.dumps(calls) or mode!='read'
        (tmp_path/'request-tools.json').write_text(json.dumps([call.get('tools',[]) for call in calls],indent=2))
        # Antigravity injects management of its own conversation's background
        # tasks. The fresh isolated adviser cannot create any such tasks.
        allowed={'finish','update_plan','manage_task','Read','Glob','Grep','view_file','read_file','list_dir','find_by_name','grep_search',
                 'mcp__research_files__read_file','mcp__research_files__list_directory','mcp__research_files__search_text'}
        assert all_names <= allowed,all_names
        assert all('UNRELATED_SECRET' not in json.dumps(call) for call in calls)
    finally:
        engine.close();server.shutdown();server.server_close();thread.join(timeout=5)

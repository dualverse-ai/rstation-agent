import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import replace
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROOT, save, read, workspace_bytes, initial_prompt, researcher_instructions, alive, pid_token
from main import parser, build_config, prepare_workspaces
from recovery import ResearchManager, ClaudeResume, routes
from proxy import Router
from gpu import reserve_slots, run_gpu
from station.eval_research.gpu_coordinator import GPUCoordinator
from station.workers.job_manager import CliJobLaunchDecision, CliJobFailure

def cfg(tmp_path,kind="codex"):
    a=parser().parse_args(["start","test_problem","--gemini","0","--name","test_run","--codex",str(int(kind=="codex")),"--claude",str(int(kind=="claude"))])
    result=build_config(a,{"category":"closed_ended"})
    result["gpu_coord_file"]=str(tmp_path/"shared.json")
    result["live_input"]=False  # Legacy lifecycle fixtures use single-turn fake CLIs.
    return result

def fake_cli(tmp_path,kind):
    p=tmp_path/("fake-"+kind)
    p.write_text("#!"+sys.executable+"\n"+'''
import json,sys,pathlib,time
root=pathlib.Path.cwd();n=int((root/"counter").read_text())+1 if (root/"counter").exists() else 1
(root/"counter").write_text(str(n))
kind="claude" if "claude" in sys.argv[0] else "codex"
prompt=sys.stdin.read() if kind=="codex" else next((v for v in sys.argv if v=="Continue your research."), "fresh")
(root/("prompt"+str(n))).write_text(prompt)
if kind=="codex":
 print(json.dumps({"type":"thread.started","thread_id":"stable-session"}),flush=True)
 if n==1:
  print(json.dumps({"type":"error","message":"transient"}),flush=True);sys.exit(1)
 print(json.dumps({"type":"turn.completed"}),flush=True)
else:
 print(json.dumps({"type":"system","subtype":"init","session_id":"stable-session"}),flush=True)
 print(json.dumps({"type":"result","session_id":"stable-session","is_error":n==1,"result":"Hello" if n>1 else "transient"}),flush=True)
''')
    p.chmod(0o755);return str(p)

def env(tmp_path,kind):
    return dict(os.environ,RSTATION_WORKER_CODEX_API_KEY="test-codex",RSTATION_WORKER_CODEX_BASE_URL="http://127.0.0.1:1",
                RSTATION_WORKER_CLAUDE_API_KEY="test-claude",RSTATION_WORKER_CLAUDE_BASE_URL="http://127.0.0.1:1",
                CODEX_BIN_PATH=fake_cli(tmp_path,"codex"),CLAUDE_BIN_PATH=fake_cli(tmp_path,"claude"))

def drain(manager,timeout=5):
    deadline=time.monotonic()+timeout
    while manager.active_sessions and time.monotonic()<deadline:
        manager.poll_cli_jobs();time.sleep(.02)
    assert not manager.active_sessions

def test_defaults_and_per_worker_gpus(tmp_path):
    a=parser().parse_args(["start","p","--gpus","0,1,2,3","--worker-gpus","codex_1=0,1","--worker-gpus","claude_1=2,3"])
    c=build_config(a,{"category":"open_ended"})
    assert a.codex_model=='gpt-6.2-astra'
    assert len(c["workers"])==3 and c["storage_gb"]==50 and c["gpu_per_agent"]==1
    assert c["workers"][0]["gpus"]==[0,1]
    assert c["workers"][1]["gpus"]==[2,3]
    assert "API_KEY" not in json.dumps(c)
    assert c["model_api_proxy"] == "direct"


def test_main_codex_model_does_not_change_native_assistant_model(tmp_path):
    c=cfg(tmp_path);c['subagents']=2;c['workers'][0]['model']='gpt-6.2-astra'
    save(tmp_path/'run.json',c)
    manager=ResearchManager(tmp_path,c,env(tmp_path,'codex'))
    try:
        decision=CliJobLaunchDecision('fresh',1,0,None,0)
        claimed=manager._claim_cli_job_launch('codex_1','launch_test',decision)
        spec=manager._build_cli_job_launch_spec('codex_1','launch_test',decision,claimed)
        assert 'agents.default_subagent_model="gpt-6-astra"' in spec.codex_config_overrides
        assert 'model_reasoning_effort="max"' in spec.codex_config_overrides
    finally:manager.close()

@pytest.mark.parametrize("flags",[["--codex","-1"],["--gpu-per-agent","0"],["--gpus","0","--worker-gpus","codex_1=1"],["--worker-gpus","missing=0"],["--name","../bad"]])
def test_bad_config(flags):
    a=parser().parse_args(["start","p"]+flags)
    with pytest.raises(ValueError):build_config(a,{"category":"open_ended"})

@pytest.mark.parametrize("kind",["codex","claude"])
def test_actual_subprocess_failure_resumes_same_workspace(tmp_path,kind):
    c=cfg(tmp_path,kind);save(tmp_path/"run.json",c);prepare_workspaces(tmp_path,c)
    m=ResearchManager(tmp_path,c,env(tmp_path,kind))
    name=kind+"_1"
    try:
        assert m.launch_cli_job(name);drain(m)
        assert m.states[name]["status"]=="retrying"
        assert m.states[name]["token"]=="stable-session"
        m.states[name]["next"]=0
        assert m.launch_cli_job(name);drain(m)
        assert m.states[name]["status"]=="continuing"
        assert (tmp_path/name/"prompt2").read_text()=="Continue your research."
        assert (tmp_path/name/"counter").read_text()=="2"
        assert m.states[name]["token"]=="stable-session"
    finally:m.close()
    fresh=ResearchManager(tmp_path,c,env(tmp_path,kind))
    try:
        assert fresh.states[name]["token"]=="stable-session"
        fresh.states[name]["next"]=0
        assert fresh.launch_cli_job(name);drain(fresh)
        assert (tmp_path/name/"counter").read_text()=="3"
    finally:fresh.close()

def test_claude_resume_flags_and_error_semantics(tmp_path):
    backend=ClaudeResume()
    result=backend.prepare_resume_launch(executable="claude",run_dir=str(tmp_path),model_name="opus",
        storage_root=str(tmp_path),prompt="Continue.",workspace_root=str(tmp_path),resume_token="session")
    assert result.command[-2:]==["--resume","session"]
    assert "--no-session-persistence" not in result.command
    assert "--fork-session" not in result.command
    path=tmp_path/"stream"
    path.write_text('{"type":"result","session_id":"abc","is_error":true}\n')
    assert backend.extract_resume_token(str(path))=="abc"
    class Session:transcript_path=str(path)
    assert not ResearchManager.completed(Session())

def test_resume_budget_and_provider_rotation(tmp_path,monkeypatch):
    c=cfg(tmp_path);e=env(tmp_path,"codex")
    e.update(RSTATION_WORKER_CODEX_BACKUP_API_KEY="backup",RSTATION_WORKER_CODEX_BACKUP_BASE_URL="http://127.0.0.1:2")
    m=ResearchManager(tmp_path,c,e)
    monkeypatch.setattr(m,"_probe_primary",lambda worker:False)
    name="codex_1";s=m.states[name]
    try:
        s.update(token="existing",spawn=1,resumes=14,launches=1)
        assert m._next_cli_job_launch(name,m._load_cli_job_state(name)) is None
        assert s["token"]=="existing" and s["resumes"]==0
        assert s["next"]>time.time()
        # A positively invalidated session is the separate fresh-context case.
        s["session_unavailable"]=True
        m._schedule_cli_job_fresh_attempt(name,None,CliJobFailure("session_unavailable",False,"gone"))
        assert s["token"] is None and s["route_offset"]==1
        s.update(next=0)
        d=m._next_cli_job_launch(name,m._load_cli_job_state(name))
        claimed=m._claim_cli_job_launch(name,"launch_test",d)
        spec=m._build_cli_job_launch_spec(name,"launch_test",d,claimed)
        assert spec.env["CODEX_API_KEY"]=="backup"
        assert "CONTINUATION.md" in spec.prompt and "interrupted" in spec.prompt
        for count in [0,1,2,3,4]:
            s["route_offset"]=0
            d=CliJobLaunchDecision("resume",1,count,"existing",count)
            spec=m._build_cli_job_launch_spec(name,"launch_test",d,s)
            assert spec.env["CODEX_API_KEY"]==("test-codex" if count//2%2==0 else "backup")
    finally:m.close()

def test_claude_dedicated_backup_routes():
    r=routes('claude', {'RSTATION_WORKER_CLAUDE_API_KEY':'a', 'RSTATION_WORKER_CLAUDE_BASE_URL':'http://a',
        'RSTATION_WORKER_CLAUDE_BACKUP_API_KEY':'b;c', 'RSTATION_WORKER_CLAUDE_BACKUP_BASE_URL':'http://b;'})
    assert [x['api_key'] for x in r] == ['a','b','c']
    assert [x['base_url'] for x in r] == ['http://a','http://b','http://a']
    with pytest.raises(ValueError):
        routes('claude', {'RSTATION_WORKER_CLAUDE_API_KEY':'a', 'RSTATION_WORKER_CLAUDE_BASE_URL':'http://a',
            'RSTATION_WORKER_CLAUDE_BACKUP_API_KEY':'b;c', 'RSTATION_WORKER_CLAUDE_BACKUP_BASE_URL':'http://b'})


def test_unselected_provider_credentials_are_not_used_for_claude():
    with pytest.raises(ValueError, match='CLAUDE_API_KEY'):
        routes('claude', {'CODEX_API_KEY':'test-key', 'CODEX_BASE_URL':'http://codex',
                         'ANTHROPIC_AUTH_TOKEN':'other-profile'})


def test_prompts_open_closed_and_proxy(tmp_path):
    c=cfg(tmp_path);w=c["workers"][0]
    p=researcher_instructions(c,w,tmp_path)
    assert "Solved status is informational and does not end your assignment" in p
    assert "50 GB" in p and "GPU access is disabled" in p
    c["category"]="open_ended"
    assert "until the operator stops you" in researcher_instructions(c,w,tmp_path)
    c["research_proxy"]="http://localhost:8118";w["gpus"]=[1]
    p=researcher_instructions(c,w,tmp_path)
    assert "8118" in p and "release" in p.lower() and "at most 1 GPU" in p

def test_symlink_usage_not_counted(tmp_path):
    (tmp_path/"a").write_bytes(b"abc")
    (tmp_path/"link").symlink_to(tmp_path/"a")
    (tmp_path/"loop").symlink_to(tmp_path,target_is_directory=True)
    assert workspace_bytes(tmp_path)==3

def test_gpu_atomic_per_worker_limit(tmp_path):
    first=reserve_slots(tmp_path,1,1);assert len(first)==1
    assert reserve_slots(tmp_path,1,1)==[]
    first[0].close()
    second=reserve_slots(tmp_path,2,2);assert len(second)==2
    assert reserve_slots(tmp_path,2,1)==[]
    for slot in second:slot.close()

def test_gpu_cooperates_with_station_and_releases(tmp_path):
    c=cfg(tmp_path);c["workers"][0]["gpus"]=[0,1];save(tmp_path/"run.json",c)
    station=GPUCoordinator(c["gpu_coord_file"],[0,1],"ordinary-station",300)
    assert station.allocate("test",1)==[0]
    output=tmp_path/"assigned"
    result=run_gpu(tmp_path,"codex_1",1,[sys.executable,"-c",f"import os,pathlib; pathlib.Path({str(output)!r}).write_text(os.environ['CUDA_VISIBLE_DEVICES'])"])
    assert result==0 and output.read_text()=="1"
    remaining=read(c["gpu_coord_file"])["allocations"]
    assert list(remaining)==["ordinary-station:test"]
    station.deallocate("test")

def test_gpu_failure_timeout_and_limit(tmp_path):
    c=cfg(tmp_path);c["workers"][0]["gpus"]=[0];save(tmp_path/"run.json",c)
    assert run_gpu(tmp_path,"codex_1",1,[sys.executable,"-c","raise SystemExit(7)"])==7
    assert read(c["gpu_coord_file"])["allocations"]=={}
    assert run_gpu(tmp_path,"codex_1",1,[sys.executable,"-c","import time;time.sleep(60)"],timeout=.2)==124
    assert read(c["gpu_coord_file"])["allocations"]=={}
    with pytest.raises(ValueError):run_gpu(tmp_path,"codex_1",2,["true"])

def test_proxy_routing():
    r=Router({"model.example"},"http://research:8118","http://model:8119",["station.example"])
    assert r.upstream("station.example") is None
    assert r.upstream("model.example")=="http://model:8119"
    assert r.upstream("arxiv.org")=="http://research:8118"

def test_proxy_real_http_post_and_connect():
    import http.server
    seen=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body=self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path,body));self.send_response(200);self.send_header("Content-Length","2");self.end_headers();self.wfile.write(b"OK")
        def log_message(self,*a):pass
    server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        port=server.server_port
        with Router(set()) as r:
            from urllib.parse import urlsplit
            s=socket.create_connection(("127.0.0.1",urlsplit(r.url).port))
            request=f"POST http://127.0.0.1:{port}/test HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\nConnection: close\r\n\r\ndata".encode()
            s.sendall(request);response=b""
            while True:
                data=s.recv(4096)
                if not data:break
                response+=data
            assert response.endswith(b"OK");s.close()
            s=socket.create_connection(("127.0.0.1",urlsplit(r.url).port))
            s.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\nHost: localhost\r\n\r\n".encode())
            assert b"200 Connection Established" in s.recv(4096)
            s.sendall(b"POST /tunnel HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\nConnection: close\r\n\r\nx")
            while s.recv(4096):pass
            s.close()
        assert seen==[("/test",b"data"),("/tunnel",b"x")]
    finally:server.shutdown();server.server_close()

def test_private_paths_ignored():
    repo=ROOT
    private_paths=[ROOT/p for p in [".env",".env.local",".venv/bin/python","agent_data/secret","auth.json",".claude/session","worker.log"]]
    result=subprocess.run(["git","check-ignore","--no-index"]+list(map(str,private_paths)),cwd=repo,capture_output=True,text=True)
    assert result.returncode==0
    assert set(result.stdout.splitlines())==set(map(str,private_paths))
    source=subprocess.run(["git","check-ignore","--no-index",str(ROOT/"main.py")],cwd=repo,capture_output=True,text=True)
    assert source.returncode==1

def test_connect_failure_closes_tunnel_without_plaintext_error():
    import socketserver
    import struct
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlsplit

    class ResetPeer(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.recv(1)
            self.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack('ii', 1, 0))

    with socketserver.ThreadingTCPServer(('127.0.0.1', 0), ResetPeer) as peer:
        threading.Thread(target=peer.serve_forever, daemon=True).start()
        try:
            with Router(set()) as router:
                def connect_and_reset(_):
                    with socket.create_connection(('127.0.0.1', urlsplit(router.url).port), timeout=5) as client:
                        client.sendall(f'CONNECT 127.0.0.1:{peer.server_address[1]} HTTP/1.1\r\n\r\n'.encode())
                        header=b''
                        while not header.endswith(b'\r\n\r\n'):header+=client.recv(1)
                        assert b'200 Connection Established' in header
                        client.sendall(b'x')
                        assert client.recv(1024)==b''
                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(connect_and_reset, range(8)))
        finally:peer.shutdown()

def test_native_requires_explicit_isolated_mode(monkeypatch):
    import common
    monkeypatch.setattr(common,"fetch_guide",lambda *a:"# rStation\n**Isolated research setting: external web access is disabled.**")
    with pytest.raises(ValueError,match="isolated"):common.problem_info("http://localhost","p")

def test_gpu_helper_cli_options(tmp_path):
    c=cfg(tmp_path);c["workers"][0]["gpus"]=[0];save(tmp_path/"run.json",c);prepare_workspaces(tmp_path,c)
    result=subprocess.run([str(tmp_path/"codex_1/gpu-run"),"--count","1","--timeout","2","--",sys.executable,"-c","print('gpu-cli-ok')"],capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
    assert "gpu-cli-ok" in result.stdout

def test_monitor_real_response_not_process_only(tmp_path):
    from monitor import snapshot
    c=cfg(tmp_path);save(tmp_path/"run.json",c);prepare_workspaces(tmp_path,c)
    save(tmp_path/"controller.json",{"pid":os.getpid(),"token":pid_token(os.getpid()),"heartbeat":time.time()})
    save(tmp_path/"codex_1/.runtime/state.json",{"status":"running","pid":os.getpid(),"pid_token":pid_token(os.getpid()),
         "last_response":time.time()-120,"last_tool_completed":time.time()-30,"last_stream_event":time.time()-5,"workspace_bytes":1500000000})
    row=snapshot(tmp_path)["workers"][0]
    assert row["process_alive"] and row["last_response_age_seconds"]>=120
    assert row["last_tool_completed_age_seconds"]>=30 and row["workspace_gb"]==1.5
    assert "token" not in row and "pid_token" not in row

def test_exited_unreaped_process_is_not_alive():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        token = pid_token(child.pid)
        assert token and alive(child.pid, token)
        child.kill()
        # Observe exit without reaping: reproduce a stopped controller whose
        # parent remains alive to monitor another worker.
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOWAIT)
        assert pid_token(child.pid) is None
        assert not alive(child.pid, token)
    finally:
        child.wait(timeout=5)

@pytest.mark.parametrize("kind",["codex","claude"])
@pytest.mark.parametrize("terminal_error",[False,True])
def test_native_retries_finish_before_provider_recovery(tmp_path,kind,terminal_error):
    c=cfg(tmp_path,kind);save(tmp_path/"run.json",c);prepare_workspaces(tmp_path,c)
    e=env(tmp_path,kind);p=Path(e["CODEX_BIN_PATH" if kind=="codex" else "CLAUDE_BIN_PATH"])
    start=({"type":"thread.started","thread_id":"preserved"} if kind=="codex" else
           {"type":"system","subtype":"init","session_id":"preserved"})
    retry=({"type":"error","message":"stream timeout; reconnecting"} if kind=="codex" else
           {"type":"system","subtype":"api_retry","error_status":503,"max_retries":10})
    end=({"type":"turn.failed" if terminal_error else "turn.completed"} if kind=="codex" else
         {"type":"result","session_id":"preserved","is_error":terminal_error,"result":"failed" if terminal_error else "recovered"})
    p.write_text("#!"+sys.executable+"\nimport json,time,pathlib\n"+
                 "print(json.dumps("+repr(start)+"),flush=True)\n"+
                 "for i in range(4):\n print(json.dumps("+repr(retry)+"),flush=True)\n time.sleep(.2)\n"+
                 "pathlib.Path('native-retries-finished').touch()\n"+
                 "print(json.dumps("+repr(end)+"),flush=True)\n"+
                 ("raise SystemExit(1)\n" if terminal_error and kind=="codex" else ""))
    m=ResearchManager(tmp_path,c,e);name=kind+"_1"
    try:
        m.launch_cli_job(name);drain(m)
        state=m.states[name]
        assert (tmp_path/name/"native-retries-finished").exists()
        assert state["token"]=="preserved"
        assert state["status"]==("retrying" if terminal_error else "continuing")
        assert not state.get("provider_interrupt")
        assert state.get("last_response") is None  # Retries are not model progress.
    finally:m.close()

def test_claude_intentional_background_wait_is_not_killed(tmp_path):
    c=cfg(tmp_path,"claude");c["idle_seconds"]=.1
    save(tmp_path/"run.json",c);prepare_workspaces(tmp_path,c)
    e=env(tmp_path,"claude");p=Path(e["CLAUDE_BIN_PATH"])
    p.write_text("#!"+sys.executable+"\n"+"""
import json,time,pathlib
print(json.dumps({"type":"system","subtype":"init","session_id":"waiting"}),flush=True)
print(json.dumps({"type":"system","subtype":"task_started","task_id":"background-timer"}),flush=True)
time.sleep(.7)
pathlib.Path('wait-finished').touch()
print(json.dumps({"type":"result","session_id":"waiting","is_error":False,"result":"Wait finished"}),flush=True)
""")
    m=ResearchManager(tmp_path,c,e)
    try:
        m.launch_cli_job("claude_1");drain(m)
        assert (tmp_path/"claude_1/wait-finished").exists()
        assert m.states["claude_1"]["status"]=="continuing"
        assert m.states["claude_1"]["token"]=="waiting"
    finally:m.close()

@pytest.mark.parametrize("kind",["codex"])
def test_idle_watchdog_and_retry(tmp_path,kind):
    c=cfg(tmp_path,kind);c["idle_seconds"]=1  # Allow cold CLI startup on slower filesystems.
    save(tmp_path/"run.json",c);prepare_workspaces(tmp_path,c)
    e=env(tmp_path,kind);p=Path(e["CODEX_BIN_PATH" if kind=="codex" else "CLAUDE_BIN_PATH"])
    event={"type":"thread.started","thread_id":"idle-token"} if kind=="codex" else {"type":"system","subtype":"init","session_id":"idle-token"}
    p.write_text("#!"+sys.executable+"\nimport time,json\nprint("+repr(json.dumps(event))+",flush=True)\ntime.sleep(60)\n")
    m=ResearchManager(tmp_path,c,e)
    try:
        m.launch_cli_job(kind+"_1");drain(m)
        assert m.states[kind+"_1"]["status"]=="retrying"
        assert m.states[kind+"_1"]["token"]=="idle-token"
    finally:m.close()

@pytest.mark.parametrize("protected",[False,True])
def test_repeated_provider_notices_do_not_mask_stall(tmp_path,protected):
    c=cfg(tmp_path,"codex");c["idle_seconds"]=.2
    save(tmp_path/"run.json",c);prepare_workspaces(tmp_path,c)
    e=env(tmp_path,"codex")
    Path(e["CODEX_BIN_PATH"]).write_text("#!"+sys.executable+"\n"+'''
import json,time,sys
sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"retry-session"}),flush=True)
while True:
 print(json.dumps({"type":"error","message":"Reconnecting: timeout"}),flush=True)
 time.sleep(.03)
''')
    m=ResearchManager(tmp_path,c,e)
    try:
        if protected:
            save(tmp_path/'codex_1/.runtime/gpu-jobs/active.json',dict(pid=os.getpid(),token=pid_token(os.getpid())))
        m.launch_cli_job('codex_1')
        deadline=time.monotonic()+1.5
        while m.active_sessions and time.monotonic()<deadline:
            m.poll_cli_jobs();time.sleep(.02)
        if protected:
            assert m.active_sessions and m.states['codex_1']['api_retries']>5
        else:
            assert not m.active_sessions
            assert m.states['codex_1']['status']=='retrying'
            assert m.states['codex_1']['token']=='retry-session'
    finally:m.close()

def test_claude_bare_avoids_project_instructions(tmp_path):
    result=ClaudeResume().prepare_launch(executable="claude",run_dir=str(tmp_path),model_name="opus",storage_root=str(tmp_path),prompt="Research.",workspace_root=str(tmp_path))
    assert "--bare" in result.command
    assert "--fork-session" not in result.command

def test_four_worker_tmux_start_stop_resume(tmp_path):
    import http.server
    class API(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            data={"category":"open_ended","status":"open"}
            body=b"# rStation\n" if self.path=="/skill.md" else json.dumps({"success":True,"data":data}).encode()
            self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*a):pass
    server=http.server.ThreadingHTTPServer(("127.0.0.1",0),API)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    e=env(tmp_path,"codex")
    for kind,key in [("codex","CODEX_BIN_PATH"),("claude","CLAUDE_BIN_PATH")]:
        p=Path(e[key])
        p.write_text("#!"+sys.executable+"\n"+'''
import json,sys,pathlib,time
kind="claude" if "claude" in sys.argv[0] else "codex"
if "app-server" in sys.argv:
 for line in sys.stdin:
  q=json.loads(line);method=q.get("method");out={}
  if method=="initialize":out={}
  elif method in {"thread/start","thread/resume"}:out={"thread":{"id":"integration-session"}}
  elif method=="turn/start":out={"turn":{"id":"turn-test"}}
  else:continue
  print(json.dumps({"id":q["id"],"result":out}),flush=True)
  if method=="turn/start":
   print(json.dumps({"method":"item/completed","params":{"item":{"id":"reply","type":"agentMessage","text":"Hello"}}}),flush=True)
   print(json.dumps({"method":"turn/completed","params":{"turn":{"id":"turn-test","status":"completed"}}}),flush=True)
 sys.exit(0)
if "--input-format" in sys.argv:
 for line in sys.stdin:
  q=json.loads(line)
  print(json.dumps({"type":"system","subtype":"init","session_id":"integration-session"}),flush=True)
  print(json.dumps({"type":"user","message":q["message"]}),flush=True)
  print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"Hello"}]}}),flush=True)
  print(json.dumps({"type":"result","subtype":"success","result":"Hello","is_error":False,"session_id":"integration-session"}),flush=True)
 sys.exit(0)
prompt=sys.stdin.read() if kind=="codex" else " ".join(sys.argv)
if kind=="codex":
 print(json.dumps({"type":"thread.started","thread_id":"integration-session"}),flush=True)
 print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"Hello"}}),flush=True)
 print(json.dumps({"type":"turn.completed"}),flush=True)
else:
 print(json.dumps({"type":"system","subtype":"init","session_id":"integration-session"}),flush=True)
 print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"Hello"}]}}),flush=True)
 print(json.dumps({"type":"result","result":"Hello","session_id":"integration-session","is_error":False}),flush=True)
''')
    e["RSTATION_AGENT_DATA"]=str(tmp_path/"data")
    name="integration_"+str(os.getpid())
    cli=[str(ROOT/"agents")]
    run=tmp_path/"data"/name
    try:
        r=subprocess.run(cli+["start","test_problem","--codex","2","--claude","2","--gemini","0","--no-council","--no-coder","--name",name,"--url",f"http://127.0.0.1:{server.server_port}"],env=e,capture_output=True,text=True,timeout=45)
        assert r.returncode==0,r.stdout+r.stderr
        deadline=time.time()+10
        while time.time()<deadline:
            states=[read(run/f"{kind}_{i}/.runtime/state.json",{}) for kind in ["codex","claude"] for i in [1,2]]
            if all(s.get("last_response") for s in states):break
            time.sleep(.2)
        assert all(s.get("last_response") for s in states)
        panes=subprocess.check_output(["tmux","list-panes","-t","rs-"+name],text=True).splitlines()
        assert len(panes)==4
        r=subprocess.run(cli+["status",name,"--json"],env=e,capture_output=True,text=True,timeout=10)
        rows=json.loads(r.stdout)[0]["workers"];assert len(rows)==4 and all(w["last_response"] for w in rows)
        r=subprocess.run(cli+["pause",name,"codex_1"],env=e,capture_output=True,text=True,timeout=10)
        assert r.returncode==0,r.stderr
        deadline=time.time()+5
        while time.time()<deadline:
            paused=read(run/"codex_1/.runtime/state.json",{})
            if paused.get("status")=="paused":break
            time.sleep(.1)
        assert paused["status"]=="paused" and paused["token"]=="integration-session"
        assert read(run/"claude_1/.runtime/state.json")["status"]!="paused"
        r=subprocess.run(cli+["resume",name,"--worker","codex_1"],env=e,capture_output=True,text=True,timeout=10)
        assert r.returncode==0,r.stderr
        deadline=time.time()+5
        while time.time()<deadline:
            resumed=read(run/"codex_1/.runtime/state.json",{})
            if resumed.get("status")!="paused":break
            time.sleep(.1)
        assert resumed["status"]!="paused" and resumed["token"]=="integration-session"
        r=subprocess.run(cli+["stop",name],env=e,capture_output=True,text=True,timeout=45);assert r.returncode==0,r.stderr
        r=subprocess.run(cli+["resume",name],env=e,capture_output=True,text=True,timeout=45);assert r.returncode==0,r.stdout+r.stderr
        deadline=time.time()+10
        while time.time()<deadline:
            states=[read(run/f"{kind}_{i}/.runtime/state.json",{}) for kind in ["codex","claude"] for i in [1,2]]
            if all(s.get("launches",0)>=2 for s in states):break
            time.sleep(.2)
        assert all(s.get("token")=="integration-session" for s in states)
        assert all(s.get("launches",0)>=2 for s in states)
    finally:
        if (run/"run.json").exists():subprocess.run(cli+["stop",name],env=e,capture_output=True,timeout=45)
        subprocess.run(["tmux","kill-session","-t","rs-"+name],capture_output=True)
        server.shutdown();server.server_close()

@pytest.mark.parametrize("kind",["codex","claude"])
def test_recover_session_from_transcript_after_state_write_crash(tmp_path,kind):
    c=cfg(tmp_path,kind);name=kind+"_1"
    runtime=tmp_path/name/".runtime";runtime.mkdir(parents=True)
    save(runtime/"state.json",{"spawn":1,"resumes":0,"token":None,"next":0,"launches":1,"route_count":0})
    launch=runtime/"launch_000001";launch.mkdir()
    event={"type":"thread.started","thread_id":"recover-me"} if kind=="codex" else {"type":"system","subtype":"init","session_id":"recover-me"}
    (launch/"transcript.jsonl").write_text(json.dumps(event)+"\n")
    m=ResearchManager(tmp_path,c,env(tmp_path,kind))
    try:assert m.states[name]["token"]=="recover-me"
    finally:m.close()

def test_empty_failure_stream_keeps_existing_context(tmp_path):
    c=cfg(tmp_path);m=ResearchManager(tmp_path,c,env(tmp_path,"codex"))
    try:
        m.states["codex_1"].update(token="valid",spawn=1,resumes=14)
        m._schedule_cli_job_fresh_attempt("codex_1",None,CliJobFailure("infra_transient",True,"provider unavailable"))
        assert m.states["codex_1"]["token"]=="valid"
        assert m.states["codex_1"]["resumes"]==0
        assert m.states["codex_1"]["status"]=="retrying"
    finally:m.close()

@pytest.mark.parametrize('kind,message',[
    ('claude','No conversation found with session ID: missing'),
    ('codex','Error: thread/resume: thread/resume failed: no rollout found for thread id missing (code -32600)'),
])
def test_confirmed_missing_session_allows_fresh_context(tmp_path,kind,message):
    c=cfg(tmp_path,kind);m=ResearchManager(tmp_path,c,env(tmp_path,kind));name=kind+'_1'
    stderr=tmp_path/"stderr";stderr.write_text(message+'\n')
    transcript=tmp_path/"stream";transcript.write_text("")
    from types import SimpleNamespace
    session=SimpleNamespace(stderr_path=str(stderr),transcript_path=str(transcript))
    try:
        m.states[name].update(token="missing",spawn=1)
        failure=m._classify_cli_job_failure(name,session)
        assert failure.category=="session_unavailable"
        m._schedule_cli_job_fresh_attempt(name,session,failure)
        assert m.states[name]["token"] is None
    finally:m.close()

def test_two_gpu_experiments_same_worker_never_exceed_limit(tmp_path):
    c=cfg(tmp_path);c["workers"][0]["gpus"]=[0,1];save(tmp_path/"run.json",c)
    runtime=tmp_path/"codex_1/.runtime";runtime.mkdir(parents=True)
    log=tmp_path/"intervals"
    command=[sys.executable,"-c",
        "import os,time,json; p="+repr(str(log))+"; f=open(p,'a'); f.write(json.dumps(['start',os.getpid(),time.time()])+'\\n'); f.flush(); time.sleep(1.5); f.write(json.dumps(['end',os.getpid(),time.time()])+'\\n'); f.close()"]
    helpers=[]
    try:
        for _ in range(2):
            helpers.append(subprocess.Popen([str(ROOT/"agents"),"_gpu",str(tmp_path),"codex_1","--"]+command,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True))
        for helper in helpers:
            _,err=helper.communicate(timeout=20);assert helper.returncode==0,err
        running=0;peak=0
        for line in log.read_text().splitlines():
            kind,_,_=json.loads(line);running+=1 if kind=="start" else -1;peak=max(peak,running)
        assert peak==1 and running==0
        assert read(c["gpu_coord_file"])["allocations"]=={}
    finally:
        for helper in helpers:
            if helper.poll() is None:helper.terminate();helper.wait(timeout=5)

@pytest.mark.parametrize('explicit_model_host',[False,True])
def test_model_and_research_proxies_forward_to_distinct_upstreams(explicit_model_host):
    import http.server
    def upstream(label):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body=(label+" "+self.path).encode()
                self.send_response(200);self.send_header("Content-Length",str(len(body)));self.end_headers();self.wfile.write(body)
            def do_CONNECT(self):
                self.connection.sendall(b"HTTP/1.1 200 Connection Established\\r\\n\\r\\n".replace(b"\\r",b"\r").replace(b"\\n",b"\n"))
                data=self.rfile.read(4);self.connection.sendall(label.encode()+data)
            def log_message(self,*a):pass
        server=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        return server
    research=upstream("research");model=upstream("model")
    from urllib.parse import urlsplit
    try:
        model_proxy=f"http://127.0.0.1:{model.server_port}"
        with Router({"model.example","backup.example"},f"http://127.0.0.1:{research.server_port}",
                    None if explicit_model_host else model_proxy,
                    host_proxies={"model.example":model_proxy} if explicit_model_host else {}) as router:
            assert router.upstream('backup.example')==(None if explicit_model_host else model_proxy)
            for host,label in [("web.example","research"),("model.example","model")]:
                s=socket.create_connection(("127.0.0.1",urlsplit(router.url).port))
                s.sendall(f"GET http://{host}/test HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                response=b""
                while data:=s.recv(4096):response+=data
                s.close()
                assert response.endswith((label+" http://"+host+"/test").encode())
            s=socket.create_connection(("127.0.0.1",urlsplit(router.url).port))
            s.sendall(b"CONNECT model.example:443 HTTP/1.1\r\nHost: model.example\r\n\r\n")
            assert b"200 Connection Established" in s.recv(4096)
            s.sendall(b"ping");assert s.recv(4096)==b"modelping";s.close()
    finally:
        for server in [model,research]:server.shutdown();server.server_close()


@pytest.mark.parametrize("disabled", [False, True])
def test_worker_web_mode_matches_service(monkeypatch, disabled):
    import common
    monkeypatch.setattr(common, "fetch_guide", lambda base: "# rStation\n" +
                        ("**Isolated research setting: external web access is disabled.**" if disabled else ""))
    def fetch(base, path):
        assert path == "/api/v1/problems/p"
        return {"category": "open_ended"}
    monkeypatch.setattr(common, "fetch", fetch)
    assert common.problem_info("http://localhost", "p", disabled)["category"] == "open_ended"
    with pytest.raises(ValueError, match="match"):
        common.problem_info("http://localhost", "p", not disabled)


def test_disabled_mode_rejects_research_proxy():
    args=parser().parse_args(["start","p","--disable-web","--research-proxy","http://localhost:1234"])
    with pytest.raises(ValueError, match="research-proxy"):
        build_config(args,{"category":"open_ended"})


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("disabled", [False, True])
@pytest.mark.parametrize("resuming", [False, True])
def test_web_flags_survive_fresh_and_resumed_launch(tmp_path,kind,disabled,resuming):
    c=cfg(tmp_path,kind);c["disable_web"]=disabled
    save(tmp_path/"run.json",c)
    assert read(tmp_path/"run.json")["disable_web"] is disabled
    manager=ResearchManager(tmp_path,c,env(tmp_path,kind))
    name=kind+"_1"
    decision=CliJobLaunchDecision("resume" if resuming else "fresh",1,0,"saved-session" if resuming else None,0)
    try:
        claimed=manager._claim_cli_job_launch(name,"launch_test",decision)
        spec=manager._build_cli_job_launch_spec(name,"launch_test",decision,claimed)
        prepared=manager._prepare_cli_job_launch(spec,decision,manager._get_cli_job_backend)
        command=prepared.command
        if kind=="codex":
            assert ("--search" in command) is not disabled
            assert ('web_search="disabled"' in command) is disabled
            assert ('features.apps=false' in command) is disabled
            assert ('mcp_servers={}' in command) is disabled
        else:
            assert "--disallowedTools" in command
            assert "Agent" in command and "Task" in command
            assert ("WebSearch" in command) is disabled
            assert ("WebFetch" in command) is disabled
        if resuming:assert "saved-session" in command
        prompt=researcher_instructions(c,c["workers"][0],tmp_path/name)
        assert ("Do not use external websites" in prompt) is disabled
        assert "For external web access" not in prompt
    finally:manager.close()

@pytest.mark.parametrize("kind",["codex","claude"])
@pytest.mark.parametrize("proxy_mode",["direct","none","research","model","route-change"])
def test_launch_proxy_only_when_routing_is_needed(tmp_path,kind,proxy_mode):
    c=cfg(tmp_path,kind);e=env(tmp_path,kind)
    for key in list(e):
        if "PROXY" in key.upper():e.pop(key)
    if proxy_mode=="direct":
        c["model_api_proxy"]="direct"
        e.update(HTTPS_PROXY="http://unused:8119",ALL_PROXY="http://unused:8120")
    elif proxy_mode=="research":c["research_proxy"]="http://research:8118"
    elif proxy_mode=="model":c["model_api_proxy"]="http://model:8119"
    elif proxy_mode=="route-change":c["model_api_proxy"]="http://model:8119"
    m=ResearchManager(tmp_path,c,e);name=kind+"_1"
    try:
        d=CliJobLaunchDecision("resume",1,0,"existing",0)
        spec=m._build_cli_job_launch_spec(name,"launch_test",d,m.states[name])
        if proxy_mode in ("direct","none"):
            assert not m.routers
            assert not any(k in spec.env for k in ("HTTP_PROXY","HTTPS_PROXY","http_proxy","https_proxy","ALL_PROXY","all_proxy"))
        else:
            router=m.routers[name]
            assert spec.env["HTTPS_PROXY"]==router.url
            if proxy_mode=="research":assert router.upstream("127.0.0.1") is None
            if proxy_mode=="route-change":
                c["model_api_proxy"]="direct"
                spec=m._build_cli_job_launch_spec(name,"launch_direct",d,m.states[name])
                assert "HTTPS_PROXY" not in spec.env
    finally:m.close()


@pytest.mark.parametrize('kind,role',[('codex','CODEX'),('claude','CLAUDE')])
def test_dedicated_worker_routes_do_not_inherit_station(kind,role):
    env={
        'CODEX_ADVANCED_API_KEY':'station-astra','CODEX_ADVANCED_BASE_URL':'https://station.example/v1',
        'BACKUP_CODEX_ADVANCED_API_KEY':'station-backup','BACKUP_CODEX_ADVANCED_BASE_URL':'https://station-backup.example/v1',
        'ANTHROPIC_API_KEY':'station-claude','ANTHROPIC_BASE_URL':'https://station.example',
        'BACKUP_ANTHROPIC_API_KEY':'station-claude-backup',
        'CLAUDE_CODE_API_KEY':'station-claude-alias','CLAUDE_CODE_BASE_URL':'https://alias.example',
        f'RSTATION_WORKER_{role}_API_KEY':'worker-primary',
        f'RSTATION_WORKER_{role}_BASE_URL':'https://worker.example',
        f'RSTATION_WORKER_{role}_BACKUP_API_KEY':'worker-backup',
        f'RSTATION_WORKER_{role}_BACKUP_BASE_URL':'https://worker-backup.example',
    }
    assert [r['api_key'] for r in routes(kind,env)]==['worker-primary','worker-backup']
    del env[f'RSTATION_WORKER_{role}_BACKUP_API_KEY']
    del env[f'RSTATION_WORKER_{role}_BACKUP_BASE_URL']
    assert [r['api_key'] for r in routes(kind,env)]==['worker-primary']
    del env[f'RSTATION_WORKER_{role}_API_KEY']
    with pytest.raises(ValueError):routes(kind,env)


@pytest.mark.parametrize("kind", ["codex", "claude"])
@pytest.mark.parametrize("legacy", ["default", "missing", "null"])
@pytest.mark.parametrize("route_index", [0, 1])
def test_default_direct_model_keeps_research_proxy_on_resume(tmp_path, kind, legacy, route_index):
    c=cfg(tmp_path,kind);e=env(tmp_path,kind)
    if legacy=="missing":c.pop("model_api_proxy")
    elif legacy=="null":c["model_api_proxy"]=None
    c["research_proxy"]="http://research.example:8118"
    role="RSTATION_WORKER_"+kind.upper()
    e.update(HTTPS_PROXY="http://inherited.example:8119",ALL_PROXY="http://inherited.example:8120")
    e.update({role+"_BASE_URL":"https://model.example/v1",
              role+"_HTTPS_PROXY":"http://profile.example:8119",
              role+"_BACKUP_API_KEY":"test-backup",
              role+"_BACKUP_BASE_URL":"https://backup.example/v1",
              role+"_BACKUP_HTTPS_PROXY":"http://backup-proxy.example:8119"})
    m=ResearchManager(tmp_path,c,e,probe=True);name=kind+"_1"
    try:
        d=CliJobLaunchDecision("resume",1,0,"existing",route_index*2)
        spec=m._build_cli_job_launch_spec(name,"launch_test",d,m.states[name])
        router=m.routers[name]
        assert spec.env["HTTPS_PROXY"]==router.url
        assert router.upstream("model.example") is None
        assert router.upstream("backup.example") is None
        assert router.upstream("downloads.example")==c["research_proxy"]
        assert {"model.example","backup.example"}<=set(spec.env["NO_PROXY"].split(","))
        assert spec.env["NO_PROXY"]==spec.env["no_proxy"]
        assert "ALL_PROXY" not in spec.env
    finally:m.close()


@pytest.mark.parametrize("source,failed,expected",[
    ("assistant",True,True), ("assistant",False,False),
    ("user",True,False), ("tool",True,False), ("quote",True,False),
    ("transient",True,False), ("historical",True,False), ("policy_only",True,False),
])
def test_claude_model_refusal_requires_failed_turn(tmp_path,source,failed,expected):
    from types import SimpleNamespace
    m=ResearchManager(tmp_path,cfg(tmp_path,"claude"),env(tmp_path,"claude"))
    text="The selected model cannot continue this conversation. Please select a different model, or start a new conversation."
    event={"type":"assistant","message":{"content":[{"type":"text","text":text}]}}
    if source=="user":event["type"]="user"
    if source=="tool":event={"type":"user","message":{"content":[{"type":"tool_result","content":text}]}}
    if source=="quote":event["message"]["content"][0]["text"]="The operator reported: "+text
    if source=="policy_only":event["message"]["content"][0]["text"]="Another provider notice."
    events=[event,{"type":"result","subtype":"success","is_error":failed,"stop_reason":"refusal",
                   "result":"API Error: Claude Code is unable to respond to this request, which appears to violate our Usage Policy."}]
    if source in ("user","tool","quote","policy_only"):events[-1]["stop_reason"]=None
    if source=="transient":events[-1].update(result="HTTP 429",stop_reason=None)
    if source=="historical":events += [{"type":"result","is_error":False,"result":"Research continued."},
                                      {"type":"result","is_error":True,"result":"HTTP 503"}]
    # A later transport error must not turn a successfully completed discussion
    # of this message into an unavailable conversation.
    if not failed:events.append({"type":"result","is_error":True,"result":"HTTP 503"})
    path=tmp_path/"events.jsonl";path.write_text("\n".join(map(json.dumps,events))+"\n")
    session=SimpleNamespace(backend="claude",stderr_path=str(tmp_path/"stderr"),transcript_path=str(path),transcript_idle_timeout_reason=None)
    try:
        m.states["claude_1"].update(token="retained",spawn=1)
        failure=m._classify_cli_job_failure("claude_1",session)
        assert (failure.category=="model_refusal")==expected
        m._schedule_cli_job_fresh_attempt("claude_1",session,failure)
        assert m.states["claude_1"]["token"]=="retained"
        assert (m.states["claude_1"].get("blocked_reason")=="model_refusal")==expected
    finally:m.close()


def test_claude_refused_conversation_remains_blocked_after_controller_restart(tmp_path):
    c=cfg(tmp_path,"claude");save(tmp_path/"run.json",c);prepare_workspaces(tmp_path,c)
    environment=env(tmp_path,"claude");script=Path(environment["CLAUDE_BIN_PATH"])
    script.write_text("#!"+sys.executable+"\n"+'''import json,sys,pathlib
resuming="--resume" in sys.argv
session="unusable-session" if resuming else "fresh-session"
print(json.dumps({"type":"system","subtype":"init","session_id":session}),flush=True)
if resuming:
 print(json.dumps({"type":"assistant","message":{"content":[{"type":"text","text":"The selected model cannot continue this conversation. Please select a different model, or start a new conversation."}]}}),flush=True)
else:
 pathlib.Path("research-recovered").write_text(pathlib.Path("CONTINUATION.md").read_text())
print(json.dumps({"type":"result","subtype":"success","is_error":resuming,"stop_reason":"refusal" if resuming else "end_turn","session_id":session,"result":"API Error: unable to respond" if resuming else "Research continues"}),flush=True)
''')
    notes=tmp_path/"claude_1/CONTINUATION.md";notes.write_text("Preserved research plan and findings.")
    m=ResearchManager(tmp_path,c,environment)
    try:
        m.states["claude_1"].update(token="unusable-session",spawn=1)
        assert m.launch_cli_job("claude_1");drain(m)
        assert m.states["claude_1"].get("blocked_reason")=="model_refusal"
        assert m.states["claude_1"]["token"]=="unusable-session"
    finally:m.close()
    m=ResearchManager(tmp_path,c,environment)
    try:
        assert m.states["claude_1"]["token"]=="unusable-session"
        assert m.states["claude_1"]["blocked_reason"]=="model_refusal"
        m.states["claude_1"]["next"]=0
        assert not m.launch_cli_job("claude_1")
        assert not (tmp_path/"claude_1/research-recovered").exists()
        assert notes.read_text()=="Preserved research plan and findings."
        assert (tmp_path/"claude_1/.runtime/launch_000001/transcript.jsonl").exists()
    finally:m.close()

"""Live CLI recovery trial. No research account or publication is created."""
from dataclasses import replace
import json
import os
from pathlib import Path
import signal
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from common import ROOT,save
from main import parser,build_config,prepare_workspaces
from recovery import ResearchManager

ROOT_OUT=Path("/tmp/rstation-private-worker-live")
ROOT_OUT.mkdir(exist_ok=True)
class Trial(ResearchManager):
    prompt_override=""
    def _build_cli_job_launch_spec(self,*a,**kw):
        return replace(super()._build_cli_job_launch_spec(*a,**kw),prompt=self.prompt_override)

def wait(manager,name,seconds=300):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        manager.poll_cli_jobs()
        if not manager.active_sessions:
            if manager.states[name]["status"]=="continuing":return
            manager.launch_cli_job(name)
        time.sleep(.2)
    raise RuntimeError("Trial exceeded its bounded test window.")
def text(manager,name):
    s=manager.states[name]
    p=manager.run/name/".runtime"/f"launch_{s['launches']:06d}"/"transcript.jsonl"
    outputs=[]
    for line in p.read_text().splitlines():
        try:e=json.loads(line)
        except ValueError:continue
        if e.get("type")=="item.completed" and e.get("item",{}).get("type")=="agent_message":outputs.append(e["item"].get("text",""))
        if e.get("event")=="result":outputs.append(e.get("result",{}).get("response",""))
        if e.get("type")=="assistant":
            outputs.extend(p.get("text","") for p in e.get("message",{}).get("content",[]) if p.get("type")=="text")
    return "\n".join(outputs)
results={}
for kind in sys.argv[1:] or ["claude","codex"]:
    directory=ROOT_OUT/(kind+"-"+str(int(time.time())))
    a=parser().parse_args(["start","test_problem","--name","live_recovery","--codex",str(int(kind=="codex")),"--claude",str(int(kind=="claude")),"--gemini",str(int(kind=="gemini"))])
    c=build_config(a,{"category":"open_ended"})
    save(directory/"run.json",c);prepare_workspaces(directory,c)
    m=Trial(directory,c,dict(os.environ));name=kind+"_1"
    codeword="silver apricot telescope"
    try:
        m.prompt_override=f"Private CLI recovery test; do not register with or contact rStation. Remember this exact phrase in our conversation: {codeword}. Write the text persistent-proof into a file named continuity.txt in your working directory. Then reply READY."
        assert m.launch_cli_job(name);wait(m,name)
        token=m.states[name]["token"]
        assert token and (directory/name/"continuity.txt").read_text().strip()=="persistent-proof"
        print(kind+": initial tool use and session persistence passed",flush=True)
        m.prompt_override="Without reading any files, what exact phrase did I ask you to remember? Reply with that phrase only."
        m.states[name]["next"]=0
        assert m.launch_cli_job(name);wait(m,name)
        assert codeword in text(m,name).lower()
        assert m.states[name]["token"]==token
        print(kind+": same-conversation resume recalled hidden phrase",flush=True)
        m.prompt_override="Now use your shell tool to execute: touch interruption_started; sleep 120. This is an operator interruption test. Do not contact any service."
        m.states[name]["next"]=0
        assert m.launch_cli_job(name)
        deadline=time.monotonic()+120
        while not (directory/name/"interruption_started").exists() and time.monotonic()<deadline:
            m.poll_cli_jobs();time.sleep(.2)
        assert (directory/name/"interruption_started").exists(),"No tool activity before interruption."
        m._abort_cli_job_process(m.active_sessions[name])
        m.poll_cli_jobs()
        assert not m.active_sessions
        assert m.states[name]["token"]==token
        m.close()
        # A new controller instance loads the existing session/workspace.
        m=Trial(directory,c,dict(os.environ))
        m.prompt_override="The interruption test is over. Without opening any files, repeat the exact phrase I asked you to remember at the beginning. Do not run sleep again."
        m.states[name]["next"]=0
        assert m.launch_cli_job(name);wait(m,name)
        assert m.states[name]["token"]==token
        assert codeword in text(m,name).lower()
        assert (directory/name/"continuity.txt").read_text().strip()=="persistent-proof"
        print(kind+": interrupted process and new controller resumed same session and files",flush=True)
        results[kind]={"passed":True,"directory":str(directory),"same_session":True,"file_preserved":True,"interruption_recovery":True}
    except BaseException as exc:
        results[kind]={"passed":False,"directory":str(directory),"error":type(exc).__name__,"detail":str(exc)}
        print(kind+": FAILED "+type(exc).__name__+": "+str(exc),flush=True)
    finally:
        m.close();save(ROOT_OUT/"results.json",results)
if not all(v["passed"] for v in results.values()):raise SystemExit(1)

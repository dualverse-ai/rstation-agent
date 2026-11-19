"""Read-only worker monitoring for operators and remote audits."""
from collections import deque
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from common import read, alive, NAME

def snapshot(run):
    config=read(run/"run.json")
    if not config:return None
    controller=read(run/"controller.json",{})
    active=alive(controller.get("pid"),controller.get("token"))
    now=time.time()
    workers=[]
    for worker in config["workers"]:
        state=read(run/worker["name"]/".runtime/state.json",{})
        live=alive(state.get("pid"),state.get("pid_token"))
        row={"worker":worker["name"],"model":worker["model"],"state":state.get("status","not started") if active else "stopped",
             "process_alive":live,"provider_route":state.get("route_index",0)+1,"retries":state.get("retry_total",0),
             "launches":state.get("launches",0),"workspace_gb":round(state.get("workspace_bytes",0)/1e9,3),
             "storage_budget_gb":config["storage_gb"],"gpu_reservations":[]}
        for field in ("last_response","last_tool_completed","last_stream_event","last_success"):
            value=state.get(field)
            row[field]=datetime.fromtimestamp(value,timezone.utc).isoformat() if value else None
            row[field+"_age_seconds"]=round(now-value,1) if value else None
        row["last_response_excerpt"]=state.get("last_response_excerpt")
        row["retry_in_seconds"]=max(0,round(state.get("next",0)-now))
        supervision=read(run/worker['name']/'.runtime/supervision.json',{})
        if supervision.get('database'):
            row['supervision_group']=Path(supervision['database']).parent.name
        wait=read(run/worker['name']/'.runtime/supervisor-wait.json',{}) if config.get('role')=='supervisor' else {}
        if live and wait.get('until',0)>now:
            row['state']='sleeping'
            row['wake_at']=datetime.fromtimestamp(wait['until'],timezone.utc).isoformat()
        if active and not live and row["state"]=="running":row["state"]="recovering"
        if active and live and row["last_stream_event_age_seconds"] is not None and row["last_stream_event_age_seconds"]>config.get("idle_seconds",1800):
            row["attention"]="No recent transcript activity; inspect logs and running experiments."
        for path in (run/worker["name"]/".runtime/gpu-jobs").glob("*.json"):
            item=read(path)
            if item and alive(item["helper_pid"],item["helper_token"]):row["gpu_reservations"].extend(item["gpus"])
        workers.append(row)
    result={"run":config["name"],"problem":config["problem"],"controller_alive":active,
            "controller_heartbeat_age_seconds":round(now-controller["heartbeat"],1) if controller.get("heartbeat") else None,
            "workers":workers}
    if config.get('role')=='supervisor':
        result.update(role='supervisor',supervision_group=config['supervision_group'])
    from capabilities import health
    result["helpers"]={name:health(run,name) for name in ("coder","council")}
    result["helper_errors"]=read(run/"helpers-health.json",{}).get("errors",{})
    return result

def age(seconds):
    if seconds is None:return "never"
    if seconds<60:return f"{seconds:.0f}s"
    if seconds<3600:return f"{seconds/60:.0f}m"
    return f"{seconds/3600:.1f}h"

def show_status(runs,as_json=False):
    records=[r for run in runs if (r:=snapshot(Path(run)))]
    if as_json:
        print(json.dumps(records,indent=2));return
    for record in records:
        print(f"{record['run']} — {'active' if record['controller_alive'] else 'stopped'}")
        print(f"{'Worker':12} {'Model':22} {'State':12} {'Response':>9} {'Tool':>7} {'Retries':>7} {'Disk GB':>9} GPU")
        for w in record["workers"]:
            print(f"{w['worker']:12} {w['model']:22} {w['state']:12} {age(w['last_response_age_seconds']):>9} {age(w['last_tool_completed_age_seconds']):>7} {w['retries']:>7} {w['workspace_gb']:>9.2f} {w['gpu_reservations']}")
            if w.get("attention"):print("  "+w["attention"])
    if not records:print("No runs found.")

def show_logs(run,worker,lines):
    if not NAME.fullmatch(worker):raise ValueError("Invalid worker name.")
    config=read(run/"run.json")
    if not config or worker not in {w["name"] for w in config["workers"]}:raise ValueError("Worker not found.")
    if not 1<=lines<=10000:raise ValueError("Choose between 1 and 10000 log lines.")
    with (run/worker/".runtime/console.log").open() as f:
        print("".join(deque(f,maxlen=lines)),end="")

"""Per-experiment GPU reservations using Station's unchanged coordinator."""
import argparse
import contextlib
import ctypes
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from common import ROOT, lock, read, save, alive, pid_token
from station.eval_research.gpu_coordinator import GPUCoordinator

def child_guard():
    # The kernel also stops the experiment's main process if its helper dies.
    parent = os.getppid()
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)
    if os.getppid() != parent: os.kill(os.getpid(), signal.SIGKILL)

def stop_group(pid):
    try: os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError: return
    time.sleep(0.2)
    try: os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError: pass

def reserve_slots(directory, maximum, count):
    # All concurrent helpers for this agent share these locks. Acquire the
    # whole request atomically, releasing partial acquisitions immediately.
    slots = []
    for i in range(maximum):
        try: slots.append(lock(Path(directory)/f"gpu-slot-{i}.lock", True))
        except BlockingIOError: continue
        if len(slots) == count: return slots
    for item in slots: item.close()
    return []

def run_gpu(run, worker_name, count, command, timeout=None):
    config=read(Path(run)/"run.json")
    worker=next(w for w in config["workers"] if w["name"] == worker_name)
    maximum=config["gpu_per_agent"]
    if not worker["gpus"]: raise ValueError("GPU access is disabled for this worker.")
    if not 1 <= count <= min(maximum,len(worker["gpus"])):
        raise ValueError("GPU request exceeds this worker's configured limit or available pool.")
    if not command: raise ValueError("Supply a command after --.")
    timeout=config["gpu_job_seconds"] if timeout is None else timeout
    if timeout <= 0 or timeout > config["gpu_job_seconds"]:
        raise ValueError("GPU timeout must be positive and no larger than the configured experiment limit.")
    runtime=Path(run)/worker_name/".runtime"
    runtime.mkdir(parents=True,exist_ok=True)
    job=f"{worker_name}-{os.getpid()}-{time.time_ns()}"
    # Never reuse a station_id across live helpers: coordinator construction
    # clears that owner's allocations.
    owner="rstation-"+config["name"]+"-"+job
    coordinator=GPUCoordinator(config["gpu_coord_file"],worker["gpus"],owner,max(2*timeout,300))
    allocated=None; slots=[]; child=None; stopped=False
    def halt(*_):
        nonlocal stopped
        stopped=True
    old={sig:signal.signal(sig,halt) for sig in (signal.SIGINT,signal.SIGTERM)}
    jobfile=runtime/"gpu-jobs"/(job+".json")
    try:
        print("Waiting for a shared GPU...",flush=True)
        while not stopped:
            from restart_timer import gpu_start_allowed
            if (runtime/"PAUSE").exists() or not gpu_start_allowed(runtime.parent):return 130
            slots=reserve_slots(runtime,maximum,count)
            if slots:
                allocated=coordinator.allocate(job,count)
                if allocated: break
                for slot in slots: slot.close()
                slots=[]
            time.sleep(1)
        if stopped or (runtime/"PAUSE").exists() or not gpu_start_allowed(runtime.parent): return 130
        env=dict(os.environ,CUDA_VISIBLE_DEVICES=",".join(map(str,allocated)))
        print(f"Reserved GPU(s) {allocated}; releasing automatically when the command exits.",flush=True)
        child=subprocess.Popen(command,env=env,start_new_session=True,preexec_fn=child_guard)
        save(jobfile,{"owner":owner,"job":job,"helper_pid":os.getpid(),"helper_token":pid_token(os.getpid()),
                      "pid":child.pid,"token":pid_token(child.pid),"gpus":allocated})
        deadline=time.monotonic()+timeout
        while child.poll() is None and not stopped and time.monotonic()<deadline: time.sleep(.2)
        timed_out=child.poll() is None and not stopped
        stop_group(child.pid)  # Includes children left behind after the main process exits.
        code=child.wait()
        return 130 if stopped else 124 if timed_out else code
    finally:
        if child and child.poll() is None:
            stop_group(child.pid);child.wait()
        if allocated: coordinator.deallocate(job)
        jobfile.unlink(missing_ok=True)
        for slot in slots: slot.close()
        for sig,handler in old.items(): signal.signal(sig,handler)

def reap_orphan_gpu_jobs(run, worker_name, stop_all=False):
    # Recovery after a helper crash: kill the experiment before releasing its
    # shared reservation. PID start tokens avoid signaling a recycled PID.
    config=read(Path(run)/"run.json")
    directory=Path(run)/worker_name/".runtime/gpu-jobs"
    for path in directory.glob("*.json"):
        job=read(path)
        if not job:continue
        if not stop_all and alive(job["helper_pid"],job["helper_token"]): continue
        if alive(job["pid"],job["token"]): stop_group(job["pid"])
        if stop_all and alive(job["helper_pid"],job["helper_token"]):
            os.kill(job["helper_pid"],signal.SIGTERM)
            continue  # The live helper releases its own reservation.
        # Constructor safely clears this dead job's uniquely owned allocation.
        GPUCoordinator(config["gpu_coord_file"],[0],job["owner"],300)
        path.unlink(missing_ok=True)

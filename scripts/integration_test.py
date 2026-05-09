#!/usr/bin/env python3
"""
Integration test for Isogrid ML workloads in DRY_RUN mode.

This script:
1. Starts the orchestrator in a background thread
2. Starts a worker in DRY_RUN mode with mock executors
3. Submits an ML job (Stable Diffusion) using the CLI
4. Polls job status until complete
5. Downloads the (mock) result
6. Cleans up

Usage:
  python scripts/integration_test.py

Requirements:
  - Python 3.10+
  - All dependencies from requirements.txt

Environment:
  - ORCHESTRATOR_API_KEY optional but recommended
  - ORCHESTRATOR_URL defaults to http://localhost:8000
"""

import os
import sys
import time
import json
import subprocess
import requests
import threading
from pathlib import Path
from datetime import datetime

ORCH_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8000").rstrip("/")
API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")

def headers():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h

def api_health():
    try:
        r = requests.get(f"{ORCH_URL}/health", headers=headers(), timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def start_orchestrator():
    """Start orchestrator in background."""
    print("[test] Starting orchestrator...")
    env = os.environ.copy()
    env["DRY_RUN"] = "false"
    proc = subprocess.Popen(
        [sys.executable, "backend/orchestrator.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    time.sleep(2)
    if not api_health():
        print("[ERROR] Orchestrator failed to start!")
        sys.exit(1)
    print("[test] Orchestrator ready")
    return proc

def start_worker():
    """Start worker in DRY_RUN with mock executors."""
    print("[test] Starting worker (DRY_RUN + mock executors)...")
    env = os.environ.copy()
    env["DRY_RUN"] = "true"
    env["USE_LOCAL_EXECUTORS"] = "true"
    env["DRY_RUN_GPU_VRAM_MB"] = "16384"
    env["ORCHESTRATOR_URL"] = ORCH_URL
    if API_KEY:
        env["ORCHESTRATOR_API_KEY"] = API_KEY
    proc = subprocess.Popen(
        [sys.executable, "workers/node_agent.py"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    time.sleep(2)
    print("[test] Worker ready")
    return proc

def upload_test_file():
    """Upload a dummy test file."""
    print("[test] Uploading test file...")
    test_file = Path("/tmp/test_input.txt")
    test_file.write_text("test input for sd job\n")
    
    with test_file.open("rb") as f:
        r = requests.post(
            f"{ORCH_URL}/upload",
            files={"file": (test_file.name, f, "text/plain")},
            headers=headers(),
            timeout=30,
        )
    r.raise_for_status()
    result = r.json()
    print(f"[test] Upload complete: {result['file_name']}, token={result['upload_token'][:12]}...")
    return result

def submit_ml_job(upload_result):
    """Submit a Stable Diffusion test job."""
    print("[test] Submitting Stable Diffusion job...")
    payload = {
        "file_name": upload_result["file_name"],
        "upload_token": upload_result["upload_token"],
        "job_kind": "stable_diffusion",
        "model_name": "test-sd-v1",
        "gpu_vram_required": 4096,
        "executor_image": "mock:sd",
        "executor_command": "sd_mock.py",
        "executor_args": [],
        "frame_start": 1,
        "frame_end": 1,
        "chunk_size": 1,
        "replication_factor": 1,
    }
    r = requests.post(
        f"{ORCH_URL}/jobs",
        json=payload,
        headers=headers(),
        timeout=30,
    )
    r.raise_for_status()
    job = r.json()
    print(f"[test] Job created: {job['job_id'][:12]}...")
    return job

def poll_job(job_id, timeout_sec=60):
    """Poll job status until complete or timeout."""
    print(f"[test] Polling job {job_id[:12]}... (timeout={timeout_sec}s)")
    start = time.time()
    while time.time() - start < timeout_sec:
        r = requests.get(
            f"{ORCH_URL}/jobs/{job_id}",
            headers=headers(),
            timeout=10,
        )
        r.raise_for_status()
        job = r.json()
        status = job["status"]
        tasks = job.get("tasks", [])
        complete_tasks = sum(1 for t in tasks if t["status"] == "complete")
        print(f"[test]   status={status}, tasks={complete_tasks}/{len(tasks)}")
        
        if status in ("complete", "failed"):
            return job
        time.sleep(2)
    
    raise TimeoutError(f"Job {job_id} did not complete within {timeout_sec}s")

def main():
    print("=" * 60)
    print("Isogrid ML Integration Test (DRY_RUN)")
    print("=" * 60)
    
    # Ensure directories exist
    Path("node_data/outputs").mkdir(parents=True, exist_ok=True)
    Path("model_cache").mkdir(parents=True, exist_ok=True)
    
    orch_proc = None
    worker_proc = None
    
    try:
        # Start services
        orch_proc = start_orchestrator()
        worker_proc = start_worker()
        
        # Upload test file
        upload_result = upload_test_file()
        
        # Submit ML job
        job = submit_ml_job(upload_result)
        
        # Poll until complete
        final_job = poll_job(job["job_id"], timeout_sec=30)
        
        if final_job["status"] == "complete":
            print("\n" + "=" * 60)
            print("[SUCCESS] Integration test passed!")
            print("=" * 60)
            print(f"Job {final_job['job_id'][:12]}... completed successfully")
            print(f"Status: {final_job['status']}")
            return 0
        else:
            print(f"\n[FAILED] Job ended with status: {final_job['status']}")
            return 1
    
    except Exception as exc:
        print(f"\n[ERROR] {exc}")
        import traceback
        traceback.print_exc()
        return 1
    
    finally:
        print("\n[test] Cleaning up...")
        for proc in [worker_proc, orch_proc]:
            if proc:
                try:
                    proc.terminate()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()

if __name__ == "__main__":
    sys.exit(main())

"""Smoke integration script (not executed automatically).

Usage notes:
 - Start the orchestrator: `python backend/orchestrator.py` (or set env and run uvicorn)
 - Start a worker in local-mock mode:
     `USE_LOCAL_EXECUTORS=true python workers/node_agent.py`
 - Use the CLI to upload and submit a job with `--preset sd` or `--preset ffmpeg`.

This helper describes the manual steps and provides an example API call.
"""
import os
import requests

ORCH = os.getenv('ORCHESTRATOR_URL','http://127.0.0.1:8000').rstrip('/')
API_KEY = os.getenv('ORCHESTRATOR_API_KEY','')

def headers():
    h = {'Content-Type':'application/json'}
    if API_KEY:
        h['X-API-Key'] = API_KEY
    return h

def example_submit(file_name, upload_token):
    payload = {
        'file_name': file_name,
        'upload_token': upload_token,
        'job_kind': 'stable_diffusion',
        'executor_image': 'mock:sd',
        'executor_command': 'sd_mock.py',
        'executor_args': [],
        'frame_start': 1,
        'frame_end': 1,
        'chunk_size': 1,
        'replication_factor': 1,
    }
    r = requests.post(f"{ORCH}/jobs", json=payload, headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()

if __name__ == '__main__':
    print('This script is a guide for smoke-testing. Run the orchestrator and a worker in mock mode, then use the CLI or call example_submit.')
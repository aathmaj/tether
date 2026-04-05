

# Tether: Decentralized P2P Compute Fabric

Tether is a decentralized compute coordination layer for Blender rendering jobs. It has two runtime components:

- Orchestrator: a FastAPI control plane that tracks workers, schedules frame chunks, leases tasks, and stores output artifacts.
- Node Agent: a worker daemon that registers with the orchestrator, claims render tasks, executes Blender in Docker, and uploads validated artifacts.

## What Is New (Production ready (new commit by me --> cosmic-hydra))

This repository now includes a production-style orchestration flow instead of a one-shot MVP script.

- Typed API contracts using Pydantic models.
- Worker lifecycle management (register, heartbeat, stale detection).
- Chunk-based task scheduler with priority ordering.
- Lease-based execution to recover from crashed workers.
- Persistent orchestrator state using SQLite + WAL mode.
- Task failure API with bounded retries and terminal failure state.
- SHA256 integrity checks for input jobs and uploaded artifacts.
- API key authentication for worker control paths.
- Long-running resilient node agent loop with retry/backoff networking.
- Deterministic artifact naming for traceability.
- Optional DRY_RUN mode for local testing without Blender/Docker.
- Optional ngrok tunneling for public worker connectivity.
- Runtime fleet and queue counters via /metrics.

## Repository Layout

- orchestrator.py: FastAPI scheduler and control plane.
- node_agent.py: worker daemon and Blender execution client.
- jobs/: input .blend files mounted and served by orchestrator.
- results/: uploaded task artifacts (.zip per task).

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Copy and edit environment values:

```bash
cp .env.example .env
```

Common settings:

- ORCHESTRATOR_URL: worker target URL, defaults to http://127.0.0.1:8000
- ORCHESTRATOR_API_KEY: shared token for worker auth (recommended)
- ENABLE_NGROK: true/false, enables public tunnel in orchestrator
- NGROK_AUTH_TOKEN: required when ENABLE_NGROK=true
- DRY_RUN: true/false, lets worker simulate rendering without Docker

### 3. Start orchestrator

```bash
python orchestrator.py
```

### 4. Add a .blend job input

Place file(s) in jobs/, for example:

```bash
cp /path/to/scene.blend jobs/
```

### 5. Create a scheduled job

```bash
curl -X POST http://127.0.0.1:8000/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "file_name": "scene.blend",
    "frame_start": 1,
    "frame_end": 120,
    "chunk_size": 10,
    "priority": 100
  }'
```

### 6. Start one or more workers

```bash
python node_agent.py
```

Workers continuously:

- register and heartbeat,
- claim queued chunks,
- render chunk frames,
- zip and upload output artifact,
- repeat until queue is empty.

## API Overview

- GET /health: service health probe.
- POST /jobs: create a new chunked render job.
- GET /jobs: list all jobs and task states.
- GET /jobs/{job_id}: inspect one job.
- GET /download_job/{filename}: fetch blend file.
- POST /workers/register: register worker (auth protected).
- POST /workers/heartbeat: heartbeat and activity updates (auth protected).
- POST /jobs/claim: claim next queued task (auth protected).
- POST /jobs/{job_id}/tasks/{task_id}/artifact: upload task zip + checksum (auth protected).
- POST /jobs/{job_id}/tasks/{task_id}/fail: report task failure and trigger retry/fail policy.
- GET /metrics: aggregated counters for jobs, tasks, and worker states.

## Security and Integrity

- Worker endpoints can require X-API-Key if ORCHESTRATOR_API_KEY is set.
- Job input files are checksummed and validated on worker download.
- Task artifacts are verified by SHA256 before acceptance.
- Leases expire automatically to requeue stuck tasks.
- Task retries are capped by MAX_TASK_ATTEMPTS to avoid infinite flapping.

## Persistence

- State is persisted to SQLite (default: ./orchestrator_state.db).
- Jobs and workers survive orchestrator restarts.
- WAL mode is enabled for safer concurrent read/write behavior.
- You can change the file location using STATE_DB_PATH.

## Local Development Tips

- Set DRY_RUN=true in worker env to test full scheduling flow without Docker/Blender.
- Tune TASK_LEASE_SECONDS for slower/longer renders.
- Tune POLL_INTERVAL_SECONDS and HEARTBEAT_INTERVAL_SECONDS for lower orchestrator load.
- Tune MAX_TASK_ATTEMPTS to control retry aggressiveness for unstable workloads.

## Next Roadmap Ideas

- Persistent DB state (PostgreSQL/SQLite) and restart-safe scheduler.
- Web dashboard for queue monitoring and worker fleet insights.
- Multi-tenant auth with per-tenant quotas and billing.
- Redundant verification runs for anti-cheat and confidence scoring.
- Plugin integration for direct Blender submit and status sync.

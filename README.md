# Tether: Decentralized P2P Compute Fabric

Tether is a decentralized compute coordination layer for Blender rendering jobs. It captures spare CPU/GPU headroom on participating machines and coordinates chunked rendering across a fleet.

It has two core runtime components:

- Orchestrator: FastAPI control plane for workers, queueing, task leases, artifact validation, and final assembly.
- Node Agent: worker daemon that registers, claims tasks, renders in Docker Blender, and uploads validated artifacts.

Additional user-facing surfaces:

- CLI client for upload/submit/status/watch/download workflows.
- Blender add-on for one-click submit directly from an open scene.
- HTML dashboard for jobs, workers, and live metrics.

## Features

- Typed API contracts (Pydantic models).
- Worker lifecycle: register, heartbeat, stale detection.
- Priority scheduler with chunked frame tasks.
- Lease-based execution with automatic requeue on expiry.
- Retry/fail policy with `MAX_TASK_ATTEMPTS`.
- Artifact integrity checks (SHA256).
- Input file checksum validation on workers.
- Optional API key auth for control/worker endpoints.
- Strict upload-to-submit flow: jobs require a valid `/upload` token (manual file drops in `jobs/` are rejected).
- Persistent state in SQLite with WAL mode.
- Optional replicated rendering (`replication_factor` 1-3) with verification:
  - Per-frame hash comparison across replicas.
  - Conflict state and automatic tie-breaker replica scheduling.
- Optional stitcher daemon to assemble frames into final MP4 via FFmpeg.
- Optional S3/R2 object storage and presigned URLs.
- Optional ngrok public tunnel for internet-reachable orchestrator.
- Headroom-aware worker claiming and container pause/unpause behavior.
- DRY_RUN mode for local/dev tests without Blender/Docker.
- Upload inventory endpoint: `GET /uploads` shows active upload tokens and expiry.
- Blender add-on quality-of-life controls: copy current job ID, open download URL, optional auto-open result.

## Latest Update

Last version:
- Supported upload and job submission, but files manually dropped into `jobs/` could still be submitted.
- Basic CLI/add-on submit flow without upload-token enforcement.

This version improves:
- Strict upload-only submission by requiring a valid `upload_token` from `/upload` when creating jobs.
- Token expiry and persisted upload records, plus `GET /uploads` visibility.
- CLI updates (`--token` support and `uploads` command) and Blender add-on quality-of-life controls.
- Railway deployment readiness with `Procfile`, `railway.json`, and cloud rollout instructions.

## Repository Layout

- `orchestrator.py`: FastAPI scheduler/control plane + stitcher + persistence.
- `node_agent.py`: worker runtime and Blender execution flow.
- `tether_cli.py`: operator/consumer command line client.
- `blender_addon.py`: Blender plugin for submit + status polling.
- `dashboard.html`: browser dashboard for fleet and queue monitoring.
- `jobs/`: uploaded `.blend` inputs.
- `results/`: uploaded task artifacts (`.zip`).
- `finals/`: stitched final videos (`.mp4`, created at runtime).
- `staging/`: temporary stitcher workspace (created at runtime).

## Dependency Matrix

### System prerequisites

- Python 3.10+ (3.11 recommended)
- pip
- Docker Desktop / Docker Engine (worker rendering, unless `DRY_RUN=true`)
- FFmpeg (required for final MP4 stitching when `ENABLE_STITCHER=true`)
- Optional NVIDIA stack for GPU nodes (driver + NVIDIA container runtime)

### Exact Python packages used by this repo

The following packages are required by the current code paths and are included in `requirements.txt`:

- `fastapi`
- `uvicorn`
- `python-dotenv`
- `requests`
- `python-multipart`
- `urllib3`
- `ngrok`
- `typer`
- `rich`
- `psutil`
- `pynvml`
- `boto3`
- `botocore`

Equivalent explicit install command:

```bash
pip install fastapi uvicorn python-dotenv requests python-multipart urllib3 ngrok typer rich psutil pynvml boto3 botocore
```

### Component-specific dependency notes

- Orchestrator needs FastAPI stack and optional `ngrok`, `boto3/botocore`, FFmpeg.
- Node Agent needs `requests`, `urllib3`, and optionally `psutil/pynvml` for telemetry.
- Tether CLI needs `typer`, `rich`, `requests`, and `python-dotenv`.
- Blender add-on uses Blender Python plus `requests`.

## Install

### Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Linux/macOS

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### Verify key tooling

```bash
python --version
docker --version
ffmpeg -version
```

If `ffmpeg` is not in PATH, set `FFMPEG_PATH` in `.env` to the full executable path.

### Pull Blender container image (worker nodes)

```bash
docker pull linuxserver/blender:latest
```

## Configuration

Copy `.env.example` to `.env` and edit values.

Quick preset options:

- `.env.local-dev.example`: one-machine or LAN testing.
- `.env.public-production.example`: internet-facing deployment baseline.

Use one of these commands:

Windows PowerShell:

```powershell
Copy-Item .env.local-dev.example .env
```

Linux/macOS:

```bash
cp .env.local-dev.example .env
```

### Shared

- `ORCHESTRATOR_API_KEY`: shared token for worker/control auth.

### Orchestrator settings

- `PORT` (default `8000`)
- `ENABLE_NGROK` (`true`/`false`)
- `NGROK_AUTH_TOKEN`
- `TASK_LEASE_SECONDS`
- `WORKER_STALE_SECONDS`
- `MAX_TASK_ATTEMPTS`
- `STATE_DB_PATH`
- `LOG_LEVEL`
- `FFMPEG_PATH` (default `ffmpeg`)
- `ENABLE_STITCHER` (`true`/`false`)
- `STITCHER_FPS`
- `UPLOAD_TOKEN_TTL_SECONDS` (default `86400`)

Optional S3/R2:

- `S3_ENDPOINT_URL`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_BUCKET_NAME`
- `S3_PRESIGN_EXPIRY`

### Worker settings

- `ORCHESTRATOR_URL` (default `http://127.0.0.1:8000`)
- `WORKER_ID` (optional, auto-generated if blank)
- `BLENDER_IMAGE`
- `LOCAL_DIR`
- `POLL_INTERVAL_SECONDS`
- `HEARTBEAT_INTERVAL_SECONDS`
- `DRY_RUN`

Headroom tuning:

- `CPU_CLAIM_THRESHOLD`
- `GPU_CLAIM_THRESHOLD`
- `CPU_PAUSE_THRESHOLD`
- `GPU_PAUSE_THRESHOLD`
- `HEADROOM_CHECK_INTERVAL`

## Role-Based Quickstart (Copy-Paste)

These flows are separated by role so operators can start only what they need.

### 1) Coordinator Role (orchestrator operator)

Use this when you host the scheduler/control plane.

1. Prepare env from preset:

Windows PowerShell:

```powershell
Copy-Item .env.local-dev.example .env
```

Linux/macOS:

```bash
cp .env.local-dev.example .env
```

2. Start orchestrator:

```bash
python orchestrator.py
```

3. Validate endpoints:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/metrics
```

4. Optional dashboard:

- Open `dashboard.html` and set URL to `http://127.0.0.1:8000`.

### 2) Supplier Role (worker/node provider)

Use this on compute machines that contribute render capacity.

1. Install deps and verify Docker:

```bash
pip install -r requirements.txt
docker --version
docker pull linuxserver/blender:latest
```

2. Set node target and auth in `.env`:

- `ORCHESTRATOR_URL=http://<orchestrator-host>:8000`
- `ORCHESTRATOR_API_KEY=<same key as orchestrator>`

3. Start worker:

```bash
python node_agent.py
```

4. Optional dry-run mode (no Docker/Blender render):

Windows PowerShell:

```powershell
$env:DRY_RUN = "true"
python node_agent.py
```

Linux/macOS:

```bash
DRY_RUN=true python node_agent.py
```

### 3) Buyer Role (job submitter / consumer)

Use this to submit and monitor render jobs.

CLI path:

```bash
python tether_cli.py upload scene.blend
python tether_cli.py submit scene.blend --start 1 --end 120 --chunk 10 --replicas 1
python tether_cli.py watch <job_id>
python tether_cli.py download <job_id>
```

Note: `submit` auto-uploads by default and uses the returned upload token automatically. If you disable auto-upload, pass `--token <upload_token>` from a prior `upload` call.

Blender path:

- Install and enable `blender_addon.py`.
- Set add-on `Orchestrator URL` and `API Key`.
- Submit from Render Properties -> Tether Render Farm panel.

## Public Production Preset Workflow

For public deployment, start from `.env.public-production.example`:

Windows PowerShell:

```powershell
Copy-Item .env.public-production.example .env
```

Linux/macOS:

```bash
cp .env.public-production.example .env
```

Then update these required values before launch:

- `ORCHESTRATOR_API_KEY`
- `ORCHESTRATOR_URL`
- `S3_ENDPOINT_URL`
- `S3_ACCESS_KEY_ID`
- `S3_SECRET_ACCESS_KEY`
- `S3_BUCKET_NAME`

## End-to-End Usage

## 1. Start orchestrator

```bash
python orchestrator.py
```

Health check:

- `GET /health`
- `GET /docs`

## 2. Start one or more workers

```bash
python node_agent.py
```

For local dry test without Docker/Blender:

```bash
DRY_RUN=true python node_agent.py
```

## 3. Submit jobs (three options)

### Option A: CLI

Upload + submit + monitor:

```bash
python tether_cli.py upload scene.blend
python tether_cli.py submit scene.blend --start 1 --end 120 --chunk 10 --replicas 2
python tether_cli.py watch <job_id>
python tether_cli.py download <job_id>
```

No-auto-upload flow:

```bash
python tether_cli.py upload scene.blend
python tether_cli.py submit scene.blend --start 1 --end 120 --no-auto-upload --token <upload_token>
```

Useful CLI commands:

- `python tether_cli.py jobs`
- `python tether_cli.py status <job_id>`
- `python tether_cli.py workers`
- `python tether_cli.py metrics`
- `python tether_cli.py uploads`

### Option B: Blender Add-on (Detailed)

1. Open Blender.
2. Go to Edit -> Preferences -> Add-ons.
3. Click Install, choose `blender_addon.py`, and enable "Tether Render Farm".
4. In add-on preferences, set:
  - `Orchestrator URL` (local URL, LAN URL, or public ngrok/domain URL)
  - `API Key` (must match `ORCHESTRATOR_API_KEY` if auth is enabled)
5. Open Render Properties (camera icon) -> Tether Render Farm panel.
6. Configure job controls:
  - Frame range from scene (`frame_start`, `frame_end`)
  - `chunk_size`
  - `priority`
  - `replication_factor` (1-3)
  - `pack_textures` toggle
  - `stitch_output` toggle
7. Click Test Connection.
8. Click Submit to Tether.
9. Watch live status/progress in Blender (queued/running/stitching/complete/failed).
10. Use Stop Watching to stop polling if needed.
11. Optional: use Copy Job ID / Open Download URL controls in the status box.

How the add-on submits:

- Saves current scene state.
- Optionally packs external textures into a temporary blend copy.
- Uploads the packed blend via `/upload` and captures the upload token.
- Creates the job via `/jobs` using scene frame range, panel settings, and that upload token.
- Polls `/jobs/{job_id}` to update UI progress.

## Upload Flow Rules

- Only assets uploaded via `POST /upload` can be submitted.
- `POST /jobs` requires both `file_name` and a matching `upload_token`.
- Upload tokens expire after `UPLOAD_TOKEN_TTL_SECONDS` (default 24 hours).
- If the uploaded file changes on disk, submission is rejected until re-uploaded.
- Manually dropping `.blend` files into `jobs/` is not a valid submission path.

## Railway Deployment (Cloud Orchestrator)

### 1. Push code to GitHub

```bash
git init
git add .
git commit -m "Tether orchestrator with tokenized upload flow"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

### 2. Create Railway project

1. Sign in to Railway and create a New Project.
2. Choose Deploy from GitHub Repo and select your repository.
3. Railway will detect Python and use `railway.json` / `Procfile` to start `uvicorn orchestrator:app`.
4. Set service Variables in Railway:
  - `ORCHESTRATOR_API_KEY` (required)
  - `UPLOAD_TOKEN_TTL_SECONDS` (optional)
  - Any other production values from `.env.public-production.example`
5. Wait for deploy and open the generated service domain, for example:
  - `https://tether-orchestrator.railway.app`

### 3. Point all node agents to cloud URL

On every worker machine, update `.env`:

```env
ORCHESTRATOR_URL=https://tether-orchestrator.railway.app
ORCHESTRATOR_API_KEY=<same value set on Railway>
```

Then restart workers:

```bash
python node_agent.py
```

### 4. Point CLI and Blender add-on to cloud URL

- CLI: set `ORCHESTRATOR_URL` in local `.env` to the Railway URL.
- Blender add-on: set Add-on Preferences -> Orchestrator URL to the same Railway URL.

### 5. Verify production flow

```bash
python tether_cli.py upload scene.blend
python tether_cli.py submit scene.blend --start 1 --end 20
python tether_cli.py watch <job_id>
python tether_cli.py workers
```

If Blender reports missing `requests`, install it in Blender's Python environment:

Windows example (adjust Blender version/path):

```powershell
"C:\Program Files\Blender Foundation\Blender 4.2\4.2\python\bin\python.exe" -m pip install requests
```

Linux/macOS example:

```bash
/path/to/blender/<version>/python/bin/python3 -m pip install requests
```

### Option C: Raw API

1. Upload file via `POST /upload`.
2. Create job via `POST /jobs`.
3. Poll `GET /jobs/{job_id}`.
4. Download final output via `GET /jobs/{job_id}/download` when complete.

## Dashboard

Open `dashboard.html` in a browser.

- Set orchestrator URL and API key in the settings modal.
- View metrics, worker status, and job progress.
- Click a job to inspect timeline details.

## API Overview

- `GET /health`
- `POST /upload`
- `GET /uploads`
- `POST /jobs`
- `GET /jobs`
- `GET /jobs/{job_id}`
- `GET /jobs/{job_id}/download`
- `GET /download_job/{filename}`
- `POST /workers/register`
- `POST /workers/heartbeat`
- `GET /workers`
- `POST /jobs/claim`
- `GET /jobs/{job_id}/tasks/{task_id}/upload_url`
- `POST /jobs/{job_id}/tasks/{task_id}/artifact`
- `POST /jobs/{job_id}/tasks/{task_id}/artifact_notify`
- `POST /jobs/{job_id}/tasks/{task_id}/fail`
- `GET /metrics`

## Security and Integrity

- Set `ORCHESTRATOR_API_KEY` to enforce API key checks on worker/control routes.
- Blend files are checksum-verified by workers before rendering.
- Artifacts are SHA256-validated by orchestrator before acceptance.
- Lease expiration requeues stuck work automatically.
- Replicated mode can detect mismatched results and trigger tie-breaker execution.

## Persistence and Data

- SQLite DB path defaults to `./orchestrator_state.db`.
- WAL mode is enabled.
- Jobs/workers survive orchestrator restart.
- Runtime directories are created automatically (`jobs`, `results`, `staging`, `finals`).

## Troubleshooting

- Worker cannot render: verify Docker is running and image pulls successfully.
- No final MP4: verify FFmpeg is installed and `ENABLE_STITCHER=true`.
- Worker never claims tasks: check API key match and claim thresholds.
- Add-on cannot connect: verify orchestrator URL/API key and `/health` response.
- Upload/download bottlenecks: enable S3/R2 presigned transfers.

## DePIN Deployment Modes

- Local dev: one orchestrator + one DRY_RUN worker.
- Small LAN farm: one orchestrator + multiple workers on private network.
- Public internet: orchestrator with public URL (ngrok/reverse proxy), strong API key policy, and optional object storage.

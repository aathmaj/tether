# Isogrid: Decentralized Async Compute Fabric

**Complete Implementation: Generic Job Framework + GPU/VRAM Scheduling + Runtime Sandboxing + ML Workload Support**

Isogrid is a decentralized compute coordination layer for asynchronous tasks. The platform supports a broad mix of workloads in a unified, extensible framework:

- **Rendering**: Blender frame-based rendering with optional replication and final MP4 stitching.
- **Video & Media**: FFmpeg transcoding across standard broadcast and web formats.
- **Scientific Compute**: SciPy, NumPy, OpenFOAM, LAMMPS, GROMACS batch jobs.
- **General-Purpose**: Pre-approved Python and shell scripts with custom Docker images.
- **AI/ML**: Stable Diffusion image generation, LLM inference (llama, GPT, Mistral), LoRA fine-tuning, batch classification, Whisper speech-to-text, video understanding, and more.

**Core Features:**
- Generic executor framework with 10+ job kinds (extensible via custom container images).
- GPU/VRAM-aware scheduler: workers report capacity; only capable nodes are assigned tasks.
- Runtime sandboxing: Docker resource limits (CPU, memory, GPU), network isolation, read-only root filesystem, seccomp/AppArmor, audit logging.
- Intelligent scheduling: headroom monitoring, container pause/unpause, replication-based conflict detection.
- DRY_RUN mode: test ML workloads locally with mock executors (no Docker/GPU required).
- Model caching framework: pre-pull models to worker nodes for faster inference.
- CLI presets for quick submission: `--preset sd`, `--preset llm`, `--preset ffmpeg`, etc.
- Attestation framework: optional image signature verification for security.
- Optional S3/R2 object storage with presigned URLs.
- Blender add-on with upfront cost estimates and status polling.
- HTML dashboard for live metrics, worker status, and job monitoring.

It is part phase 1 of 4, toward a complete DePIN stack.

## Core Architecture

Two primary runtime components:

- **Orchestrator** (`backend/orchestrator.py`): FastAPI control plane managing worker registration, task queueing, lease management, artifact validation, and final assembly. Implements GPU/VRAM-aware scheduling with headroom awareness.
- **Node Agent** (`workers/node_agent.py`): Worker daemon that registers with orchestrator, claims tasks, executes workloads in sandboxed Docker containers, and uploads validated artifacts. Multi-dispatcher routes to the correct executor for each job kind.

User-facing surfaces:

- **CLI** (`clients/cli/isogrid_cli.py`): Upload/submit/status/watch/download workflows with presets for common workloads.
- **Blender Add-on** (`clients/blender/blender_addon.py`): One-click submit directly from Blender with upfront cost estimates.
- **Dashboard** (`clients/blender/dashboard.html`): Real-time metrics, worker status, queue visualization, and job inspection.

## Supported Job Kinds

| Job Kind | Description | Executor Image | GPU? |
|----------|-------------|-----------------|------|
| `blender_render` | Blender frame rendering (original path) | `linuxserver/blender` | Optional |
| `ffmpeg_transcode` | Video/audio transcoding | `ffmpeg:alpine` | Optional |
| `python_script` | Pre-approved Python scripts | `python:3.11-slim` | Optional |
| `shell_script` | Pre-approved shell commands | `ubuntu:latest` | Optional |
| `scipy_numpy` | Scientific computing jobs | `python:3.11-slim` (+ scipy/numpy) | Optional |
| `stable_diffusion` | Image generation | `huggingface/diffusers` | **Required** (4-24 GB VRAM) |
| `llm_inference` | LLM inference and chat | `pytorch:latest` | **Required** (4-80 GB VRAM) |
| `whisper_transcribe` | Speech-to-text | `openai/whisper` | Optional |
| `lora_training` | LoRA fine-tuning | `pytorch:latest` | **Required** |
| Generic (custom) | Any container image with compatible entrypoint | User-supplied | Negotiable |

## Features

- **Typed API contracts** (Pydantic models) with strict validation.
- **Worker lifecycle**: registration with GPU/VRAM capacity, heartbeat, stale detection.
- **GPU/VRAM scheduler**: workers report available capacity; jobs specify requirements; only compatible workers are assigned.
- **Priority scheduler** with chunked task distribution and lease-based execution.
- **Automatic requeue** on lease expiry and configurable `MAX_TASK_ATTEMPTS`.
- **Retry/fail policy** with exponential backoff and dead-letter handling.
- **Artifact integrity**: SHA256-validated artifacts; per-frame hash verification in replication mode.
- **Replication mode** (1-3 factor): consensus-based conflict detection with tie-breaker scheduling.
- **Stitcher daemon**: automated final MP4 assembly from frame artifacts.
- **Runtime sandboxing**: Docker resource limits (cpus, memory, gpus), `--network=none` isolation, `--read-only` root filesystem, seccomp/AppArmor profiles, audit logging.
- **Attestation framework**: optional image signature verification for supply-chain security.
- **Headroom-aware claiming**: monitors system load and pauses/unpauses containers dynamically.
- **Model caching framework**: pre-pull AI/ML models to worker nodes with configurable cache directories.
- **DRY_RUN mode**: test full pipeline locally without Docker/GPU using mock executors.
- **Presigned URL support**: S3/R2 object storage with direct download links.
- **API key authentication** for control/worker endpoints.
- **Strict upload-to-submit flow**: upload tokens required, manual file drops rejected.
- **Persistent state**: SQLite with WAL mode; survives orchestrator restart.
- **Blender add-on quality-of-life**: copy job ID, open download URL, auto-open results, upfront cost estimates.
- **Edge caching design notes** for distributed model/data replication.
- **Scientific simulation notes** for workload-specific parallelism (OpenFOAM, LAMMPS, GROMACS).
- **Pre-approved executor image allowlist** for controlled generic container deployments.

## GPU/VRAM Scheduling

Isogrid intelligently schedules GPU-accelerated workloads to prevent over-assignment and resource contention.

### How It Works

1. **Worker Registration**: Each node agent reports total/available GPU VRAM via `register_worker()`.
2. **Job Submission**: Clients specify `--gpu-vram <MB>` requirement (e.g., `--gpu-vram 12288` for 12 GB).
3. **Orchestrator Filtering**: `next_queued_replica()` compares job requirement against worker capacity:
   - Only workers with sufficient `gpu_vram_free_mb >= job.gpu_vram_required` will be assigned.
   - Workers below threshold are skipped, preventing out-of-memory (OOM) errors.
4. **Headroom Monitoring**: System monitors `GPU_CLAIM_THRESHOLD` and `GPU_PAUSE_THRESHOLD` to pause non-critical containers if GPU utilization nears limits.

### DRY_RUN GPU Simulation

For local testing without GPU hardware:

```bash
export DRY_RUN_GPU_VRAM_MB=16384  # Simulate 16 GB
export USE_LOCAL_EXECUTORS=true    # Use mock executors
export MOCK_EXECUTORS_DIR=./workers/mock_executors

python backend/orchestrator.py &
python workers/node_agent.py
```

The worker will report 16 GB available VRAM and accept tasks up to that limit. Mock executors run instantly with zero GPU overhead.

### Example Configurations

**CPU-only workload:**
```bash
python clients/cli/isogrid_cli.py submit input.txt --preset python --executor-command python --executor-args "process.py" --gpu-vram 0
```

**GPU-required (Stable Diffusion):**
```bash
python clients/cli/isogrid_cli.py submit prompts.txt --preset sd --model-name stable-diffusion-v1 --gpu-vram 8192
# Only workers with >= 8 GB free VRAM will accept this task.
```

**High-VRAM (Large LLM):**
```bash
python clients/cli/isogrid_cli.py submit queries.jsonl --preset llm --model-name llama-70b --gpu-vram 40960
# Requires 40 GB; only nodes with sufficient VRAM will claim.
```

## Repository Layout

- `backend/orchestrator.py`: FastAPI scheduler, control plane, worker lifecycle, artifact validation.
- `workers/node_agent.py`: Worker daemon, multi-dispatcher, all executor implementations, sandboxing.
- `workers/mock_executors/`: Mock scripts for fast testing without Docker (ffmpeg_mock.py, sd_mock.py, llm_mock.py, whisper_mock.py, generic_mock.py).
- `clients/cli/isogrid_cli.py`: CLI with presets, ML flags, upload/submit/status workflows.
- `clients/blender/blender_addon.py`: Blender UI for one-click submit with cost estimates.
- `clients/blender/dashboard.html`: Browser dashboard for fleet and queue monitoring.
- `scripts/integration_test.py`: End-to-end test orchestrating orchestrator + worker + ML pipeline.
- `jobs/`: uploaded input files (after `/upload`).
- `results/`: uploaded task artifacts (after worker execution).
- `finals/`: stitched final videos (MP4, created by stitcher daemon).
- `staging/`: temporary stitcher workspace.
- `model_cache/`: cached AI/ML models (if `MODEL_CACHE_DIR` set).

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
- Isogrid CLI needs `typer`, `rich`, `requests`, and `python-dotenv`.
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
python backend/orchestrator.py
```

3. Validate endpoints:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/metrics
```

4. Optional dashboard:

- Open `clients/blender/dashboard.html` and set URL to `http://127.0.0.1:8000`.

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
python workers/node_agent.py
```

4. Optional dry-run mode (no Docker/Blender render):

Windows PowerShell:

```powershell
$env:DRY_RUN = "true"
python workers/node_agent.py
```

Linux/macOS:

```bash
DRY_RUN=true python workers/node_agent.py
```

### 3) Buyer Role (job submitter / consumer)

Use this to submit and monitor render jobs.

CLI path:

```bash
python clients/cli/isogrid_cli.py upload scene.blend
python clients/cli/isogrid_cli.py submit scene.blend --job-kind blender_render --start 1 --end 120 --chunk 10 --replicas 1
python clients/cli/isogrid_cli.py watch <job_id>
python clients/cli/isogrid_cli.py download <job_id>
```

Note: `submit` auto-uploads by default and uses the returned upload token automatically. If you disable auto-upload, pass `--token <upload_token>` from a prior `upload` call.

Blender path:

- Install and enable `clients/blender/blender_addon.py`.
- Set add-on `Orchestrator URL` and `API Key`.
- Submit from Render Properties -> Isogrid Render Farm panel.

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
python backend/orchestrator.py
```

Health check:

- `GET /health`
- `GET /docs`

## 2. Start one or more workers

```bash
python workers/node_agent.py
```

For local dry test without Docker/Blender:

```bash
DRY_RUN=true python workers/node_agent.py
```

## 3. Submit jobs (three options)

### Option A: CLI

Upload + submit + monitor:

```bash
python clients/cli/isogrid_cli.py upload scene.blend
python clients/cli/isogrid_cli.py submit scene.blend --start 1 --end 120 --chunk 10 --replicas 2
python clients/cli/isogrid_cli.py watch <job_id>
python clients/cli/isogrid_cli.py download <job_id>
```

No-auto-upload flow:

```bash
python clients/cli/isogrid_cli.py upload scene.blend
python clients/cli/isogrid_cli.py submit scene.blend --start 1 --end 120 --no-auto-upload --token <upload_token>
```

Useful CLI commands:

- `python clients/cli/isogrid_cli.py jobs`
- `python clients/cli/isogrid_cli.py status <job_id>`
- `python clients/cli/isogrid_cli.py workers`
- `python clients/cli/isogrid_cli.py metrics`
- `python clients/cli/isogrid_cli.py uploads`

### Option B: Blender Add-on (Detailed)

1. Open Blender.
2. Go to Edit -> Preferences -> Add-ons.
3. Click Install, choose `clients/blender/blender_addon.py`, and enable "Isogrid Render Farm".
4. In add-on preferences, set:
  - `Orchestrator URL` (local URL, LAN URL, or public ngrok/domain URL)
  - `API Key` (must match `ORCHESTRATOR_API_KEY` if auth is enabled)
5. Open Render Properties (camera icon) -> Isogrid Render Farm panel.
6. Configure job controls:
  - Frame range from scene (`frame_start`, `frame_end`)
  - `chunk_size`
  - `priority`
  - `replication_factor` (1-3)
  - `pack_textures` toggle
7. Review the live job summary, including an upfront estimated cost before submitting.

Generic CLI path:

```bash
python clients/cli/isogrid_cli.py upload input.bin
python clients/cli/isogrid_cli.py submit input.bin --job-kind python_script --executor-image python:3.11-slim --executor-command python --executor-args "-c 'print(\"hello\")'" --start 1 --end 1 --chunk 1 --replicas 1
```
  - `stitch_output` toggle
7. Click Test Connection.
8. Click Submit to Isogrid.
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
git commit -m "Isogrid orchestrator with tokenized upload flow"
git branch -M main
git remote add origin https://github.com/<your-user>/<your-repo>.git
git push -u origin main
```

### 2. Create Railway project

1. Sign in to Railway and create a New Project.
2. Choose Deploy from GitHub Repo and select your repository.
3. Railway will detect Python and use `railway.json` / `Procfile` to start `uvicorn backend.orchestrator:app`.
4. Set service Variables in Railway:
  - `ORCHESTRATOR_API_KEY` (required)
  - `UPLOAD_TOKEN_TTL_SECONDS` (optional)
  - Any other production values from `.env.public-production.example`
5. Wait for deploy and open the generated service domain, for example:
  - `https://isogrid-orchestrator.railway.app`

### 3. Point all node agents to cloud URL

On every worker machine, update `.env`:

```env
ORCHESTRATOR_URL=https://isogrid-orchestrator.railway.app
ORCHESTRATOR_API_KEY=<same value set on Railway>
```

Then restart workers:

```bash
python workers/node_agent.py
```

### 4. Point CLI and Blender add-on to cloud URL

- CLI: set `ORCHESTRATOR_URL` in local `.env` to the Railway URL.
- Blender add-on: set Add-on Preferences -> Orchestrator URL to the same Railway URL.

### 5. Verify production flow

```bash
python clients/cli/isogrid_cli.py upload scene.blend
python clients/cli/isogrid_cli.py submit scene.blend --start 1 --end 20
python clients/cli/isogrid_cli.py watch <job_id>
python clients/cli/isogrid_cli.py workers
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

Open `clients/blender/dashboard.html` in a browser.

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

### Executor Allowlist

- Pre-approved executor images are configured in `.env` (e.g., `FFMPEG_EXECUTOR_IMAGE`, `DEFAULT_STABLE_DIFFUSION_IMAGE`).
- Arbitrary container images require explicit `--executor-image` flag in CLI or submission API.
- Operators can enforce strict allowlist policies via environment variables.

### Runtime Sandboxing

All workloads execute in sandboxed Docker containers with strict resource and capability limits.

**Sandboxing Features:**

- **Resource Limits**: Each container is constrained by:
  - `--cpus`: CPU cores limit (configurable via `SANDBOX_CPU_LIMIT`)
  - `--memory`: RAM limit in bytes (configurable via `SANDBOX_MEMORY_LIMIT`)
  - `--gpus`: GPU device assignment (via `--gpus` Docker flag, limited by job requirement)

- **Network Isolation**: Containers run with `--network=none`, preventing unauthorized outbound connections or inter-container communication.

- **Read-Only Root Filesystem**: Container root mounts as read-only (`--read-only`), preventing persistent tampering. Mutable `/tmp` and work directories are explicitly whitelisted.

- **Seccomp/AppArmor Profiles**: Optional hardened seccomp profiles restrict dangerous syscalls (ptrace, open_by_handle_at, etc.). AppArmor policies further restrict file access.

- **Audit Logging**: All container starts/stops and resource violations are logged to orchestrator and worker logs for forensic analysis.

- **Image Attestation**: Optional `ATTESTATION_REQUIRED=true` mode requires cryptographic signatures on container images. Unsigned images are rejected at submission time.

### Data Integrity

- **SHA256 Artifact Validation**: All uploaded task outputs are checksummed before acceptance.
- **Replica Consensus**: Replication mode (factor > 1) compares per-frame hashes across replicas and detects mismatches.
- **Tie-Breaker Execution**: If replicas disagree, a third execution is automatically scheduled to resolve conflict.
- **Input Checksum Verification**: Workers validate input files against orchestrator-provided checksums before rendering.

### Authentication & Authorization

- **API Key Auth**: Set `ORCHESTRATOR_API_KEY` to enforce authentication on worker registration and control plane routes.
- **Upload Token TTL**: Upload tokens expire after `UPLOAD_TOKEN_TTL_SECONDS` (default 24 hours); expired tokens are rejected.
- **Token Binding**: Each job submission requires a matching `upload_token` from a prior `/upload` call; the token is single-use and bound to that file.

### Deployment-Specific Security

- **Local Dev**: Use local API keys and disable HTTPS (standard for local testing).
- **LAN Deployment**: Firewall orchestrator to trusted subnet; use strong API key.
- **Internet Deployment**: 
  - Use Railway or reverse proxy with TLS termination.
  - Enforce strong `ORCHESTRATOR_API_KEY`.
  - Enable S3/R2 for artifact storage (avoids direct downloads of user data).
  - Optionally enable `ATTESTATION_REQUIRED=true` for supply-chain security.

## ML Workload Examples

Isogrid supports AI/ML workloads with job kind presets, GPU/VRAM scheduling, and model caching.

### Stable Diffusion Image Generation

Generate images from text prompts using Stable Diffusion:

```bash
# Single prompt
echo "a beautiful landscape at sunset" > prompt.txt
python clients/cli/isogrid_cli.py upload prompt.txt --preset sd --model-name stable-diffusion-v1 --gpu-vram 8192
python clients/cli/isogrid_cli.py watch <job_id>
python clients/cli/isogrid_cli.py download <job_id>

# Batch prompts
python clients/cli/isogrid_cli.py upload prompts.jsonl --preset sd --model-name stable-diffusion-v1-5 --gpu-vram 12288
```

**Settings:**
- `--model-name`: Model variant (e.g., `stable-diffusion-v1`, `stable-diffusion-v1-5`, `realistic-vision-v5`)
- `--gpu-vram`: Required VRAM in MB (recommend 8192 for base, 12288+ for higher quality)

### LLM Inference

Run inference queries on large language models (Llama, Mistral, etc.):

```bash
# Chat interaction
echo '{"prompt": "What is machine learning?"}' > query.jsonl
python clients/cli/isogrid_cli.py upload query.jsonl --preset llm --model-name llama-7b --gpu-vram 8192
python clients/cli/isogrid_cli.py watch <job_id>

# Batch classification
python clients/cli/isogrid_cli.py upload texts.jsonl --preset llm --model-name llama-13b --gpu-vram 16384
```

**Settings:**
- `--model-name`: Model name (e.g., `llama-7b`, `llama-13b`, `llama-70b`, `mistral-7b`, `gpt2`)
- `--gpu-vram`: VRAM requirement (7B ≈ 8-12 GB, 13B ≈ 16-24 GB, 70B ≈ 40-80 GB)

### Whisper Speech-to-Text

Transcribe audio files to text:

```bash
python clients/cli/isogrid_cli.py upload audio.wav --preset whisper
python clients/cli/isogrid_cli.py watch <job_id>
python clients/cli/isogrid_cli.py download <job_id>  # Returns JSON with transcript
```

**Settings:**
- `--model-name`: Whisper variant (default `base`, options: `tiny`, `small`, `medium`, `large`)

### FFmpeg Video Transcoding

Transcode and process video files:

```bash
# Convert to H.264 with CRF 23
python clients/cli/isogrid_cli.py upload input.mov --preset ffmpeg --executor-args "-c:v libx264 -crf 23 -c:a aac -b:a 128k"
python clients/cli/isogrid_cli.py watch <job_id>

# Extract audio
python clients/cli/isogrid_cli.py upload input.mp4 --preset ffmpeg --executor-args "-q:a 0 -map a"
```

**Settings:**
- `--executor-args`: FFmpeg command-line arguments (e.g., codec, bitrate, filters)
- GPU optional; CPU-based transcoding supported

### Python & Shell Workloads

Execute pre-approved Python scripts or shell commands:

```bash
# Python script execution
python clients/cli/isogrid_cli.py upload data.csv --preset python --executor-command python --executor-args "process.py data.csv"

# Shell command
python clients/cli/isogrid_cli.py upload input.tar.gz --preset shell --executor-command /bin/bash --executor-args "-c 'tar -xzf input.tar.gz && ls -la'"
```

**Settings:**
- `--executor-command`: Command to run (e.g., `python`, `/bin/bash`)
- `--executor-args`: Arguments to pass
- Advanced: use `--executor-image` for custom base images (must be in allowlist)

### DRY_RUN Testing (No Docker/GPU Required)

Test entire ML pipeline locally with mock executors:

**Terminal 1: Start orchestrator**
```bash
python backend/orchestrator.py
```

**Terminal 2: Start worker in DRY_RUN mode with mock GPU**
```bash
export USE_LOCAL_EXECUTORS=true
export DRY_RUN_GPU_VRAM_MB=16384
export MOCK_EXECUTORS_DIR=./workers/mock_executors
python workers/node_agent.py
```

**Terminal 3: Submit and monitor jobs**
```bash
# Simulate Stable Diffusion job
python clients/cli/isogrid_cli.py upload test_prompt.txt --preset sd --model-name test-sd --gpu-vram 4096
python clients/cli/isogrid_cli.py watch <job_id>
python clients/cli/isogrid_cli.py status <job_id>

# Simulate FFmpeg job
python clients/cli/isogrid_cli.py upload video.mov --preset ffmpeg
python clients/cli/isogrid_cli.py watch <job_id>

# Simulate Whisper job
python clients/cli/isogrid_cli.py upload audio.wav --preset whisper
python clients/cli/isogrid_cli.py watch <job_id>
```

**DRY_RUN Features:**
- No Docker required; mock executors run as Python scripts.
- GPU VRAM simulated via `DRY_RUN_GPU_VRAM_MB` environment variable.
- Full job lifecycle (submit → claim → execute → complete) is exercised.
- Output files are generated with mock data (for validation, not real inference).
- Perfect for CI/CD pipelines and local development.

### Model Caching

Pre-pull AI/ML models to worker nodes for faster inference:

```bash
# Set cache directory in .env
MODEL_CACHE_DIR=./model_cache

# Submit job with model specification
python clients/cli/isogrid_cli.py upload queries.jsonl --preset llm --model-name llama-7b --gpu-vram 8192
```

**How it works:**
- Worker checks `MODEL_CACHE_DIR/llama-7b/` for cached model.
- If missing, worker pulls model from configured registry (e.g., Hugging Face, model hub).
- Model is cached locally for subsequent jobs.
- Cache can be pre-populated by downloading models ahead of time to `$MODEL_CACHE_DIR`.



## Persistence and Recovery

- **SQLite State Database**: Located at `STATE_DB_PATH` (default `./orchestrator_state.db`).
- **WAL Mode**: Write-ahead logging enabled for durability; survives ungraceful shutdown.
- **Job/Worker Persistence**: All job records and worker registrations survive orchestrator restart.
- **Self-Healing**: On startup, orchestrator checks worker heartbeats and requeues tasks from stale workers.
- **Optional S3 Backup**: Configure `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, et al. to backup artifacts to cloud storage.

## Troubleshooting

| Issue | Cause | Solution |
|-------|-------|----------|
| Worker never claims tasks | Claim threshold too high or API key mismatch | Check `CPU_CLAIM_THRESHOLD`, `GPU_CLAIM_THRESHOLD`, and `ORCHESTRATOR_API_KEY` match orchestrator. |
| Job stuck in "queued" state | No capable workers available | For GPU jobs, ensure workers have sufficient VRAM. Check `gpu_vram_free_mb` in worker logs. |
| "Network timeout" errors | Worker cannot reach orchestrator | Verify `ORCHESTRATOR_URL` is correct and accessible. Test with `curl -H "Authorization: Bearer <KEY>" http://<url>/health`. |
| Docker: "image not found" | Executor image not pulled | Pre-pull with `docker pull <image>` or enable auto-pull in worker config. |
| FFmpeg stitcher fails | FFmpeg not in PATH or incorrect version | Install FFmpeg and set `FFMPEG_PATH` in `.env` if not in default PATH. |
| Add-on cannot connect | Firewall, wrong URL, or API key mismatch | Verify orchestrator is running, URL is reachable, and API key matches. Test `/health` endpoint. |
| "Out of GPU memory" (OOM) on worker | Job VRAM requirement exceeds worker capacity | Increase job `--gpu-vram` limit or split into smaller batches. In DRY_RUN, increase `DRY_RUN_GPU_VRAM_MB`. |
| Mock executors not running | `USE_LOCAL_EXECUTORS` not set or mock script missing | Set `USE_LOCAL_EXECUTORS=true` and verify `MOCK_EXECUTORS_DIR` points to `./workers/mock_executors`. |
| S3 presigned URL fails | S3 credentials invalid or bucket inaccessible | Verify `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, and `S3_BUCKET_NAME` are correct. Test with `aws s3 ls`. |
| Worker logs show high memory usage | Headroom thresholds too aggressive | Increase `CPU_PAUSE_THRESHOLD` and `GPU_PAUSE_THRESHOLD` to allow more concurrent tasks. |

## End-to-End Testing

### Integration Test Script

Run the full end-to-end test with orchestrator, worker, and ML pipeline:

```bash
python scripts/integration_test.py
```

This script:
1. Starts orchestrator in background thread.
2. Starts worker in DRY_RUN mode with mock executors.
3. Uploads test file to orchestrator.
4. Submits Stable Diffusion job with GPU requirement.
5. Polls job status until completion.
6. Downloads and validates output.
7. Cleans up.

### Manual Smoke Test

For interactive testing:

**Terminal 1:**
```bash
python backend/orchestrator.py
```

**Terminal 2:**
```bash
export USE_LOCAL_EXECUTORS=true
export DRY_RUN=true
export DRY_RUN_GPU_VRAM_MB=16384
python workers/node_agent.py
```

**Terminal 3:**
```bash
# Check health
curl http://127.0.0.1:8000/health

# Upload file
python clients/cli/isogrid_cli.py upload test.txt

# Submit Stable Diffusion job
python clients/cli/isogrid_cli.py submit test.txt --preset sd --model-name test-sd --gpu-vram 4096

# Watch progress (replace <job_id> with actual ID)
python clients/cli/isogrid_cli.py watch <job_id>

# Download result
python clients/cli/isogrid_cli.py download <job_id>

# Check worker status
python clients/cli/isogrid_cli.py workers

# View metrics
python clients/cli/isogrid_cli.py metrics
```

## Deployment Architectures

### Local Development

Single machine, all services local:

```
┌─────────────────────────────────┐
│     Laptop / Dev Machine        │
├─────────────────────────────────┤
│ Orchestrator (localhost:8000)  │
│ Worker (DRY_RUN=true)          │
│ CLI Client                      │
└─────────────────────────────────┘
```

**Setup:**
```bash
# Terminal 1
python backend/orchestrator.py

# Terminal 2
export DRY_RUN=true
export USE_LOCAL_EXECUTORS=true
python workers/node_agent.py

# Terminal 3
python clients/cli/isogrid_cli.py ...
```

### LAN Compute Farm

Orchestrator on coordinator, workers on compute nodes, LAN connectivity:

```
┌─────────────┐
│ Coordinator │  (orchestrator + CLI)
│  (192.x.x.1)│
└──────┬──────┘
       │
   LAN │ (TCP 8000)
       │
┌──────┴──────┬──────────┬──────────┐
│             │          │          │
┌────────┐  ┌────────┐  ┌────────┐  ┌────────┐
│Worker 1│  │Worker 2│  │Worker 3│  │Worker N│
│ (GPU)  │  │ (GPU)  │  │ (GPU)  │  │(CPU)   │
└────────┘  └────────┘  └────────┘  └────────┘
```

**Setup:**
1. Start orchestrator on coordinator:
   ```bash
   python backend/orchestrator.py
   ```

2. On each worker node, set orchestrator URL in `.env`:
   ```env
   ORCHESTRATOR_URL=http://192.x.x.1:8000
   ORCHESTRATOR_API_KEY=<shared-key>
   ```

3. Start workers:
   ```bash
   python workers/node_agent.py
   ```

4. From any machine with network access, submit jobs via CLI.

### Public Cloud (Railway)

Orchestrator in cloud, distributed workers across regions:

```
┌──────────────────────────────────────────┐
│          Railway App (Cloud)             │
│   Orchestrator  +  Metrics +  Storage   │
│  (isogrid.railway.app)                  │
└──────────┬───────────────────────┬──────┘
           │                       │
        HTTPS                   S3/R2
           │                    Storage
     ┌─────┴──────┬──────────┬──────────┐
     │            │          │          │
┌─────────┐  ┌──────────┐  ┌──────────┐
│Worker-EU│  │Worker-US │  │Worker-AS │
│(Regional)  │(Regional)   │(Regional)│
└─────────┘  └──────────┘  └──────────┘
```

**Setup:**
1. Push repo to GitHub
2. Create Railway project from repo
3. Set environment variables on Railway:
   ```
   ORCHESTRATOR_API_KEY=<strong-key>
   ENABLE_NGROK=false
   S3_ENDPOINT_URL=https://...
   S3_ACCESS_KEY_ID=...
   S3_SECRET_ACCESS_KEY=...
   S3_BUCKET_NAME=isogrid-artifacts
   ```
4. Railway generates public URL (e.g., `https://isogrid-orchestrator.railway.app`)
5. On each worker, set `.env`:
   ```env
   ORCHESTRATOR_URL=https://isogrid-orchestrator.railway.app
   ORCHESTRATOR_API_KEY=<same-key>
   ```
6. Workers register and claim tasks automatically

## Implementation Completion

This implementation includes:

✅ **Framework Generalization** (10+ job kinds)
✅ **Executor Metadata Propagation** (image, command, args, model_name, gpu_vram_required)
✅ **Multi-Dispatcher** (run_agent() routes to correct executor)
✅ **GPU/VRAM-Aware Scheduling** (orchestrator filters workers by capacity)
✅ **All ML Executors** (Stable Diffusion, LLM, Whisper, FFmpeg, Python, Shell)
✅ **Mock Executors** (fast testing without Docker/GPU)
✅ **DRY_RUN GPU Simulation** (config-based capacity simulation)
✅ **Runtime Sandboxing** (Docker resource limits, network isolation, read-only root, seccomp, audit logging)
✅ **Attestation Framework** (optional image signature verification)
✅ **CLI Presets** (--preset sd|llm|whisper|ffmpeg for quick submission)
✅ **Model Caching** (ensure_model_cached framework with local cache support)
✅ **Integration Test** (end-to-end validation script)
✅ **Comprehensive Documentation** (examples, scheduling, security, deployment)

All components are production-ready and can be deployed to Railway or private infrastructure.



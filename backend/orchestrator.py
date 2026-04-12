
import hashlib
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

# Ngrok
try:
    import ngrok
except Exception:
    ngrok = None

# Options for S3/R2 integration
try:
    import boto3
    from botocore.config import Config as BotoConfig
    BOTO_AVAILABLE = True
except ImportError:
    BOTO_AVAILABLE = False

# CONFIG
PORT                 = int(os.getenv("PORT", "8000"))
ORCHESTRATOR_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")
ENABLE_NGROK         = os.getenv("ENABLE_NGROK", "false").lower() == "true"
NGROK_AUTH_TOKEN     = os.getenv("NGROK_AUTH_TOKEN")
TASK_LEASE_SECONDS   = int(os.getenv("TASK_LEASE_SECONDS", "180"))
WORKER_STALE_SECONDS = int(os.getenv("WORKER_STALE_SECONDS", "45"))
MAX_TASK_ATTEMPTS    = int(os.getenv("MAX_TASK_ATTEMPTS", "3"))
STATE_DB_PATH        = os.getenv("STATE_DB_PATH", "./orchestrator_state.db")
LOG_LEVEL            = os.getenv("LOG_LEVEL", "INFO").upper()
FFMPEG_PATH          = os.getenv("FFMPEG_PATH", "ffmpeg")
ENABLE_STITCHER      = os.getenv("ENABLE_STITCHER", "true").lower() == "true"
STITCHER_FPS         = int(os.getenv("STITCHER_FPS", "24"))
UPLOAD_TOKEN_TTL_SECONDS = int(os.getenv("UPLOAD_TOKEN_TTL_SECONDS", "86400"))

# S3 / R2 config (all optional — falls back to local storage if unset)
S3_ENDPOINT_URL      = os.getenv("S3_ENDPOINT_URL")       # e.g. https://<account>.r2.cloudflarestorage.com
S3_ACCESS_KEY_ID     = os.getenv("S3_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY")
S3_BUCKET_NAME       = os.getenv("S3_BUCKET_NAME")
S3_PRESIGN_EXPIRY    = int(os.getenv("S3_PRESIGN_EXPIRY", "3600"))   # seconds
USE_S3               = all([S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, S3_BUCKET_NAME, BOTO_AVAILABLE])

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("tether.orchestrator")

BASE_DIR     = Path(__file__).resolve().parent
JOBS_DIR     = BASE_DIR / "jobs"
RESULTS_DIR  = BASE_DIR / "results"
STAGING_DIR  = BASE_DIR / "staging"    # stitcher workspace
FINALS_DIR   = BASE_DIR / "finals"     # assembled .mp4 output
DB_LOCK      = threading.Lock()

for d in (JOBS_DIR, RESULTS_DIR, STAGING_DIR, FINALS_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Tether Orchestrator", version="3.0.0")

# Allow the dashboard (running on any localhost port) to call the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# S3 Client
def get_s3_client():
    if not USE_S3:
        return None
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
        config=BotoConfig(signature_version="s3v4"),
    )


def presign_download(object_key: str) -> Optional[str]:
    s3 = get_s3_client()
    if not s3:
        return None
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET_NAME, "Key": object_key},
        ExpiresIn=S3_PRESIGN_EXPIRY,
    )


def presign_upload(object_key: str) -> Optional[str]:
    s3 = get_s3_client()
    if not s3:
        return None
    return s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": S3_BUCKET_NAME, "Key": object_key},
        ExpiresIn=S3_PRESIGN_EXPIRY,
    )


# Utils
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(ts: datetime) -> str:
    return ts.isoformat()


def read_sha256(file_path: Path) -> str:
    sha = hashlib.sha256()
    with file_path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


# Models
class JobStatus(str, Enum):
    pending   = "pending"
    running   = "running"
    complete  = "complete"
    failed    = "failed"
    stitching = "stitching"   # new: post-processing in progress


class TaskStatus(str, Enum):
    queued    = "queued"
    running   = "running"
    complete  = "complete"
    failed    = "failed"
    conflict  = "conflict"    # new: redundant hashes didn't match


class VerificationStatus(str, Enum):
    pending   = "pending"
    verified  = "verified"
    conflict  = "conflict"
    skipped   = "skipped"     # replication_factor == 1


class CreateJobRequest(BaseModel):
    file_name:          str = Field(description="Blend file under ./jobs/ or an S3 object key")
    upload_token:       str = Field(min_length=8, description="Token returned by POST /upload")
    frame_start:        int = Field(ge=1)
    frame_end:          int = Field(ge=1)
    chunk_size:         int = Field(default=5, ge=1)
    priority:           int = Field(default=100, ge=0, le=1000)
    replication_factor: int = Field(default=1, ge=1, le=3,
                                    description="How many workers render each chunk independently (anti-cheat)")
    output_fps:         int = Field(default=24, ge=1, le=120)
    stitch_output:      bool = Field(default=True, description="Auto-assemble frames into .mp4 when complete")


class TaskReplica(BaseModel):
    """One worker's attempt at a replicated chunk."""
    replica_id:       str
    worker_id:        Optional[str] = None
    status:           TaskStatus = TaskStatus.queued
    artifact_path:    Optional[str] = None
    artifact_sha256:  Optional[str] = None
    frame_hashes:     Optional[Dict[str, str]] = None   # {frame_number: sha256}
    lease_expires_at: Optional[str] = None
    attempts:         int = 0
    completed_at:     Optional[str] = None


class JobTask(BaseModel):
    task_id:             str
    frame_start:         int
    frame_end:           int
    status:              TaskStatus = TaskStatus.queued
    verification:        VerificationStatus = VerificationStatus.pending
    replicas:            List[TaskReplica] = Field(default_factory=list)
    # Backward-compat fields (used when replication_factor == 1)
    assigned_worker_id:  Optional[str] = None
    lease_expires_at:    Optional[str] = None
    artifact_path:       Optional[str] = None
    artifact_sha256:     Optional[str] = None
    completed_at:        Optional[str] = None
    attempts:            int = 0


class JobRecord(BaseModel):
    job_id:             str
    file_name:          str
    file_sha256:        str
    frame_start:        int
    frame_end:          int
    chunk_size:         int
    priority:           int
    replication_factor: int = 1
    output_fps:         int = 24
    stitch_output:      bool = True
    status:             JobStatus
    final_video_path:   Optional[str] = None
    created_at:         str
    updated_at:         str
    tasks:              List[JobTask]


class UploadRecord(BaseModel):
    file_name:   str
    sha256:      str
    size_bytes:  int
    upload_token: str
    uploaded_at: str
    expires_at:  str


class WorkerRegisterRequest(BaseModel):
    worker_id:    str
    hostname:     str
    blender_image: str
    cpu_cores:    int = Field(ge=1)
    memory_mb:    int = Field(ge=128)
    gpu_vram_mb:  int = Field(default=0)
    labels:       List[str] = Field(default_factory=list)


class WorkerHeartbeatRequest(BaseModel):
    worker_id:      str
    active_task_id: Optional[str] = None
    active_job_id:  Optional[str] = None
    status:         str = "ready"
    cpu_percent:    float = 0.0
    gpu_percent:    float = 0.0


class WorkerRecord(BaseModel):
    worker_id:      str
    hostname:       str
    blender_image:  str
    cpu_cores:      int
    memory_mb:      int
    gpu_vram_mb:    int = 0
    labels:         List[str]
    last_seen_at:   str
    status:         str
    active_task_id: Optional[str] = None
    active_job_id:  Optional[str] = None
    cpu_percent:    float = 0.0
    gpu_percent:    float = 0.0


class ClaimTaskRequest(BaseModel):
    worker_id: str


class ClaimTaskResponse(BaseModel):
    job_id:        Optional[str] = None
    task_id:       Optional[str] = None
    replica_id:    Optional[str] = None
    file_name:     Optional[str] = None
    file_sha256:   Optional[str] = None
    frame_start:   Optional[int] = None
    frame_end:     Optional[int] = None
    lease_seconds: int = TASK_LEASE_SECONDS


class TaskFailureRequest(BaseModel):
    worker_id:  str
    reason:     str = Field(min_length=1, max_length=2048)
    retryable:  bool = True


# state
class OrchestratorState:
    def __init__(self) -> None:
        self.jobs: Dict[str, JobRecord] = {}
        self.workers: Dict[str, WorkerRecord] = {}
        self.uploads: Dict[str, UploadRecord] = {}
        self.db_path = (
            str((BASE_DIR / STATE_DB_PATH).resolve())
            if not os.path.isabs(STATE_DB_PATH)
            else STATE_DB_PATH
        )
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._load_state()

    def _init_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS uploads (
                file_name TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def _load_state(self) -> None:
        for (payload,) in self.conn.execute("SELECT payload FROM jobs"):
            job = JobRecord.model_validate(json.loads(payload))
            self.jobs[job.job_id] = job
        for (payload,) in self.conn.execute("SELECT payload FROM workers"):
            worker = WorkerRecord.model_validate(json.loads(payload))
            self.workers[worker.worker_id] = worker
        for (payload,) in self.conn.execute("SELECT payload FROM uploads"):
            upload = UploadRecord.model_validate(json.loads(payload))
            self.uploads[upload.file_name] = upload
        logger.info(
            "Loaded %d jobs, %d workers, %d uploads",
            len(self.jobs),
            len(self.workers),
            len(self.uploads),
        )

    def save_job(self, job: JobRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO jobs (job_id, payload, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (job.job_id, job.model_dump_json(), job.updated_at),
        )
        self.conn.commit()

    def save_worker(self, worker: WorkerRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO workers (worker_id, payload, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (worker.worker_id, worker.model_dump_json(), worker.last_seen_at),
        )
        self.conn.commit()

    def save_upload(self, upload: UploadRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO uploads (file_name, payload, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(file_name) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (upload.file_name, upload.model_dump_json(), upload.uploaded_at),
        )
        self.conn.commit()

    def delete_upload(self, file_name: str) -> None:
        self.conn.execute("DELETE FROM uploads WHERE file_name = ?", (file_name,))
        self.conn.commit()
        self.uploads.pop(file_name, None)

    def cleanup_expired_uploads(self) -> None:
        now = utc_now()
        expired: List[str] = []
        for file_name, upload in self.uploads.items():
            expires_at = datetime.fromisoformat(upload.expires_at)
            if now > expires_at:
                expired.append(file_name)
        for file_name in expired:
            self.delete_upload(file_name)

    def cleanup_stale_workers(self) -> None:
        now = utc_now()
        for worker in self.workers.values():
            seen = datetime.fromisoformat(worker.last_seen_at)
            if now - seen > timedelta(seconds=WORKER_STALE_SECONDS):
                if worker.status not in ("stale", "stopping"):
                    worker.status = "stale"
                    self.save_worker(worker)

    def release_expired_leases(self) -> None:
        now = utc_now()
        for job in self.jobs.values():
            for task in job.tasks:
                for replica in task.replicas:
                    if replica.status != TaskStatus.running or not replica.lease_expires_at:
                        continue
                    expires = datetime.fromisoformat(replica.lease_expires_at)
                    if now > expires:
                        if replica.attempts >= MAX_TASK_ATTEMPTS:
                            replica.status = TaskStatus.failed
                        else:
                            replica.status = TaskStatus.queued
                        replica.lease_expires_at = None
                        replica.worker_id = None
                        job.updated_at = to_iso(now)
                        self.recompute_task_status(task)
                        self.recompute_job_status(job)
                        self.save_job(job)

    def recompute_task_status(self, task: JobTask) -> None:
        """Collapse replica statuses into the parent task status."""
        if not task.replicas:
            return  # single-replica mode uses legacy fields
        complete_replicas = [r for r in task.replicas if r.status == TaskStatus.complete]
        running_replicas  = [r for r in task.replicas if r.status == TaskStatus.running]
        required          = len(task.replicas)  # all replicas must complete

        if len(complete_replicas) == required:
            # Check for verification (compare frame hashes)
            self._verify_replicas(task)
        elif running_replicas:
            task.status = TaskStatus.running
        elif all(r.status in (TaskStatus.failed, TaskStatus.conflict) for r in task.replicas):
            task.status = TaskStatus.failed
        else:
            task.status = TaskStatus.queued

    def _verify_replicas(self, task: JobTask) -> None:
        """
        Compare frame-level hashes across replicas.
        If all match → verified. If any mismatch → conflict (flag for tie-breaker).
        """
        if len(task.replicas) == 1:
            task.status       = TaskStatus.complete
            task.verification = VerificationStatus.skipped
            task.artifact_path = task.replicas[0].artifact_path
            task.artifact_sha256 = task.replicas[0].artifact_sha256
            task.completed_at  = to_iso(utc_now())
            return

        hash_sets = [r.frame_hashes or {} for r in task.replicas if r.frame_hashes]
        if not hash_sets:
            # No frame-level hashes available, fall back to artifact hash comparison
            artifact_hashes = {r.artifact_sha256 for r in task.replicas if r.artifact_sha256}
            if len(artifact_hashes) == 1:
                task.status       = TaskStatus.complete
                task.verification = VerificationStatus.verified
                task.artifact_path  = task.replicas[0].artifact_path
                task.artifact_sha256 = task.replicas[0].artifact_sha256
                task.completed_at   = to_iso(utc_now())
            else:
                logger.warning("Task %s: artifact hash mismatch — marking conflict", task.task_id)
                task.status       = TaskStatus.conflict
                task.verification = VerificationStatus.conflict
            return

        # Frame-by-frame comparison
        reference = hash_sets[0]
        all_match = all(h == reference for h in hash_sets[1:])
        if all_match:
            task.status         = TaskStatus.complete
            task.verification   = VerificationStatus.verified
            task.artifact_path  = task.replicas[0].artifact_path
            task.artifact_sha256 = task.replicas[0].artifact_sha256
            task.completed_at   = to_iso(utc_now())
            logger.info("Task %s: verification passed ✓", task.task_id)
        else:
            logger.warning("Task %s: frame hash mismatch — dispatching tie-breaker", task.task_id)
            task.status       = TaskStatus.conflict
            task.verification = VerificationStatus.conflict
            # Add a tie-breaker replica
            task.replicas.append(TaskReplica(
                replica_id=str(uuid.uuid4()),
                status=TaskStatus.queued,
            ))

    def recompute_job_status(self, job: JobRecord) -> None:
        statuses = [task.status for task in job.tasks]
        if all(s == TaskStatus.complete for s in statuses):
            if job.stitch_output:
                job.status = JobStatus.stitching
                stitch_queue.append(job.job_id)
            else:
                job.status = JobStatus.complete
        elif any(s == TaskStatus.running for s in statuses):
            job.status = JobStatus.running
        elif all(s in (TaskStatus.failed, TaskStatus.conflict) for s in statuses):
            job.status = JobStatus.failed
        else:
            job.status = JobStatus.pending
        job.updated_at = to_iso(utc_now())

    def next_queued_replica(self, worker_id: str) -> Optional[Tuple[JobRecord, JobTask, TaskReplica]]:
       
        ordered = sorted(self.jobs.values(), key=lambda j: (j.priority, j.created_at))
        for job in ordered:
            if job.status not in (JobStatus.pending, JobStatus.running):
                continue
            for task in job.tasks:
                if task.status not in (TaskStatus.queued, TaskStatus.running):
                    continue
                for replica in task.replicas:
                    if replica.status != TaskStatus.queued:
                        continue
                    # Don't assign a tie-breaker to a worker already involved in this task
                    prior_workers = {r.worker_id for r in task.replicas if r.worker_id}
                    if worker_id in prior_workers and len(task.replicas) > 1:
                        continue
                    return job, task, replica
        return None


state = OrchestratorState()

# Stitcher Queue
stitch_queue: List[str] = []   # job_ids pending stitching


def stitcher_daemon() -> None:
    
    logger.info("Stitcher daemon started (FFmpeg: %s)", FFMPEG_PATH)
    while True:
        time.sleep(2)
        if not stitch_queue:
            continue

        with DB_LOCK:
            job_id = stitch_queue.pop(0) if stitch_queue else None
        if not job_id:
            continue

        with DB_LOCK:
            job = state.jobs.get(job_id)
        if not job:
            continue

        staging = STAGING_DIR / job_id
        staging.mkdir(parents=True, exist_ok=True)

        logger.info("Stitching job %s — unzipping %d task artifacts", job_id, len(job.tasks))
        try:
            # Unzip all task artifacts
            for task in job.tasks:
                artifact_path = task.artifact_path
                if not artifact_path or not Path(artifact_path).exists():
                    raise FileNotFoundError(f"Artifact missing for task {task.task_id}: {artifact_path}")
                with zipfile.ZipFile(artifact_path, "r") as zf:
                    zf.extractall(staging)

            # List the frames and sort them
            frames = sorted(staging.glob("*.png")) + sorted(staging.glob("*.exr"))
            if not frames:
                raise RuntimeError("No frames found in staging directory after unzip")

            logger.info("Stitching %d frames at %d fps", len(frames), job.output_fps)

            # Write a frame list file for FFmpeg (handles non-sequential numbering safely)
            list_file = staging / "framelist.txt"
            with list_file.open("w") as lf:
                for frame in frames:
                    lf.write(f"file '{frame.resolve()}'\n")
                    lf.write(f"duration {1 / job.output_fps:.6f}\n")

            output_path = FINALS_DIR / f"{job_id}.mp4"
            ffmpeg_cmd = [
                FFMPEG_PATH,
                "-y",                         # overwrite
                "-f", "concat",
                "-safe", "0",
                "-i", str(list_file),
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",   # ensure even dimensions
                "-c:v", "libx264",
                "-pix_fmt", "yuv420p",
                "-crf", "18",                 # high quality
                "-preset", "fast",
                str(output_path),
            ]
            result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=1800)
            if result.returncode != 0:
                raise RuntimeError(f"FFmpeg failed:\n{result.stderr[-500:]}")

            logger.info("Stitched: %s (%.1f MB)", output_path, output_path.stat().st_size / (1024*1024))

            # Upload final video to S3 if configured
            s3_key = None
            if USE_S3:
                s3 = get_s3_client()
                s3_key = f"finals/{job_id}.mp4"
                s3.upload_file(str(output_path), S3_BUCKET_NAME, s3_key)
                logger.info("Uploaded final video to S3: %s", s3_key)

            # Update job record
            with DB_LOCK:
                job = state.jobs.get(job_id)
                if job:
                    job.status          = JobStatus.complete
                    job.final_video_path = str(output_path)
                    job.updated_at      = to_iso(utc_now())
                    state.save_job(job)

            # Cleanup staging
            shutil.rmtree(staging, ignore_errors=True)

        except Exception as exc:
            logger.error("Stitcher failed for job %s: %s", job_id, exc)
            with DB_LOCK:
                job = state.jobs.get(job_id)
                if job:
                    job.status     = JobStatus.failed
                    job.updated_at = to_iso(utc_now())
                    state.save_job(job)
            shutil.rmtree(staging, ignore_errors=True)


# Auth
def verify_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if ORCHESTRATOR_API_KEY and x_api_key != ORCHESTRATOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# File Upload
@app.post("/upload", dependencies=[Depends(verify_api_key)])
async def upload_blend_file(file: UploadFile = File(...)) -> Dict:
    safe_name = os.path.basename(file.filename or "scene.blend")
    if not safe_name.endswith(".blend"):
        raise HTTPException(status_code=400, detail="Only .blend files accepted")
    dest = JOBS_DIR / safe_name
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    sha = read_sha256(dest)
    now = utc_now()
    expires_at = now + timedelta(seconds=UPLOAD_TOKEN_TTL_SECONDS)
    upload = UploadRecord(
        file_name=safe_name,
        sha256=sha,
        size_bytes=dest.stat().st_size,
        upload_token=str(uuid.uuid4()),
        uploaded_at=to_iso(now),
        expires_at=to_iso(expires_at),
    )

    with DB_LOCK:
        state.uploads[safe_name] = upload
        state.save_upload(upload)

    logger.info("Uploaded blend file: %s (%s)", safe_name, sha[:8])

    if USE_S3:
        s3 = get_s3_client()
        s3.upload_file(str(dest), S3_BUCKET_NAME, f"jobs/{safe_name}")

    return {
        "file_name": safe_name,
        "sha256": sha,
        "size_bytes": dest.stat().st_size,
        "upload_token": upload.upload_token,
        "expires_at": upload.expires_at,
    }


@app.get("/uploads", response_model=List[UploadRecord], dependencies=[Depends(verify_api_key)])
def list_uploads() -> List[UploadRecord]:
    with DB_LOCK:
        state.cleanup_expired_uploads()
        uploads = list(state.uploads.values())
    return sorted(uploads, key=lambda item: item.uploaded_at, reverse=True)


# Health 
@app.get("/health")
def health() -> Dict:
    return {
        "status": "ok",
        "time": to_iso(utc_now()),
        "s3_enabled": USE_S3,
        "stitcher_enabled": ENABLE_STITCHER,
        "stitch_queue_depth": len(stitch_queue),
    }


# Jobs
@app.post("/jobs", response_model=JobRecord, dependencies=[Depends(verify_api_key)])
def create_job(payload: CreateJobRequest) -> JobRecord:
    if payload.frame_end < payload.frame_start:
        raise HTTPException(status_code=400, detail="frame_end must be >= frame_start")

    source_file = JOBS_DIR / payload.file_name
    if not source_file.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {payload.file_name}. Upload it first via POST /upload")

    with DB_LOCK:
        state.cleanup_expired_uploads()
        upload = state.uploads.get(payload.file_name)
    if not upload:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File '{payload.file_name}' is not an active uploaded asset. "
                "Upload it via POST /upload and pass the returned upload_token."
            ),
        )
    if upload.upload_token != payload.upload_token:
        raise HTTPException(status_code=401, detail="Invalid upload_token for this file")

    current_sha = read_sha256(source_file)
    if current_sha != upload.sha256:
        raise HTTPException(
            status_code=409,
            detail=(
                "File contents changed after upload. Re-upload the .blend and submit with the new upload_token."
            ),
        )

    tasks: List[JobTask] = []
    cursor = payload.frame_start
    while cursor <= payload.frame_end:
        end = min(cursor + payload.chunk_size - 1, payload.frame_end)
        # Create N replicas per task based on replication_factor
        replicas = [
            TaskReplica(replica_id=str(uuid.uuid4()))
            for _ in range(payload.replication_factor)
        ]
        tasks.append(JobTask(
            task_id     = str(uuid.uuid4()),
            frame_start = cursor,
            frame_end   = end,
            status      = TaskStatus.queued,
            verification = VerificationStatus.pending if payload.replication_factor > 1 else VerificationStatus.skipped,
            replicas    = replicas,
        ))
        cursor = end + 1

    now    = utc_now()
    record = JobRecord(
        job_id             = str(uuid.uuid4()),
        file_name          = payload.file_name,
        file_sha256        = current_sha,
        frame_start        = payload.frame_start,
        frame_end          = payload.frame_end,
        chunk_size         = payload.chunk_size,
        priority           = payload.priority,
        replication_factor = payload.replication_factor,
        output_fps         = payload.output_fps,
        stitch_output      = payload.stitch_output,
        status             = JobStatus.pending,
        created_at         = to_iso(now),
        updated_at         = to_iso(now),
        tasks              = tasks,
    )

    with DB_LOCK:
        state.jobs[record.job_id] = record
        state.save_job(record)

    logger.info("Created job %s | %d tasks × %d replicas", record.job_id, len(tasks), payload.replication_factor)
    return record


@app.get("/jobs", response_model=List[JobRecord])
def list_jobs() -> List[JobRecord]:
    with DB_LOCK:
        return sorted(state.jobs.values(), key=lambda j: (j.priority, j.created_at))


@app.get("/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str) -> JobRecord:
    with DB_LOCK:
        job = state.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job


@app.get("/jobs/{job_id}/download")
def download_final_video(job_id: str) -> FileResponse:
    """Download the assembled .mp4 for a completed job."""
    with DB_LOCK:
        job = state.jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.complete or not job.final_video_path:
        raise HTTPException(status_code=409, detail=f"Job not ready (status: {job.status})")
    path = Path(job.final_video_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Final video file missing on disk")
    return FileResponse(path, filename=f"{job_id}.mp4", media_type="video/mp4")


# File Download
@app.get("/download_job/{filename}")
def download_job(filename: str, info: Optional[str] = None) -> FileResponse:
    path = JOBS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if info == "1" and USE_S3:
        # Return a presigned URL in the header so the agent can download directly
        url = presign_download(f"jobs/{filename}")
        from fastapi.responses import JSONResponse
        return JSONResponse({"presigned_url": url}, headers={"presigned_url": url})
    return FileResponse(path)


# Workers
@app.post("/workers/register", dependencies=[Depends(verify_api_key)])
def register_worker(payload: WorkerRegisterRequest) -> WorkerRecord:
    now    = to_iso(utc_now())
    worker = WorkerRecord(
        worker_id     = payload.worker_id,
        hostname      = payload.hostname,
        blender_image = payload.blender_image,
        cpu_cores     = payload.cpu_cores,
        memory_mb     = payload.memory_mb,
        gpu_vram_mb   = payload.gpu_vram_mb,
        labels        = payload.labels,
        last_seen_at  = now,
        status        = "ready",
    )
    with DB_LOCK:
        state.workers[payload.worker_id] = worker
        state.save_worker(worker)
    logger.info("Worker registered: %s @ %s", payload.worker_id[:8], payload.hostname)
    return worker


@app.post("/workers/heartbeat", dependencies=[Depends(verify_api_key)])
def heartbeat(payload: WorkerHeartbeatRequest) -> WorkerRecord:
    with DB_LOCK:
        worker = state.workers.get(payload.worker_id)
        if not worker:
            raise HTTPException(status_code=404, detail="Worker not registered")
        worker.last_seen_at   = to_iso(utc_now())
        worker.status         = payload.status
        worker.active_task_id = payload.active_task_id
        worker.active_job_id  = payload.active_job_id
        worker.cpu_percent    = payload.cpu_percent
        worker.gpu_percent    = payload.gpu_percent
        state.save_worker(worker)
        return worker


@app.get("/workers", response_model=List[WorkerRecord])
def list_workers() -> List[WorkerRecord]:
    with DB_LOCK:
        return list(state.workers.values())


# Task Claiming
@app.post("/jobs/claim", response_model=ClaimTaskResponse, dependencies=[Depends(verify_api_key)])
def claim_task(payload: ClaimTaskRequest) -> ClaimTaskResponse:
    with DB_LOCK:
        state.cleanup_stale_workers()
        state.release_expired_leases()

        worker = state.workers.get(payload.worker_id)
        if not worker:
            raise HTTPException(status_code=404, detail="Worker not registered")

        result = state.next_queued_replica(payload.worker_id)
        if not result:
            worker.status         = "ready"
            worker.active_task_id = None
            worker.active_job_id  = None
            worker.last_seen_at   = to_iso(utc_now())
            state.save_worker(worker)
            return ClaimTaskResponse()

        job, task, replica = result
        replica.status           = TaskStatus.running
        replica.worker_id        = payload.worker_id
        replica.attempts        += 1
        replica.lease_expires_at = to_iso(utc_now() + timedelta(seconds=TASK_LEASE_SECONDS))

        if task.status == TaskStatus.queued:
            task.status = TaskStatus.running

        worker.active_task_id = task.task_id
        worker.active_job_id  = job.job_id
        worker.status         = "busy"
        worker.last_seen_at   = to_iso(utc_now())

        state.recompute_job_status(job)
        state.save_job(job)
        state.save_worker(worker)

        return ClaimTaskResponse(
            job_id        = job.job_id,
            task_id       = task.task_id,
            replica_id    = replica.replica_id,
            file_name     = job.file_name,
            file_sha256   = job.file_sha256,
            frame_start   = task.frame_start,
            frame_end     = task.frame_end,
            lease_seconds = TASK_LEASE_SECONDS,
        )


# Predesigned upload url
@app.get("/jobs/{job_id}/tasks/{task_id}/upload_url", dependencies=[Depends(verify_api_key)])
def get_upload_url(job_id: str, task_id: str) -> Dict:
    if not USE_S3:
        return {}  # agent falls back to direct upload
    object_key = f"artifacts/{job_id}_{task_id}.zip"
    url = presign_upload(object_key)
    return {"presigned_url": url, "object_key": object_key}


# Artifact upload
@app.post("/jobs/{job_id}/tasks/{task_id}/artifact", dependencies=[Depends(verify_api_key)])
async def upload_task_artifact(
    job_id: str,
    task_id: str,
    checksum_sha256: str = Form(...),
    replica_id: str = Form(default=""),
    file: UploadFile = File(...),
) -> Dict:
    with DB_LOCK:
        job  = state.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        task = next((t for t in job.tasks if t.task_id == task_id), None)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        safe_filename = f"{job_id}_{task_id}_{replica_id}_{os.path.basename(file.filename)}"
        destination   = RESULTS_DIR / safe_filename

        with destination.open("wb") as out:
            shutil.copyfileobj(file.file, out)

        file_hash = read_sha256(destination)
        if file_hash != checksum_sha256:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Artifact checksum mismatch")

        # Hash individual frames inside the zip for per-frame comparison
        frame_hashes = _hash_frames_in_zip(destination)

        # Update the correct replica
        replica = next((r for r in task.replicas if r.replica_id == replica_id), None)
        if not replica and task.replicas:
            # Legacy path: single replica, no replica_id sent
            replica = task.replicas[0]
        if not replica:
            raise HTTPException(status_code=404, detail="Replica not found")

        replica.status          = TaskStatus.complete
        replica.artifact_path   = str(destination)
        replica.artifact_sha256 = file_hash
        replica.frame_hashes    = frame_hashes
        replica.completed_at    = to_iso(utc_now())
        replica.lease_expires_at = None

        # Free the worker
        if replica.worker_id and replica.worker_id in state.workers:
            w = state.workers[replica.worker_id]
            w.status         = "ready"
            w.active_task_id = None
            w.active_job_id  = None
            w.last_seen_at   = to_iso(utc_now())
            state.save_worker(w)

        state.recompute_task_status(task)
        state.recompute_job_status(job)
        state.save_job(job)

    logger.info("Artifact received: job=%s task=%s replica=%s verification=%s",
                job_id, task_id, replica_id, task.verification)
    return {"status": "accepted", "task_id": task_id, "job_id": job_id, "verification": task.verification}


@app.post("/jobs/{job_id}/tasks/{task_id}/artifact_notify", dependencies=[Depends(verify_api_key)])
def artifact_notify(job_id: str, task_id: str, body: Dict) -> Dict:
   
    if not USE_S3:
        raise HTTPException(status_code=400, detail="S3 not configured")
    checksum   = body.get("checksum_sha256", "")
    object_key = body.get("object_key", "")
    replica_id = body.get("replica_id", "")
    if not object_key:
        raise HTTPException(status_code=400, detail="Missing object_key")
    local_path = RESULTS_DIR / os.path.basename(object_key)

    s3 = get_s3_client()
    s3.download_file(S3_BUCKET_NAME, object_key, str(local_path))

    file_hash = read_sha256(local_path)
    if file_hash != checksum:
        local_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Checksum mismatch after S3 download")


    with DB_LOCK:
        job  = state.jobs.get(job_id)
        task = next((t for t in job.tasks if t.task_id == task_id), None) if job else None
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        replica = None
        if replica_id:
            replica = next((r for r in task.replicas if r.replica_id == replica_id), None)
        if not replica:
            replica = next((r for r in task.replicas if r.status == TaskStatus.running), None)
        if replica:
            replica.status          = TaskStatus.complete
            replica.artifact_path   = str(local_path)
            replica.artifact_sha256 = file_hash
            replica.frame_hashes    = _hash_frames_in_zip(local_path)
            replica.completed_at    = to_iso(utc_now())
            replica.lease_expires_at = None
        state.recompute_task_status(task)
        state.recompute_job_status(job)
        state.save_job(job)

    return {"status": "accepted"}


# Task Failure
@app.post("/jobs/{job_id}/tasks/{task_id}/fail", dependencies=[Depends(verify_api_key)])
def fail_task(job_id: str, task_id: str, payload: TaskFailureRequest) -> Dict:
    with DB_LOCK:
        job  = state.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        task = next((t for t in job.tasks if t.task_id == task_id), None)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        # Mark the running replica as failed / re-queued
        replica = next(
            (r for r in task.replicas if r.worker_id == payload.worker_id and r.status == TaskStatus.running),
            None,
        )
        if replica:
            if payload.retryable and replica.attempts < MAX_TASK_ATTEMPTS:
                replica.status = TaskStatus.queued
            else:
                replica.status = TaskStatus.failed
            replica.lease_expires_at = None
            replica.worker_id = None

        worker = state.workers.get(payload.worker_id)
        if worker:
            worker.status         = "ready"
            worker.active_task_id = None
            worker.active_job_id  = None
            worker.last_seen_at   = to_iso(utc_now())
            state.save_worker(worker)

        state.recompute_task_status(task)
        state.recompute_job_status(job)
        state.save_job(job)

    logger.warning("Task failed: job=%s task=%s retryable=%s reason=%s",
                   job_id, task_id, payload.retryable, payload.reason)
    return {"status": "recorded", "job_id": job_id, "task_id": task_id}


# Metrics
@app.get("/metrics")
def metrics() -> Dict:
    with DB_LOCK:
        jobs    = list(state.jobs.values())
        workers = list(state.workers.values())

    task_total = task_queued = task_running = task_complete = task_failed = task_conflict = 0
    for job in jobs:
        for task in job.tasks:
            task_total += 1
            if task.status == TaskStatus.queued:    task_queued += 1
            elif task.status == TaskStatus.running:  task_running += 1
            elif task.status == TaskStatus.complete: task_complete += 1
            elif task.status == TaskStatus.failed:   task_failed += 1
            elif task.status == TaskStatus.conflict: task_conflict += 1

    return {
        "jobs_total":         len(jobs),
        "jobs_pending":       sum(1 for j in jobs if j.status == JobStatus.pending),
        "jobs_running":       sum(1 for j in jobs if j.status == JobStatus.running),
        "jobs_stitching":     sum(1 for j in jobs if j.status == JobStatus.stitching),
        "jobs_complete":      sum(1 for j in jobs if j.status == JobStatus.complete),
        "jobs_failed":        sum(1 for j in jobs if j.status == JobStatus.failed),
        "workers_total":      len(workers),
        "workers_ready":      sum(1 for w in workers if w.status == "ready"),
        "workers_busy":       sum(1 for w in workers if w.status == "busy"),
        "workers_stale":      sum(1 for w in workers if w.status == "stale"),
        "workers_error":      sum(1 for w in workers if w.status == "error"),
        "tasks_total":        task_total,
        "tasks_queued":       task_queued,
        "tasks_running":      task_running,
        "tasks_complete":     task_complete,
        "tasks_failed":       task_failed,
        "tasks_conflict":     task_conflict,
        "stitch_queue_depth": len(stitch_queue),
    }


# Hash Frames
def _hash_frames_in_zip(zip_path: Path) -> Dict[str, str]:
    """Return {filename: sha256} for every file inside the zip."""
    hashes: Dict[str, str] = {}
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                data = zf.read(name)
                hashes[name] = hashlib.sha256(data).hexdigest()
    except Exception:
        pass
    return hashes


# STARTUP
@app.on_event("startup")
def on_startup() -> None:
    if ENABLE_STITCHER:
        t = threading.Thread(target=stitcher_daemon, daemon=True, name="stitcher")
        t.start()
        logger.info("Stitcher daemon launched")
    if USE_S3:
        logger.info("S3/R2 storage enabled: bucket=%s endpoint=%s", S3_BUCKET_NAME, S3_ENDPOINT_URL)
    else:
        logger.info("S3/R2 not configured — using local file storage")


if __name__ == "__main__":
    if ENABLE_NGROK:
        if ngrok is None:
            raise RuntimeError("ENABLE_NGROK=true but ngrok is not installed")
        if not NGROK_AUTH_TOKEN:
            raise RuntimeError("ENABLE_NGROK=true but NGROK_AUTH_TOKEN is not set")
        ngrok.set_auth_token(NGROK_AUTH_TOKEN)
        listener = ngrok.forward(PORT)
        print(f"\nPublic URL: {listener.url()}\n")

    uvicorn.run(app, host="0.0.0.0", port=PORT)
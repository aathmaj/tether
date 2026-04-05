import hashlib
import json
import logging
import os
import sqlite3
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

load_dotenv()

try:
    import ngrok
except Exception:
    ngrok = None


PORT = int(os.getenv("PORT", "8000"))
ORCHESTRATOR_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")
ENABLE_NGROK = os.getenv("ENABLE_NGROK", "false").lower() == "true"
NGROK_AUTH_TOKEN = os.getenv("NGROK_AUTH_TOKEN")
TASK_LEASE_SECONDS = int(os.getenv("TASK_LEASE_SECONDS", "180"))
WORKER_STALE_SECONDS = int(os.getenv("WORKER_STALE_SECONDS", "45"))
MAX_TASK_ATTEMPTS = int(os.getenv("MAX_TASK_ATTEMPTS", "3"))
STATE_DB_PATH = os.getenv("STATE_DB_PATH", "./orchestrator_state.db")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("tether.orchestrator")

BASE_DIR = Path(__file__).resolve().parent
JOBS_DIR = BASE_DIR / "jobs"
RESULTS_DIR = BASE_DIR / "results"
DB_LOCK = threading.Lock()

JOBS_DIR.mkdir(parents=True, exist_ok=True)
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Tether Orchestrator", version="2.0.0")


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


class JobStatus(str, Enum):
    pending = "pending"
    running = "running"
    complete = "complete"
    failed = "failed"


class TaskStatus(str, Enum):
    queued = "queued"
    running = "running"
    complete = "complete"
    failed = "failed"


class CreateJobRequest(BaseModel):
    file_name: str = Field(description="Blend file expected under ./jobs")
    frame_start: int = Field(ge=1)
    frame_end: int = Field(ge=1)
    chunk_size: int = Field(default=5, ge=1)
    priority: int = Field(default=100, ge=0, le=1000)


class JobTask(BaseModel):
    task_id: str
    frame_start: int
    frame_end: int
    status: TaskStatus
    assigned_worker_id: Optional[str] = None
    lease_expires_at: Optional[str] = None
    artifact_path: Optional[str] = None
    artifact_sha256: Optional[str] = None
    completed_at: Optional[str] = None
    attempts: int = 0


class JobRecord(BaseModel):
    job_id: str
    file_name: str
    file_sha256: str
    frame_start: int
    frame_end: int
    chunk_size: int
    priority: int
    status: JobStatus
    created_at: str
    updated_at: str
    tasks: List[JobTask]


class WorkerRegisterRequest(BaseModel):
    worker_id: str
    hostname: str
    blender_image: str
    cpu_cores: int = Field(ge=1)
    memory_mb: int = Field(ge=128)
    labels: List[str] = Field(default_factory=list)


class WorkerHeartbeatRequest(BaseModel):
    worker_id: str
    active_task_id: Optional[str] = None
    active_job_id: Optional[str] = None
    status: str = "ready"


class WorkerRecord(BaseModel):
    worker_id: str
    hostname: str
    blender_image: str
    cpu_cores: int
    memory_mb: int
    labels: List[str]
    last_seen_at: str
    status: str
    active_task_id: Optional[str] = None
    active_job_id: Optional[str] = None


class ClaimTaskRequest(BaseModel):
    worker_id: str


class ClaimTaskResponse(BaseModel):
    job_id: Optional[str] = None
    task_id: Optional[str] = None
    file_name: Optional[str] = None
    file_sha256: Optional[str] = None
    frame_start: Optional[int] = None
    frame_end: Optional[int] = None
    lease_seconds: int = TASK_LEASE_SECONDS


class TaskFailureRequest(BaseModel):
    worker_id: str
    reason: str = Field(min_length=1, max_length=2048)
    retryable: bool = True


class OrchestratorState:
    def __init__(self) -> None:
        self.jobs: Dict[str, JobRecord] = {}
        self.workers: Dict[str, WorkerRecord] = {}
        self.db_path = str((BASE_DIR / STATE_DB_PATH).resolve()) if not os.path.isabs(STATE_DB_PATH) else STATE_DB_PATH
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self._load_state()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self.conn.commit()

    def _load_state(self) -> None:
        for (payload,) in self.conn.execute("SELECT payload FROM jobs"):
            job = JobRecord.model_validate(json.loads(payload))
            self.jobs[job.job_id] = job
        for (payload,) in self.conn.execute("SELECT payload FROM workers"):
            worker = WorkerRecord.model_validate(json.loads(payload))
            self.workers[worker.worker_id] = worker
        logger.info("Loaded %d jobs and %d workers from %s", len(self.jobs), len(self.workers), self.db_path)

    def save_job(self, job: JobRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO jobs (job_id, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (job.job_id, job.model_dump_json(), job.updated_at),
        )
        self.conn.commit()

    def save_worker(self, worker: WorkerRecord) -> None:
        self.conn.execute(
            """
            INSERT INTO workers (worker_id, payload, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (worker.worker_id, worker.model_dump_json(), worker.last_seen_at),
        )
        self.conn.commit()

    def cleanup_stale_workers(self) -> None:
        now = utc_now()
        stale_ids: List[str] = []
        for worker_id, worker in self.workers.items():
            seen = datetime.fromisoformat(worker.last_seen_at)
            if now - seen > timedelta(seconds=WORKER_STALE_SECONDS):
                stale_ids.append(worker_id)

        for worker_id in stale_ids:
            self.workers[worker_id].status = "stale"
            self.save_worker(self.workers[worker_id])

    def release_expired_leases(self) -> None:
        now = utc_now()
        for job in self.jobs.values():
            for task in job.tasks:
                if task.status != TaskStatus.running or not task.lease_expires_at:
                    continue
                expires = datetime.fromisoformat(task.lease_expires_at)
                if now > expires:
                    task.status = TaskStatus.failed if task.attempts >= MAX_TASK_ATTEMPTS else TaskStatus.queued
                    task.assigned_worker_id = None
                    task.lease_expires_at = None
                    job.updated_at = to_iso(now)
                    self.recompute_job_status(job)
                    self.save_job(job)

    def recompute_job_status(self, job: JobRecord) -> None:
        if all(task.status == TaskStatus.complete for task in job.tasks):
            job.status = JobStatus.complete
        elif any(task.status == TaskStatus.running for task in job.tasks):
            job.status = JobStatus.running
        elif all(task.status == TaskStatus.failed for task in job.tasks):
            job.status = JobStatus.failed
        else:
            job.status = JobStatus.pending
        job.updated_at = to_iso(utc_now())


state = OrchestratorState()


def verify_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if ORCHESTRATOR_API_KEY and x_api_key != ORCHESTRATOR_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "time": to_iso(utc_now())}


@app.post("/jobs", response_model=JobRecord)
def create_job(payload: CreateJobRequest) -> JobRecord:
    if payload.frame_end < payload.frame_start:
        raise HTTPException(status_code=400, detail="frame_end must be >= frame_start")

    source_file = JOBS_DIR / payload.file_name
    if not source_file.exists():
        raise HTTPException(status_code=404, detail=f"Job file not found: {payload.file_name}")

    tasks: List[JobTask] = []
    cursor = payload.frame_start
    while cursor <= payload.frame_end:
        end = min(cursor + payload.chunk_size - 1, payload.frame_end)
        tasks.append(
            JobTask(
                task_id=str(uuid.uuid4()),
                frame_start=cursor,
                frame_end=end,
                status=TaskStatus.queued,
            )
        )
        cursor = end + 1

    now = utc_now()
    record = JobRecord(
        job_id=str(uuid.uuid4()),
        file_name=payload.file_name,
        file_sha256=read_sha256(source_file),
        frame_start=payload.frame_start,
        frame_end=payload.frame_end,
        chunk_size=payload.chunk_size,
        priority=payload.priority,
        status=JobStatus.pending,
        created_at=to_iso(now),
        updated_at=to_iso(now),
        tasks=tasks,
    )

    with DB_LOCK:
        state.jobs[record.job_id] = record
        state.save_job(record)

    return record


@app.get("/jobs", response_model=List[JobRecord])
def list_jobs() -> List[JobRecord]:
    with DB_LOCK:
        jobs = sorted(state.jobs.values(), key=lambda job: (job.priority, job.created_at))
        return jobs


@app.get("/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str) -> JobRecord:
    with DB_LOCK:
        job = state.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        return job


@app.get("/download_job/{filename}")
def download_job(filename: str) -> FileResponse:
    path = JOBS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@app.post("/workers/register", dependencies=[Depends(verify_api_key)])
def register_worker(payload: WorkerRegisterRequest) -> WorkerRecord:
    now = to_iso(utc_now())
    worker = WorkerRecord(
        worker_id=payload.worker_id,
        hostname=payload.hostname,
        blender_image=payload.blender_image,
        cpu_cores=payload.cpu_cores,
        memory_mb=payload.memory_mb,
        labels=payload.labels,
        last_seen_at=now,
        status="ready",
    )
    with DB_LOCK:
        state.workers[payload.worker_id] = worker
        state.save_worker(worker)
    return worker


@app.post("/workers/heartbeat", dependencies=[Depends(verify_api_key)])
def heartbeat(payload: WorkerHeartbeatRequest) -> WorkerRecord:
    with DB_LOCK:
        worker = state.workers.get(payload.worker_id)
        if not worker:
            raise HTTPException(status_code=404, detail="Worker not registered")
        worker.last_seen_at = to_iso(utc_now())
        worker.status = payload.status
        worker.active_task_id = payload.active_task_id
        worker.active_job_id = payload.active_job_id
        state.save_worker(worker)
        return worker


@app.post("/jobs/claim", response_model=ClaimTaskResponse, dependencies=[Depends(verify_api_key)])
def claim_task(payload: ClaimTaskRequest) -> ClaimTaskResponse:
    with DB_LOCK:
        state.cleanup_stale_workers()
        state.release_expired_leases()
        worker = state.workers.get(payload.worker_id)
        if not worker:
            raise HTTPException(status_code=404, detail="Worker not registered")

        ordered_jobs = sorted(state.jobs.values(), key=lambda job: (job.priority, job.created_at))
        for job in ordered_jobs:
            for task in job.tasks:
                if task.status != TaskStatus.queued:
                    continue

                task.status = TaskStatus.running
                task.assigned_worker_id = payload.worker_id
                task.attempts += 1
                task.lease_expires_at = to_iso(utc_now() + timedelta(seconds=TASK_LEASE_SECONDS))
                worker.active_task_id = task.task_id
                worker.active_job_id = job.job_id
                worker.status = "busy"
                worker.last_seen_at = to_iso(utc_now())
                state.recompute_job_status(job)
                state.save_job(job)
                state.save_worker(worker)

                return ClaimTaskResponse(
                    job_id=job.job_id,
                    task_id=task.task_id,
                    file_name=job.file_name,
                    file_sha256=job.file_sha256,
                    frame_start=task.frame_start,
                    frame_end=task.frame_end,
                    lease_seconds=TASK_LEASE_SECONDS,
                )

        worker.status = "ready"
        worker.active_task_id = None
        worker.active_job_id = None
        worker.last_seen_at = to_iso(utc_now())
        state.save_worker(worker)
        return ClaimTaskResponse()


@app.post(
    "/jobs/{job_id}/tasks/{task_id}/artifact",
    dependencies=[Depends(verify_api_key)],
)
async def upload_task_artifact(
    job_id: str,
    task_id: str,
    checksum_sha256: str = Form(...),
    file: UploadFile = File(...),
) -> Dict[str, str]:
    with DB_LOCK:
        job = state.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        task = next((task for task in job.tasks if task.task_id == task_id), None)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != TaskStatus.running:
            raise HTTPException(status_code=409, detail="Task is not in running state")

        safe_filename = f"{job_id}_{task_id}_{os.path.basename(file.filename)}"
        destination = RESULTS_DIR / safe_filename

        with destination.open("wb") as out:
            shutil.copyfileobj(file.file, out)

        file_hash = read_sha256(destination)
        if file_hash != checksum_sha256:
            destination.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail="Artifact checksum mismatch")

        task.status = TaskStatus.complete
        task.artifact_path = str(destination)
        task.artifact_sha256 = file_hash
        task.completed_at = to_iso(utc_now())
        task.lease_expires_at = None

        if task.assigned_worker_id and task.assigned_worker_id in state.workers:
            worker = state.workers[task.assigned_worker_id]
            worker.status = "ready"
            worker.active_task_id = None
            worker.active_job_id = None
            worker.last_seen_at = to_iso(utc_now())
            state.save_worker(worker)

        state.recompute_job_status(job)
        state.save_job(job)

    return {"status": "accepted", "task_id": task_id, "job_id": job_id}


@app.post(
    "/jobs/{job_id}/tasks/{task_id}/fail",
    dependencies=[Depends(verify_api_key)],
)
def fail_task(job_id: str, task_id: str, payload: TaskFailureRequest) -> Dict[str, str]:
    with DB_LOCK:
        job = state.jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")

        task = next((task for task in job.tasks if task.task_id == task_id), None)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")
        if task.status != TaskStatus.running:
            raise HTTPException(status_code=409, detail="Task is not in running state")

        task.assigned_worker_id = None
        task.lease_expires_at = None

        if payload.retryable and task.attempts < MAX_TASK_ATTEMPTS:
            task.status = TaskStatus.queued
        else:
            task.status = TaskStatus.failed
        job.updated_at = to_iso(utc_now())

        worker = state.workers.get(payload.worker_id)
        if worker:
            worker.status = "ready"
            worker.active_task_id = None
            worker.active_job_id = None
            worker.last_seen_at = to_iso(utc_now())
            state.save_worker(worker)

        state.recompute_job_status(job)
        state.save_job(job)

    logger.warning(
        "Task failed: job_id=%s task_id=%s retryable=%s reason=%s",
        job_id,
        task_id,
        payload.retryable,
        payload.reason,
    )
    return {"status": "recorded", "job_id": job_id, "task_id": task_id}


@app.get("/metrics")
def metrics() -> Dict[str, int]:
    with DB_LOCK:
        jobs = list(state.jobs.values())
        workers = list(state.workers.values())

    task_total = 0
    task_queued = 0
    task_running = 0
    task_complete = 0
    task_failed = 0

    for job in jobs:
        task_total += len(job.tasks)
        for task in job.tasks:
            if task.status == TaskStatus.queued:
                task_queued += 1
            elif task.status == TaskStatus.running:
                task_running += 1
            elif task.status == TaskStatus.complete:
                task_complete += 1
            elif task.status == TaskStatus.failed:
                task_failed += 1

    return {
        "jobs_total": len(jobs),
        "jobs_pending": sum(1 for job in jobs if job.status == JobStatus.pending),
        "jobs_running": sum(1 for job in jobs if job.status == JobStatus.running),
        "jobs_complete": sum(1 for job in jobs if job.status == JobStatus.complete),
        "jobs_failed": sum(1 for job in jobs if job.status == JobStatus.failed),
        "workers_total": len(workers),
        "workers_ready": sum(1 for worker in workers if worker.status == "ready"),
        "workers_busy": sum(1 for worker in workers if worker.status == "busy"),
        "workers_stale": sum(1 for worker in workers if worker.status == "stale"),
        "workers_error": sum(1 for worker in workers if worker.status == "error"),
        "tasks_total": task_total,
        "tasks_queued": task_queued,
        "tasks_running": task_running,
        "tasks_complete": task_complete,
        "tasks_failed": task_failed,
    }


if __name__ == "__main__":
    if ENABLE_NGROK:
        if ngrok is None:
            raise RuntimeError("ENABLE_NGROK=true but ngrok package is not installed")
        if not NGROK_AUTH_TOKEN:
            raise RuntimeError("ENABLE_NGROK=true but NGROK_AUTH_TOKEN is not set")
        ngrok.set_auth_token(NGROK_AUTH_TOKEN)
        listener = ngrok.forward(PORT)
        print("\nLive")
        print(f" {listener.url()}")
        print("--------------------------------------------\n")

    uvicorn.run(app, host="0.0.0.0", port=PORT)
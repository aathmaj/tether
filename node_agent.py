import hashlib
import os
import signal
import socket
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

load_dotenv()


ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8000").rstrip("/")
ORCHESTRATOR_API_KEY = os.getenv("ORCHESTRATOR_API_KEY", "")
IMAGE_NAME = os.getenv("BLENDER_IMAGE", "linuxserver/blender:latest")
LOCAL_DIR = Path(os.getenv("LOCAL_DIR", "./node_data")).resolve()
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "10"))
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
WORKER_ID = os.getenv("WORKER_ID", str(uuid.uuid4()))
RUNNING = True


def request_stop(signum, _frame) -> None:
    global RUNNING
    RUNNING = False
    print(f"Received signal {signum}, shutting down worker loop...")


def setup_node() -> bool:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    (LOCAL_DIR / "frames").mkdir(parents=True, exist_ok=True)
    (LOCAL_DIR / "artifacts").mkdir(parents=True, exist_ok=True)

    if DRY_RUN:
        print("DRY_RUN enabled: blender jobs will be simulated.")
        return True

    print("Checking Docker status...")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
    except Exception:
        print("ERROR: Docker is not running. Please start Docker Desktop.")
        return False
    return True


def build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if ORCHESTRATOR_API_KEY:
        session.headers.update({"X-API-Key": ORCHESTRATOR_API_KEY})
    return session


def file_sha256(file_path: Path) -> str:
    sha = hashlib.sha256()
    with file_path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def detect_memory_mb() -> int:
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int((pages * page_size) / (1024 * 1024))
    except Exception:
        return 4096


def register_worker(session: requests.Session) -> None:
    payload = {
        "worker_id": WORKER_ID,
        "hostname": socket.gethostname(),
        "blender_image": IMAGE_NAME,
        "cpu_cores": os.cpu_count() or 1,
        "memory_mb": detect_memory_mb(),
        "labels": ["linux", "docker", "blender"],
    }
    response = session.post(f"{ORCHESTRATOR_URL}/workers/register", json=payload, timeout=15)
    if response.status_code >= 300:
        raise RuntimeError(f"Failed worker registration: {response.status_code} {response.text}")
    print(f"Registered worker {WORKER_ID} on {ORCHESTRATOR_URL}")


def heartbeat(
    session: requests.Session,
    status: str,
    active_task_id: Optional[str],
    active_job_id: Optional[str],
) -> None:
    payload = {
        "worker_id": WORKER_ID,
        "status": status,
        "active_task_id": active_task_id,
        "active_job_id": active_job_id,
    }
    session.post(f"{ORCHESTRATOR_URL}/workers/heartbeat", json=payload, timeout=10)


def claim_task(session: requests.Session) -> Dict[str, Optional[str]]:
    payload = {"worker_id": WORKER_ID}
    response = session.post(f"{ORCHESTRATOR_URL}/jobs/claim", json=payload, timeout=20)
    if response.status_code >= 300:
        raise RuntimeError(f"Failed task claim: {response.status_code} {response.text}")
    return response.json()


def ensure_job_file(session: requests.Session, filename: str, expected_hash: str) -> Path:
    target = LOCAL_DIR / filename
    if target.exists() and file_sha256(target) == expected_hash:
        return target

    print(f"Downloading {filename}...")
    response = session.get(f"{ORCHESTRATOR_URL}/download_job/{filename}", timeout=120)
    response.raise_for_status()
    with target.open("wb") as f:
        f.write(response.content)

    actual_hash = file_sha256(target)
    if actual_hash != expected_hash:
        target.unlink(missing_ok=True)
        raise RuntimeError("Downloaded file checksum mismatch")
    return target


def simulate_render(frame_start: int, frame_end: int, prefix: str) -> List[Path]:
    output_paths: List[Path] = []
    for frame in range(frame_start, frame_end + 1):
        path = LOCAL_DIR / "frames" / f"{prefix}{frame:04d}.png"
        path.write_bytes(f"simulated frame {frame}\n".encode("utf-8"))
        output_paths.append(path)
    return output_paths


def run_blender_render(job_file: Path, frame_start: int, frame_end: int, prefix: str) -> List[Path]:
    if DRY_RUN:
        return simulate_render(frame_start, frame_end, prefix)

    print(f"Rendering frames {frame_start}-{frame_end}...")
    output_pattern = f"/data/frames/{prefix}####"
    docker_cmd = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{LOCAL_DIR}:/data",
        IMAGE_NAME,
        "blender",
        "-b",
        f"/data/{job_file.name}",
        "-o",
        output_pattern,
        "-s",
        str(frame_start),
        "-e",
        str(frame_end),
        "-a",
    ]
    subprocess.run(docker_cmd, check=True)

    produced: List[Path] = []
    for frame in range(frame_start, frame_end + 1):
        candidate = LOCAL_DIR / "frames" / f"{prefix}{frame:04d}.png"
        if candidate.exists():
            produced.append(candidate)

    if not produced:
        raise RuntimeError("No render output frames produced")
    return produced


def package_frames(job_id: str, task_id: str, frame_files: List[Path]) -> Path:
    zip_path = LOCAL_DIR / "artifacts" / f"{job_id}_{task_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
        for frame in frame_files:
            zipf.write(frame, arcname=frame.name)
    return zip_path


def upload_artifact(session: requests.Session, job_id: str, task_id: str, artifact_path: Path) -> None:
    checksum = file_sha256(artifact_path)
    with artifact_path.open("rb") as f:
        response = session.post(
            f"{ORCHESTRATOR_URL}/jobs/{job_id}/tasks/{task_id}/artifact",
            data={"checksum_sha256": checksum},
            files={"file": (artifact_path.name, f, "application/zip")},
            timeout=120,
        )
    if response.status_code >= 300:
        raise RuntimeError(f"Failed artifact upload: {response.status_code} {response.text}")


def report_task_failure(
    session: requests.Session,
    job_id: str,
    task_id: str,
    reason: str,
    retryable: bool = True,
) -> None:
    payload = {
        "worker_id": WORKER_ID,
        "reason": reason[:2048],
        "retryable": retryable,
    }
    response = session.post(
        f"{ORCHESTRATOR_URL}/jobs/{job_id}/tasks/{task_id}/fail",
        json=payload,
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Failed task failure report: {response.status_code} {response.text}")


def run_agent() -> None:
    session = build_session()
    register_worker(session)

    last_heartbeat = 0.0
    active_job: Optional[str] = None
    active_task: Optional[str] = None

    while RUNNING:
        now = time.time()
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            heartbeat(
                session,
                status="busy" if active_task else "ready",
                active_task_id=active_task,
                active_job_id=active_job,
            )
            last_heartbeat = now

        try:
            assignment = claim_task(session)
            job_id = assignment.get("job_id")
            task_id = assignment.get("task_id")
            if not job_id or not task_id:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            active_job = job_id
            active_task = task_id
            frame_start = int(assignment["frame_start"])
            frame_end = int(assignment["frame_end"])
            file_name = assignment["file_name"]
            file_sha = assignment["file_sha256"]
            prefix = f"{job_id}_{task_id}_"

            job_file = ensure_job_file(session, file_name, file_sha)
            frame_files = run_blender_render(job_file, frame_start, frame_end, prefix)
            artifact = package_frames(job_id, task_id, frame_files)
            upload_artifact(session, job_id, task_id, artifact)
            print(f"Completed task {task_id} for job {job_id}")

            active_job = None
            active_task = None
            heartbeat(session, status="ready", active_task_id=None, active_job_id=None)

        except Exception as exc:
            print(f"Agent loop error: {exc}")
            try:
                if active_job and active_task:
                    report_task_failure(
                        session,
                        job_id=active_job,
                        task_id=active_task,
                        reason=str(exc),
                        retryable=True,
                    )
                heartbeat(
                    session,
                    status="error",
                    active_task_id=active_task,
                    active_job_id=active_job,
                )
            except Exception as report_exc:
                print(f"Failed to report worker error state: {report_exc}")
            active_job = None
            active_task = None
            time.sleep(min(POLL_INTERVAL_SECONDS * 2, 30))

    try:
        heartbeat(session, status="stopping", active_task_id=active_task, active_job_id=active_job)
    except Exception:
        pass


if __name__ == "__main__":
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if setup_node():
        run_agent()
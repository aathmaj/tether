import hashlib
import os
import signal
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from pathlib import Path
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import pynvml
    pynvml.nvmlInit()
    NVML_AVAILABLE = True
except Exception:
    NVML_AVAILABLE = False

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

load_dotenv()

# CONFIG
ORCHESTRATOR_URL         = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8000").rstrip("/")
ORCHESTRATOR_API_KEY     = os.getenv("ORCHESTRATOR_API_KEY", "")
IMAGE_NAME               = os.getenv("BLENDER_IMAGE", "linuxserver/blender:latest")
LOCAL_DIR                = Path(os.getenv("LOCAL_DIR", "./node_data")).resolve()
POLL_INTERVAL_SECONDS    = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
HEARTBEAT_INTERVAL_SECONDS = int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "10"))
DRY_RUN                  = os.getenv("DRY_RUN", "false").lower() == "true"
WORKER_ID                = os.getenv("WORKER_ID", str(uuid.uuid4()))

# Headroom thresholds 
CPU_CLAIM_THRESHOLD      = float(os.getenv("CPU_CLAIM_THRESHOLD", "60.0"))   
GPU_CLAIM_THRESHOLD      = float(os.getenv("GPU_CLAIM_THRESHOLD", "65.0"))   
# If host load exceeds these while a job is running, pause the container
CPU_PAUSE_THRESHOLD      = float(os.getenv("CPU_PAUSE_THRESHOLD", "80.0"))   
GPU_PAUSE_THRESHOLD      = float(os.getenv("GPU_PAUSE_THRESHOLD", "80.0"))  

HEADROOM_CHECK_INTERVAL  = int(os.getenv("HEADROOM_CHECK_INTERVAL", "3"))

RUNNING = True

# Shared state for headroom monitor
_lock               = threading.Lock()
_active_container   : Optional[str] = None   # Docker container ID
_container_paused   : bool          = False
_current_cpu        : float         = 0.0
_current_gpu        : float         = 0.0    # 0.0 if no GPU


# Signal handling
def request_stop(signum, _frame) -> None:
    global RUNNING
    RUNNING = False
    print(f"[agent] Received signal {signum}, shutting down...")


# Headroom
def sample_cpu_percent() -> float:
    
    if not PSUTIL_AVAILABLE:
        return 0.0
    return psutil.cpu_percent(interval=1)


def sample_gpu_percent() -> float:
    
    if not NVML_AVAILABLE:
        return 0.0
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util   = pynvml.nvmlDeviceGetUtilizationRates(handle)
        return float(util.gpu)
    except Exception:
        return 0.0


def sample_gpu_vram_free_mb() -> int:
    
    if not NVML_AVAILABLE:
        return 99999
    try:
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        info   = pynvml.nvmlDeviceGetMemoryInfo(handle)
        return int(info.free / (1024 * 1024))
    except Exception:
        return 99999


def host_is_idle_enough() -> bool:
 
    cpu = sample_cpu_percent()
    gpu = sample_gpu_percent()
    with _lock:
        global _current_cpu, _current_gpu
        _current_cpu = cpu
        _current_gpu = gpu
    cpu_ok = cpu < CPU_CLAIM_THRESHOLD
    gpu_ok = gpu < GPU_CLAIM_THRESHOLD
    if not cpu_ok:
        print(f"[headroom] CPU {cpu:.1f}% >= {CPU_CLAIM_THRESHOLD}% — skipping claim")
    if not gpu_ok:
        print(f"[headroom] GPU {gpu:.1f}% >= {GPU_CLAIM_THRESHOLD}% — skipping claim")
    return cpu_ok and gpu_ok


# Bg headroom monitor
def headroom_monitor() -> None:
    #runs in daemon
    global _container_paused

    while RUNNING:
        time.sleep(HEADROOM_CHECK_INTERVAL)

        cpu = sample_cpu_percent()
        gpu = sample_gpu_percent()

        with _lock:
            global _current_cpu, _current_gpu
            _current_cpu = cpu
            _current_gpu = gpu
            container_id  = _active_container
            is_paused     = _container_paused

        if container_id is None:
            continue  # nothing running

        host_overloaded = (cpu >= CPU_PAUSE_THRESHOLD or gpu >= GPU_PAUSE_THRESHOLD)

        if host_overloaded and not is_paused:
            print(f"[headroom] Host overloaded (CPU {cpu:.0f}% GPU {gpu:.0f}%) — pausing container {container_id[:12]}")
            try:
                subprocess.run(["docker", "pause", container_id], check=True, capture_output=True)
                with _lock:
                    _container_paused = True
            except Exception as exc:
                print(f"[headroom] docker pause failed: {exc}")

        elif not host_overloaded and is_paused:
            print(f"[headroom] Host recovered (CPU {cpu:.0f}% GPU {gpu:.0f}%) — unpausing container {container_id[:12]}")
            try:
                subprocess.run(["docker", "unpause", container_id], check=True, capture_output=True)
                with _lock:
                    _container_paused = False
            except Exception as exc:
                print(f"[headroom] docker unpause failed: {exc}")


def start_headroom_monitor() -> None:
    t = threading.Thread(target=headroom_monitor, daemon=True, name="headroom-monitor")
    t.start()
    print("[agent] Headroom monitor started")


# Setup
def setup_node() -> bool:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    (LOCAL_DIR / "frames").mkdir(parents=True, exist_ok=True)
    (LOCAL_DIR / "artifacts").mkdir(parents=True, exist_ok=True)

    if not PSUTIL_AVAILABLE:
        print("[warn] psutil not installed — CPU headroom detection disabled. Run: pip install psutil")
    if not NVML_AVAILABLE:
        print("[warn] pynvml not installed or no NVIDIA GPU — GPU detection disabled. Run: pip install pynvml")

    if DRY_RUN:
        print("[agent] DRY_RUN enabled: blender jobs will be simulated.")
        return True

    print("[agent] Checking Docker status...")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True)
    except Exception:
        print("[ERROR] Docker is not running. Please start Docker Desktop.")
        return False

    print(f"[agent] Worker ID : {WORKER_ID}")
    print(f"[agent] CPU claim threshold : {CPU_CLAIM_THRESHOLD}%")
    print(f"[agent] GPU claim threshold : {GPU_CLAIM_THRESHOLD}%")
    print(f"[agent] CPU pause threshold : {CPU_PAUSE_THRESHOLD}%")
    print(f"[agent] GPU pause threshold : {GPU_PAUSE_THRESHOLD}%")
    return True


# Http Session
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


# Utils
def file_sha256(file_path: Path) -> str:
    sha = hashlib.sha256()
    with file_path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


def detect_memory_mb() -> int:
    if PSUTIL_AVAILABLE:
        return int(psutil.virtual_memory().total / (1024 * 1024))
    try:
        pages = os.sysconf("SC_PHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
        return int((pages * page_size) / (1024 * 1024))
    except Exception:
        return 4096


# API Calls
def register_worker(session: requests.Session) -> None:
    vram_mb = sample_gpu_vram_free_mb() if NVML_AVAILABLE else 0
    payload = {
        "worker_id"    : WORKER_ID,
        "hostname"     : socket.gethostname(),
        "blender_image": IMAGE_NAME,
        "cpu_cores"    : os.cpu_count() or 1,
        "memory_mb"    : detect_memory_mb(),
        "gpu_vram_mb"  : vram_mb,
        "labels"       : ["linux", "docker", "blender"] + (["gpu"] if NVML_AVAILABLE else []),
    }
    response = session.post(f"{ORCHESTRATOR_URL}/workers/register", json=payload, timeout=15)
    if response.status_code >= 300:
        raise RuntimeError(f"Worker registration failed: {response.status_code} {response.text}")
    print(f"[agent] Registered on {ORCHESTRATOR_URL}")


def heartbeat(
    session: requests.Session,
    status: str,
    active_task_id: Optional[str],
    active_job_id: Optional[str],
) -> None:
    with _lock:
        cpu = _current_cpu
        gpu = _current_gpu
    payload = {
        "worker_id"     : WORKER_ID,
        "status"        : status,
        "active_task_id": active_task_id,
        "active_job_id" : active_job_id,
        "cpu_percent"   : cpu,
        "gpu_percent"   : gpu,
    }
    session.post(f"{ORCHESTRATOR_URL}/workers/heartbeat", json=payload, timeout=10)


def claim_task(session: requests.Session) -> Dict:
    payload  = {"worker_id": WORKER_ID}
    response = session.post(f"{ORCHESTRATOR_URL}/jobs/claim", json=payload, timeout=20)
    if response.status_code >= 300:
        raise RuntimeError(f"Claim failed: {response.status_code} {response.text}")
    return response.json()


def ensure_job_file(session: requests.Session, filename: str, expected_hash: str) -> Path:
   
    target = LOCAL_DIR / filename
    if target.exists() and file_sha256(target) == expected_hash:
        print(f"[agent] Using cached {filename}")
        return target

    print(f"[agent] Downloading {filename}...")


    info_resp = session.get(f"{ORCHESTRATOR_URL}/download_job/{filename}?info=1", timeout=15)
    if info_resp.status_code == 200 and "presigned_url" in info_resp.headers:
        
        presigned_url = info_resp.headers["presigned_url"]
        response = requests.get(presigned_url, timeout=300, stream=True)
    else:
        response = session.get(f"{ORCHESTRATOR_URL}/download_job/{filename}", timeout=300, stream=True)

    response.raise_for_status()
    with target.open("wb") as f:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)

    actual_hash = file_sha256(target)
    if actual_hash != expected_hash:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Checksum mismatch: expected {expected_hash[:8]}... got {actual_hash[:8]}...")
    return target


# Render logic
def simulate_render(frame_start: int, frame_end: int, prefix: str) -> List[Path]:
    output_paths: List[Path] = []
    for frame in range(frame_start, frame_end + 1):
        path = LOCAL_DIR / "frames" / f"{prefix}{frame:04d}.png"
        path.write_bytes(f"simulated frame {frame}\n".encode())
        output_paths.append(path)
    return output_paths


def run_blender_render(job_file: Path, frame_start: int, frame_end: int, prefix: str) -> List[Path]:
    global _active_container, _container_paused

    if DRY_RUN:
        return simulate_render(frame_start, frame_end, prefix)

    print(f"[agent] Rendering frames {frame_start}–{frame_end}...")
    output_pattern = f"/data/frames/{prefix}####"

    docker_cmd = [
        "docker", "run",
        "--rm",
        "--detach",                       
        "-v", f"{LOCAL_DIR}:/data",
        IMAGE_NAME,
        "blender", "-b", f"/data/{job_file.name}",
        "-o", output_pattern,
        "-s", str(frame_start),
        "-e", str(frame_end),
        "-a",
    ]

    
    result = subprocess.run(docker_cmd, check=True, capture_output=True, text=True)
    container_id = result.stdout.strip()
    print(f"[agent] Container started: {container_id[:12]}")

    with _lock:
        _active_container = container_id
        _container_paused = False

    try:
        
        subprocess.run(["docker", "wait", container_id], check=True, capture_output=True)
    finally:
        with _lock:
            _active_container = None
            _container_paused = False

    produced: List[Path] = []
    for frame in range(frame_start, frame_end + 1):
        candidate = LOCAL_DIR / "frames" / f"{prefix}{frame:04d}.png"
        if candidate.exists():
            produced.append(candidate)

    if not produced:
        
        logs = subprocess.run(
            ["docker", "logs", container_id],
            capture_output=True, text=True
        ).stderr[-1000:]
        raise RuntimeError(f"No frames produced. Last log:\n{logs}")

    return produced


# Package and upload
def package_frames(job_id: str, task_id: str, frame_files: List[Path]) -> Path:
    zip_path = LOCAL_DIR / "artifacts" / f"{job_id}_{task_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
        for frame in frame_files:
            zipf.write(frame, arcname=frame.name)
    return zip_path


def upload_artifact(
    session: requests.Session,
    job_id: str,
    task_id: str,
    replica_id: str,
    artifact_path: Path,
) -> None:
    checksum = file_sha256(artifact_path)

    # Check if orchestrator offers a presigned upload URL
    info_resp = session.get(
        f"{ORCHESTRATOR_URL}/jobs/{job_id}/tasks/{task_id}/upload_url",
        timeout=15,
    )
    if info_resp.status_code == 200:
        presigned_info = info_resp.json()
        presigned = presigned_info.get("presigned_url")
        object_key = presigned_info.get("object_key", "")
        if presigned:
            print("[agent] Using presigned upload URL (direct to cloud storage)")
            with artifact_path.open("rb") as f:
                put_resp = requests.put(presigned, data=f, timeout=300)
            put_resp.raise_for_status()
            # Notify orchestrator that upload is done
            session.post(
                f"{ORCHESTRATOR_URL}/jobs/{job_id}/tasks/{task_id}/artifact_notify",
                json={
                    "checksum_sha256": checksum,
                    "object_key": object_key,
                    "replica_id": replica_id,
                },
                timeout=30,
            )
            return

    # Falllback to uploading via orchestrator
    with artifact_path.open("rb") as f:
        response = session.post(
            f"{ORCHESTRATOR_URL}/jobs/{job_id}/tasks/{task_id}/artifact",
            data={"checksum_sha256": checksum, "replica_id": replica_id},
            files={"file": (artifact_path.name, f, "application/zip")},
            timeout=300,
        )
    if response.status_code >= 300:
        raise RuntimeError(f"Artifact upload failed: {response.status_code} {response.text}")


def report_task_failure(
    session: requests.Session,
    job_id: str,
    task_id: str,
    reason: str,
    retryable: bool = True,
) -> None:
    payload = {
        "worker_id": WORKER_ID,
        "reason"   : reason[:2048],
        "retryable": retryable,
    }
    response = session.post(
        f"{ORCHESTRATOR_URL}/jobs/{job_id}/tasks/{task_id}/fail",
        json=payload,
        timeout=30,
    )
    if response.status_code >= 300:
        raise RuntimeError(f"Failure report failed: {response.status_code} {response.text}")


# Agent Loop
def run_agent() -> None:
    global _active_container

    session = build_session()
    register_worker(session)
    start_headroom_monitor()

    last_heartbeat = 0.0
    active_job: Optional[str] = None
    active_task: Optional[str] = None

    while RUNNING:
        now = time.time()

        # Heartbeat 
        if now - last_heartbeat >= HEARTBEAT_INTERVAL_SECONDS:
            heartbeat(
                session,
                status="busy" if active_task else "ready",
                active_task_id=active_task,
                active_job_id=active_job,
            )
            last_heartbeat = now

        try:
            # Headroom gate
            if not host_is_idle_enough():
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            assignment = claim_task(session)
            job_id  = assignment.get("job_id")
            task_id = assignment.get("task_id")
            if not job_id or not task_id:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            active_job  = job_id
            active_task = task_id
            frame_start = int(assignment["frame_start"])
            frame_end   = int(assignment["frame_end"])
            file_name   = assignment["file_name"]
            file_sha    = assignment["file_sha256"]
            replica_id  = assignment.get("replica_id", "")
            prefix      = f"{job_id}_{task_id}_"

            print(f"[agent] Claimed task {task_id} | frames {frame_start}–{frame_end}")

            job_file    = ensure_job_file(session, file_name, file_sha)
            frame_files = run_blender_render(job_file, frame_start, frame_end, prefix)
            artifact    = package_frames(job_id, task_id, frame_files)
            upload_artifact(session, job_id, task_id, replica_id, artifact)

            print(f"[agent] ✓ Task {task_id} complete")
            active_job  = None
            active_task = None
            heartbeat(session, "ready", None, None)

        except Exception as exc:
            print(f"[agent] Error: {exc}")
            try:
                if active_job and active_task:
                    report_task_failure(session, active_job, active_task, str(exc), retryable=True)
                heartbeat(session, "error", active_task, active_job)
            except Exception as report_exc:
                print(f"[agent] Failed to report error: {report_exc}")

            active_job  = None
            active_task = None

            # Make sure no container is left paused or running
            with _lock:
                stale = _active_container
            if stale:
                try:
                    subprocess.run(["docker", "stop", stale], capture_output=True, timeout=10)
                except Exception:
                    pass
                with _lock:
                    _active_container = None

            time.sleep(min(POLL_INTERVAL_SECONDS * 2, 30))

    # shutdown
    try:
        heartbeat(session, "stopping", active_task, active_job)
    except Exception:
        pass
    print("[agent] Stopped.")


if __name__ == "__main__":
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    if setup_node():
        run_agent()
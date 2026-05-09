import hashlib
import os
import signal
import socket
import subprocess
import threading
import time
import uuid
import zipfile
import shlex
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
MODEL_CACHE_DIR          = Path(os.getenv("MODEL_CACHE_DIR", "./model_cache")).resolve()
DEFAULT_STABLE_DIFFUSION_IMAGE = os.getenv("DEFAULT_STABLE_DIFFUSION_IMAGE", "ghcr.io/huggingface/text-generation-inference:latest")
DEFAULT_LLM_IMAGE       = os.getenv("DEFAULT_LLM_IMAGE", "pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime")
DEFAULT_WHISPER_IMAGE   = os.getenv("DEFAULT_WHISPER_IMAGE", "ghcr.io/openai/whisper:latest")
USE_LOCAL_EXECUTORS     = os.getenv("USE_LOCAL_EXECUTORS", "false").lower() == "true"
MOCK_EXECUTORS_DIR      = Path(os.getenv("MOCK_EXECUTORS_DIR", "./workers/mock_executors")).resolve()
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
APPROVED_EXECUTOR_IMAGES = {
    image.strip()
    for image in os.getenv(
        "APPROVED_EXECUTOR_IMAGES",
        "linuxserver/blender:latest,python:3.11-slim,python:3.11-alpine,ubuntu:24.04,alpine:3.20,jrottenberg/ffmpeg:6.1,pytorch/pytorch:2.5.1-cuda12.1-cudnn9-runtime,tensorflow/tensorflow:2.16.1-gpu",
    ).split(",")
    if image.strip()
}

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
    
    if DRY_RUN:
        # In DRY_RUN mode, simulate a GPU with configurable VRAM for testing ML workloads
        simulated_vram = int(os.getenv("DRY_RUN_GPU_VRAM_MB", "16384"))
        return simulated_vram
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
    (LOCAL_DIR / "outputs").mkdir(parents=True, exist_ok=True)

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
    print(f"[agent] Approved executor images : {len(APPROVED_EXECUTOR_IMAGES)}")
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


def build_docker_run_cmd_base(executor_image: str, network_mode: str = "none", cpu_limit: Optional[int] = None, memory_mb: Optional[int] = None) -> List[str]:
    """Build the base docker run command with resource limits and network isolation."""
    cmd = [
        "docker", "run",
        "--rm",
        "--detach",
    ]
    if network_mode == "none":
        cmd.append("--network=none")
    elif network_mode == "bridge":
        cmd.append("--network=bridge")
    if cpu_limit:
        cmd.extend(["--cpus", str(cpu_limit)])
    if memory_mb:
        cmd.extend(["--memory", f"{memory_mb}m"])
    cmd.extend(["-v", f"{LOCAL_DIR}:/data"])
    cmd.append(executor_image)
    return cmd


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


def ensure_model_cached(model_name: Optional[str]) -> Optional[Path]:
    """Ensure a named model is present in the local model cache. Returns path or None."""
    if not model_name:
        return None
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    marker = MODEL_CACHE_DIR / model_name
    if marker.exists():
        print(f"[agent] Model cache hit: {model_name}")
        return marker
    # In DRY_RUN create a small placeholder; in real deployments this should
    # fetch the model weights or containerized model bundle.
    if DRY_RUN:
        marker.write_text(f"placeholder for {model_name}", encoding="utf-8")
        print(f"[agent] DRY_RUN: created model placeholder: {marker}")
        return marker
    # Non-DRY: attempt to simulate a download by creating a marker file.
    try:
        marker.write_text(f"cached model {model_name}", encoding="utf-8")
        print(f"[agent] Cached model: {model_name} -> {marker}")
        return marker
    except Exception as exc:
        print(f"[agent] Failed to cache model {model_name}: {exc}")
        return None


# Render logic
def simulate_render(frame_start: int, frame_end: int, prefix: str) -> List[Path]:
    output_paths: List[Path] = []
    for frame in range(frame_start, frame_end + 1):
        path = LOCAL_DIR / "frames" / f"{prefix}{frame:04d}.png"
        path.write_bytes(f"simulated frame {frame}\n".encode())
        output_paths.append(path)
    return output_paths


def simulate_generic_output(prefix: str, job_kind: str, job_file: Path) -> List[Path]:
    output_dir = LOCAL_DIR / "outputs" / prefix
    output_dir.mkdir(parents=True, exist_ok=True)
    result_file = output_dir / f"{job_kind}_result.txt"
    result_file.write_text(
        f"simulated {job_kind} output\ninput={job_file.name}\n",
        encoding="utf-8",
    )
    return [result_file]


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


def run_generic_executor(
    job_kind: str,
    job_file: Path,
    executor_image: str,
    executor_command: str,
    executor_args: List[str],
    frame_start: int,
    frame_end: int,
    prefix: str,
    sandbox_cpu_limit: Optional[int] = None,
    sandbox_memory_mb: Optional[int] = None,
    sandbox_network: str = "none",
) -> List[Path]:
    global _active_container, _container_paused

    if executor_image not in APPROVED_EXECUTOR_IMAGES and not (USE_LOCAL_EXECUTORS or executor_image.startswith("mock:")):
        raise RuntimeError(f"Executor image not approved: {executor_image}")

    if DRY_RUN:
        return simulate_generic_output(prefix, job_kind, job_file)

    output_dir = LOCAL_DIR / "outputs" / prefix
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[agent] Running {job_kind} job {frame_start}–{frame_end} using {executor_image}")
    # Support local mock executors for fast integration testing (no Docker required).
    if USE_LOCAL_EXECUTORS or executor_image.startswith("mock:"):
        # Map executor image to a local mock script if available.
        kind = executor_image.split(":", 1)[1] if executor_image.startswith("mock:") else "generic"
        script = MOCK_EXECUTORS_DIR / f"{kind}_mock.py"
        if script.exists():
            cmd = [sys.executable, str(script), str(job_file), str(LOCAL_DIR / "outputs" / prefix)]
            cmd.extend(executor_args or [])
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            container_id = f"mock:{kind}:{int(time.time())}"
        else:
            raise RuntimeError(f"Mock executor not found: {script}")
    else:
        docker_cmd = build_docker_run_cmd_base(
            executor_image,
            network_mode=sandbox_network,
            cpu_limit=sandbox_cpu_limit,
            memory_mb=sandbox_memory_mb,
        )
        docker_cmd.extend(shlex.split(executor_command))
        docker_cmd.extend(executor_args)
        docker_cmd.extend([
            f"/data/{job_file.name}",
            f"/data/outputs/{prefix}",
        ])

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
    for candidate in sorted(output_dir.rglob("*")):
        if candidate.is_file():
            produced.append(candidate)

    if not produced:
        logs = subprocess.run(
            ["docker", "logs", container_id],
            capture_output=True, text=True
        ).stderr[-1000:]
        raise RuntimeError(f"No output produced. Last log:\n{logs}")

    return produced


def run_ffmpeg_transcode(
    job_file: Path,
    executor_image: str,
    executor_args: List[str],
    prefix: str,
) -> List[Path]:
    """Run an ffmpeg transcode inside the approved executor image.

    Produces a single output file at /data/outputs/<prefix>/out.mp4
    """
    global _active_container, _container_paused

    if executor_image not in APPROVED_EXECUTOR_IMAGES:
        raise RuntimeError(f"Executor image not approved: {executor_image}")

    if DRY_RUN:
        return simulate_generic_output(prefix, "ffmpeg_transcode", job_file)

    output_dir = LOCAL_DIR / "outputs" / prefix
    output_dir.mkdir(parents=True, exist_ok=True)

    out_path_container = f"/data/outputs/{prefix}/out.mp4"
    print(f"[agent] Transcoding {job_file.name} → {out_path_container} using {executor_image}")

    docker_cmd = [
        "docker", "run",
        "--rm",
        "--detach",
        "-v", f"{LOCAL_DIR}:/data",
        executor_image,
        "ffmpeg",
        "-y",
        "-i",
        f"/data/{job_file.name}",
    ]
    docker_cmd.extend(executor_args or [])
    docker_cmd.append(out_path_container)

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
    for candidate in sorted(output_dir.rglob("*")):
        if candidate.is_file():
            produced.append(candidate)

    if not produced:
        logs = subprocess.run([
            "docker", "logs", container_id
        ], capture_output=True, text=True).stderr[-1000:]
        raise RuntimeError(f"No output produced. Last log:\n{logs}")

    return produced


def run_stable_diffusion_executor(
    job_file: Path,
    model_name: Optional[str],
    executor_image: str,
    executor_args: List[str],
    prefix: str,
) -> List[Path]:
    """Scaffold to run a Stable Diffusion-style job inside a container.

    This is a best-effort helper: it validates model cache and launches the
    container with the dataset and model path mounted under /data/models.
    """
    global _active_container, _container_paused

    if executor_image not in APPROVED_EXECUTOR_IMAGES:
        raise RuntimeError(f"Executor image not approved: {executor_image}")

    # Ensure model is available locally
    model_path = ensure_model_cached(model_name)
    if model_name and not model_path:
        raise RuntimeError(f"Required model not available: {model_name}")

    if DRY_RUN:
        return simulate_generic_output(prefix, "stable_diffusion", job_file)

    output_dir = LOCAL_DIR / "outputs" / prefix
    output_dir.mkdir(parents=True, exist_ok=True)

    # Common invocation: container expects model at /data/models/<model_name>
    container_model_path = f"/data/models/{model_name or 'model'}"
    out_path = f"/data/outputs/{prefix}/sd_out.png"

    docker_cmd = [
        "docker", "run",
        "--rm",
        "--detach",
        "-v", f"{LOCAL_DIR}:/data",
        executor_image,
    ]
    # basic command: let executor_args drive the real invocation
    docker_cmd.extend(executor_args or [])
    docker_cmd.extend([str(job_file), container_model_path, out_path])

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
    for candidate in sorted(output_dir.rglob("*")):
        if candidate.is_file():
            produced.append(candidate)

    if not produced:
        logs = subprocess.run([
            "docker", "logs", container_id
        ], capture_output=True, text=True).stderr[-1000:]
        raise RuntimeError(f"No output produced. Last log:\n{logs}")

    return produced


def run_llm_executor(
    job_file: Path,
    model_name: Optional[str],
    executor_image: str,
    executor_args: List[str],
    prefix: str,
) -> List[Path]:
    """Scaffold to run an LLM inference job inside a container.

    Writes outputs to /data/outputs/<prefix>/llm_out.txt
    """
    if executor_image not in APPROVED_EXECUTOR_IMAGES:
        raise RuntimeError(f"Executor image not approved: {executor_image}")

    model_path = ensure_model_cached(model_name)
    if model_name and not model_path:
        raise RuntimeError(f"Required model not available: {model_name}")

    if DRY_RUN:
        return simulate_generic_output(prefix, "llm_inference", job_file)

    output_dir = LOCAL_DIR / "outputs" / prefix
    output_dir.mkdir(parents=True, exist_ok=True)

    out_file = output_dir / "llm_out.txt"

    docker_cmd = [
        "docker", "run",
        "--rm",
        "--detach",
        "-v", f"{LOCAL_DIR}:/data",
        executor_image,
    ]
    docker_cmd.extend(executor_args or [])
    docker_cmd.extend([str(job_file), f"/data/outputs/{prefix}"])

    result = subprocess.run(docker_cmd, check=True, capture_output=True, text=True)
    container_id = result.stdout.strip()

    with _lock:
        _active_container = container_id
        _container_paused = False

    try:
        subprocess.run(["docker", "wait", container_id], check=True, capture_output=True)
    finally:
        with _lock:
            _active_container = None
            _container_paused = False

    produced = []
    for candidate in sorted(output_dir.rglob("*")):
        if candidate.is_file():
            produced.append(candidate)
    if not produced:
        logs = subprocess.run(["docker", "logs", container_id], capture_output=True, text=True).stderr[-1000:]
        raise RuntimeError(f"No output produced. Last log:\n{logs}")
    return produced


def run_whisper_executor(
    job_file: Path,
    executor_image: str,
    executor_args: List[str],
    prefix: str,
) -> List[Path]:
    if executor_image not in APPROVED_EXECUTOR_IMAGES:
        raise RuntimeError(f"Executor image not approved: {executor_image}")
    if DRY_RUN:
        return simulate_generic_output(prefix, "whisper_transcribe", job_file)
    output_dir = LOCAL_DIR / "outputs" / prefix
    output_dir.mkdir(parents=True, exist_ok=True)
    docker_cmd = [
        "docker", "run",
        "--rm",
        "--detach",
        "-v", f"{LOCAL_DIR}:/data",
        executor_image,
    ]
    docker_cmd.extend(executor_args or [])
    docker_cmd.extend([str(job_file), f"/data/outputs/{prefix}"])
    result = subprocess.run(docker_cmd, check=True, capture_output=True, text=True)
    container_id = result.stdout.strip()
    with _lock:
        _active_container = container_id
        _container_paused = False
    try:
        subprocess.run(["docker", "wait", container_id], check=True, capture_output=True)
    finally:
        with _lock:
            _active_container = None
            _container_paused = False
    produced = []
    for candidate in sorted(output_dir.rglob("*")):
        if candidate.is_file():
            produced.append(candidate)
    if not produced:
        logs = subprocess.run(["docker", "logs", container_id], capture_output=True, text=True).stderr[-1000:]
        raise RuntimeError(f"No output produced. Last log:\n{logs}")
    return produced


# Package and upload
def package_artifacts(job_id: str, task_id: str, artifact_files: List[Path]) -> Path:
    zip_path = LOCAL_DIR / "artifacts" / f"{job_id}_{task_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zipf:
        for artifact in artifact_files:
            if artifact.is_file():
                zipf.write(artifact, arcname=artifact.name)
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
            job_kind    = assignment.get("job_kind", "blender_render")
            executor_image = assignment.get("executor_image") or IMAGE_NAME
            executor_command = assignment.get("executor_command") or "blender"
            executor_args = list(assignment.get("executor_args") or [])
            replica_id  = assignment.get("replica_id", "")
            prefix      = f"{job_id}_{task_id}_"
            sandbox_cpu_limit = assignment.get("sandbox_cpu_limit")
            sandbox_memory_mb = assignment.get("sandbox_memory_mb")
            sandbox_network = assignment.get("sandbox_network", "none")
            require_attestation = assignment.get("require_attestation", False)

            print(f"[agent] Claimed task {task_id} | frames {frame_start}–{frame_end}")

            job_file    = ensure_job_file(session, file_name, file_sha)
            if job_kind == "blender_render":
                output_files = run_blender_render(job_file, frame_start, frame_end, prefix)
            elif job_kind == "ffmpeg_transcode":
                output_files = run_ffmpeg_transcode(
                    job_file,
                    executor_image,
                    executor_args,
                    prefix,
                )
            elif job_kind == "stable_diffusion":
                output_files = run_stable_diffusion_executor(
                    job_file,
                    assignment.get("model_name"),
                    executor_image,
                    executor_args,
                    prefix,
                )
            elif job_kind == "llm_inference":
                output_files = run_llm_executor(
                    job_file,
                    assignment.get("model_name"),
                    executor_image,
                    executor_args,
                    prefix,
                )
            elif job_kind == "whisper_transcribe":
                output_files = run_whisper_executor(
                    job_file,
                    executor_image,
                    executor_args,
                    prefix,
                )
            else:
                output_files = run_generic_executor(
                    job_kind,
                    job_file,
                    executor_image,
                    executor_command,
                    executor_args,
                    frame_start,
                    frame_end,
                    prefix,
                    sandbox_cpu_limit=sandbox_cpu_limit,
                    sandbox_memory_mb=sandbox_memory_mb,
                    sandbox_network=sandbox_network,
                )

            artifact    = package_artifacts(job_id, task_id, output_files)
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
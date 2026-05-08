import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.live import Live
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich import print as rprint

load_dotenv()

app     = typer.Typer(help="Isogrid Distributed Render CLI", add_completion=False)
console = Console()

ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY          = os.getenv("ORCHESTRATOR_API_KEY", "")


def headers() -> dict:
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["X-API-Key"] = API_KEY
    return h


def api_get(path: str) -> dict:
    r = requests.get(f"{ORCHESTRATOR_URL}{path}", headers=headers(), timeout=30)
    r.raise_for_status()
    return r.json()


def api_post(path: str, json_body: Optional[dict] = None, files=None, data=None) -> dict:
    h = {"X-API-Key": API_KEY} if API_KEY else {}
    r = requests.post(
        f"{ORCHESTRATOR_URL}{path}",
        json=json_body,
        headers=h,
        files=files,
        data=data,
        timeout=120,
    )
    r.raise_for_status()
    return r.json()


def status_color(status: str) -> str:
    colors = {
        "pending"  : "yellow",
        "running"  : "blue",
        "stitching": "cyan",
        "complete" : "green",
        "failed"   : "red",
        "conflict" : "magenta",
        "queued"   : "yellow",
        "stale"    : "dim",
        "ready"    : "green",
        "busy"     : "blue",
        "error"    : "red",
    }
    color = colors.get(status, "white")
    return f"[{color}]{status}[/{color}]"


def job_progress(job: dict) -> float:
    """Return 0.0–1.0 completion fraction based on tasks."""
    tasks = job.get("tasks", [])
    if not tasks:
        return 0.0
    done = sum(1 for t in tasks if t["status"] == "complete")
    return done / len(tasks)


# CMDS

@app.command()
def upload(
    blend_file: Path = typer.Argument(..., help="Path to the .blend file to upload"),
):
    """Upload a .blend file to the orchestrator."""
    if not blend_file.exists():
        rprint(f"[red]File not found: {blend_file}[/red]")
        raise typer.Exit(1)

    console.print(f"Uploading [bold]{blend_file.name}[/bold] ({blend_file.stat().st_size / 1024:.1f} KB)...")
    with blend_file.open("rb") as f:
        result = api_post("/upload", files={"file": (blend_file.name, f, "application/octet-stream")})

    rprint(f"[green] Uploaded:[/green] {result['file_name']}  sha256={result['sha256'][:12]}...")
    rprint(f"[cyan] Upload token:[/cyan] {result['upload_token']}")
    rprint(f"[dim] Expires at:[/dim] {result['expires_at']}")


@app.command()
def submit(
    blend_file: Path = typer.Argument(..., help=".blend file (must be uploaded first, or will auto-upload)"),
    start:      int  = typer.Option(1,   "--start", "-s",        help="First frame"),
    end:        int  = typer.Option(...,  "--end",   "-e",        help="Last frame"),
    chunk:      int  = typer.Option(5,   "--chunk",  "-c",        help="Frames per task chunk"),
    priority:   int  = typer.Option(100, "--priority", "-p",      help="Priority (lower = runs first)"),
    replicas:   int  = typer.Option(1,   "--replicas", "-r",      help="Replication factor (1–3) for anti-cheat"),
    fps:        int  = typer.Option(24,  "--fps",                 help="Output video FPS"),
    no_stitch:  bool = typer.Option(False, "--no-stitch",         help="Skip FFmpeg assembly step"),
    auto_upload: bool = typer.Option(True, "--auto-upload/--no-auto-upload", help="Auto-upload .blend if not on server"),
    upload_token: str = typer.Option("", "--token", help="Upload token returned by /upload (required with --no-auto-upload)"),
):
    """Submit a render job. Auto-uploads the .blend file if needed."""
    token = upload_token.strip()

    if auto_upload and blend_file.exists():
        console.print(f"Uploading [bold]{blend_file.name}[/bold]...")
        with blend_file.open("rb") as f:
            upload_result = api_post("/upload", files={"file": (blend_file.name, f, "application/octet-stream")})
        token = upload_result["upload_token"]
        console.print("[dim]Upload complete[/dim]")
    elif not blend_file.exists():
        if not token:
            rprint(f"[red]{blend_file} not found locally. Provide --token from a previous upload.[/red]")
            raise typer.Exit(1)
    elif not token:
        rprint("[red]Missing upload token. Use --token or enable --auto-upload.[/red]")
        raise typer.Exit(1)

    payload = {
        "file_name"         : blend_file.name,
        "upload_token"      : token,
        "frame_start"       : start,
        "frame_end"         : end,
        "chunk_size"        : chunk,
        "priority"          : priority,
        "replication_factor": replicas,
        "output_fps"        : fps,
        "stitch_output"     : not no_stitch,
    }

    console.print(f"Submitting job: frames [bold]{start}–{end}[/bold] in chunks of {chunk}...")
    job = api_post("/jobs", json_body=payload)

    task_count = len(job.get("tasks", []))
    rprint(f"\n[green]✓ Job created[/green]")
    rprint(f"  Job ID   : [bold]{job['job_id']}[/bold]")
    rprint(f"  Tasks    : {task_count} chunks × {replicas} replica(s)")
    rprint(f"  Frames   : {start}–{end} ({end - start + 1} total)")
    rprint(f"  Assemble : {'yes' if not no_stitch else 'no'}")
    rprint(f"\n[dim]Track progress:[/dim]  python clients/cli/isogrid_cli.py watch {job['job_id']}")


@app.command()
def uploads():
    """List currently active uploaded blend assets and token expiry."""
    items = api_get("/uploads")
    if not items:
        rprint("[yellow]No active uploads found.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold dim", title="Active Uploads")
    table.add_column("File", width=24)
    table.add_column("Size", width=10)
    table.add_column("SHA256", width=16)
    table.add_column("Token", width=18)
    table.add_column("Expires", width=28)

    for item in items:
        size_kb = item.get("size_bytes", 0) / 1024
        table.add_row(
            item.get("file_name", ""),
            f"{size_kb:.1f} KB",
            item.get("sha256", "")[:12] + "...",
            item.get("upload_token", "")[:12] + "...",
            item.get("expires_at", ""),
        )

    console.print(table)


@app.command()
def status(
    job_id: str = typer.Argument(..., help="Job ID to check"),
):
    """Show the status of a specific job."""
    job    = api_get(f"/jobs/{job_id}")
    tasks  = job.get("tasks", [])
    done   = sum(1 for t in tasks if t["status"] == "complete")
    total  = len(tasks)
    pct    = (done / total * 100) if total else 0

    rprint(f"\n[bold]Job {job['job_id'][:8]}...[/bold]")
    rprint(f"  Status   : {status_color(job['status'])}")
    rprint(f"  File     : {job['file_name']}")
    rprint(f"  Frames   : {job['frame_start']}–{job['frame_end']}")
    rprint(f"  Progress : {done}/{total} tasks ({pct:.0f}%)")
    if job.get("final_video_path"):
        rprint(f"  Output   : [green]{job['final_video_path']}[/green]")

    rprint(f"\n[bold]Tasks:[/bold]")
    table = Table(show_header=True, header_style="bold dim")
    table.add_column("Task",         width=10)
    table.add_column("Frames",       width=12)
    table.add_column("Status",       width=12)
    table.add_column("Verification", width=14)
    table.add_column("Replicas",     width=10)

    for t in tasks:
        reps    = t.get("replicas", [])
        rep_str = f"{sum(1 for r in reps if r['status'] == 'complete')}/{len(reps)}"
        table.add_row(
            t["task_id"][:8] + "...",
            f"{t['frame_start']}–{t['frame_end']}",
            status_color(t["status"]),
            status_color(t.get("verification", "skipped")),
            rep_str,
        )
    console.print(table)


@app.command()
def jobs():
    """List all jobs."""
    job_list = api_get("/jobs")

    table = Table(show_header=True, header_style="bold dim", title="Jobs")
    table.add_column("Job ID",    width=12)
    table.add_column("File",      width=22)
    table.add_column("Frames",    width=12)
    table.add_column("Status",    width=12)
    table.add_column("Progress",  width=14)
    table.add_column("Priority",  width=8)

    for j in job_list:
        pct   = job_progress(j) * 100
        bar   = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        table.add_row(
            j["job_id"][:8] + "...",
            j["file_name"][:20],
            f"{j['frame_start']}–{j['frame_end']}",
            status_color(j["status"]),
            f"{bar} {pct:.0f}%",
            str(j["priority"]),
        )

    console.print(table)


@app.command()
def workers():
    """List all registered worker nodes."""
    worker_list = api_get("/workers")

    table = Table(show_header=True, header_style="bold dim", title="Workers")
    table.add_column("Worker ID",  width=12)
    table.add_column("Hostname",   width=18)
    table.add_column("Status",     width=10)
    table.add_column("CPU%",       width=7)
    table.add_column("GPU%",       width=7)
    table.add_column("VRAM (MB)",  width=10)
    table.add_column("Cores",      width=6)
    table.add_column("RAM (MB)",   width=9)
    table.add_column("Active Task",width=12)

    for w in worker_list:
        table.add_row(
            w["worker_id"][:8] + "...",
            w["hostname"],
            status_color(w["status"]),
            f"{w.get('cpu_percent', 0):.0f}%",
            f"{w.get('gpu_percent', 0):.0f}%",
            str(w.get("gpu_vram_mb", "N/A")),
            str(w["cpu_cores"]),
            str(w["memory_mb"]),
            (w.get("active_task_id") or "")[:10] + ("..." if w.get("active_task_id") else "—"),
        )

    console.print(table)


@app.command()
def watch(
    job_id:   str = typer.Argument(..., help="Job ID to watch"),
    interval: int = typer.Option(3, "--interval", "-i", help="Refresh interval in seconds"),
):
    """Live-updating progress view for a job."""
    console.print(f"Watching job [bold]{job_id}[/bold] (Ctrl+C to stop)\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(bar_width=40),
        TextColumn("{task.percentage:.0f}%"),
        TimeElapsedColumn(),
        console=console,
        refresh_per_second=1,
    ) as progress:
        overall = progress.add_task("Overall", total=100)

        try:
            while True:
                job    = api_get(f"/jobs/{job_id}")
                tasks  = job.get("tasks", [])
                done   = sum(1 for t in tasks if t["status"] == "complete")
                total  = len(tasks) or 1
                pct    = done / total * 100
                sts    = job["status"]

                progress.update(overall, completed=pct, description=f"[bold]{sts}[/bold]  {done}/{total} chunks")

                if sts in ("complete", "failed"):
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            pass

    # Final status
    job = api_get(f"/jobs/{job_id}")
    if job["status"] == "complete":
        rprint(f"\n[green]✓ Job complete![/green]")
        if job.get("final_video_path"):
            rprint(f"  Video: {job['final_video_path']}")
        rprint(f"  Download: python clients/cli/isogrid_cli.py download {job_id}")
    elif job["status"] == "stitching":
        rprint(f"\n[cyan]Assembling video...[/cyan]  Check again in a moment.")
    else:
        rprint(f"\n[red]Job ended with status: {job['status']}[/red]")


@app.command()
def download(
    job_id: str  = typer.Argument(..., help="Job ID to download"),
    output: Path = typer.Option(None, "--output", "-o", help="Output file path (default: <job_id>.mp4)"),
):
    """Download the final assembled video for a completed job."""
    out_path = output or Path(f"{job_id[:8]}.mp4")

    console.print(f"Downloading [bold]{job_id}[/bold] → {out_path}...")

    h = {"X-API-Key": API_KEY} if API_KEY else {}
    r = requests.get(f"{ORCHESTRATOR_URL}/jobs/{job_id}/download", headers=h, stream=True, timeout=300)

    if r.status_code == 409:
        rprint(f"[yellow]Job not ready yet. Status: {r.json().get('detail')}[/yellow]")
        raise typer.Exit(1)
    r.raise_for_status()

    total   = int(r.headers.get("content-length", 0))
    written = 0
    with out_path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            written += len(chunk)
            if total:
                pct = written / total * 100
                console.print(f"\r  {pct:.0f}%  ({written // (1024*1024)} MB)", end="")

    rprint(f"\n[green]✓ Saved to {out_path}[/green]  ({out_path.stat().st_size // (1024*1024)} MB)")


@app.command()
def metrics():
    """Show orchestrator metrics."""
    m = api_get("/metrics")
    rprint(f"\n[bold]Orchestrator Metrics[/bold]")
    for key, val in m.items():
        label = key.replace("_", " ").title()
        rprint(f"  {label:<25} {val}")


if __name__ == "__main__":
    app()

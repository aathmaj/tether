from __future__ import annotations

bl_info = {
    "name"       : "Tether Render Farm",
    "author"     : "Tether",
    "version"    : (1, 0, 0),
    "blender"    : (3, 0, 0),
    "location"   : "Properties > Render > Tether Render Farm",
    "description": "Submit render jobs to the Tether distributed compute network",
    "category"   : "Render",
}

import bpy
import tempfile
import threading
from pathlib import Path


try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


# Prefs
class TetherPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    orchestrator_url: bpy.props.StringProperty(
        name    = "Orchestrator URL",
        default = "http://127.0.0.1:8000",
        description = "Base URL of the Tether orchestrator (local or ngrok URL)",
    )
    api_key: bpy.props.StringProperty(
        name    = "API Key",
        default = "",
        subtype = "PASSWORD",
        description = "ORCHESTRATOR_API_KEY (leave blank if not set)",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "orchestrator_url")
        layout.prop(self, "api_key")


# Scene Properties
class TetherJobProperties(bpy.types.PropertyGroup):
    chunk_size: bpy.props.IntProperty(
        name        = "Chunk Size",
        description = "Frames per task chunk",
        default     = 5,
        min         = 1,
        max         = 100,
    )
    priority: bpy.props.IntProperty(
        name        = "Priority",
        description = "Lower value = runs first in queue",
        default     = 100,
        min         = 0,
        max         = 1000,
    )
    replication_factor: bpy.props.IntProperty(
        name        = "Replication",
        description = "Render each chunk on N nodes and compare hashes (anti-cheat)",
        default     = 1,
        min         = 1,
        max         = 3,
    )
    pack_textures: bpy.props.BoolProperty(
        name        = "Pack Textures",
        description = "Pack all external images into the .blend before uploading",
        default     = True,
    )
    stitch_output: bpy.props.BoolProperty(
        name        = "Assemble Video",
        description = "Auto-assemble rendered frames into .mp4 when complete",
        default     = True,
    )
    # Status tracking
    job_id: bpy.props.StringProperty(default="")
    job_status: bpy.props.StringProperty(default="")
    job_progress: bpy.props.FloatProperty(default=0.0, min=0.0, max=1.0, subtype="FACTOR")
    status_message: bpy.props.StringProperty(default="")
    is_polling: bpy.props.BoolProperty(default=False)


# Util Funcs
def get_prefs() -> 'TetherPreferences':
    return bpy.context.preferences.addons[__name__].preferences


def build_headers() -> dict:
    prefs = get_prefs()
    h = {}
    if prefs.api_key:
        h["X-API-Key"] = prefs.api_key
    return h


def get_orchestrator_url() -> str:
    return get_prefs().orchestrator_url.rstrip("/")


def is_connected() -> bool:
    """Quick health check against the orchestrator."""
    if not REQUESTS_AVAILABLE:
        return False
    try:
        r = requests.get(f"{get_orchestrator_url()}/health", timeout=5, headers=build_headers())
        return r.status_code == 200
    except Exception:
        return False


# Oeprators
class TETHER_OT_TestConnection(bpy.types.Operator):
    bl_idname  = "tether.test_connection"
    bl_label   = "Test Connection"
    bl_description = "Ping the Tether orchestrator"

    def execute(self, context):
        if not REQUESTS_AVAILABLE:
            self.report({"ERROR"}, "requests library not installed. Run: pip install requests")
            return {"CANCELLED"}
        if is_connected():
            self.report({"INFO"}, f"Connected to {get_orchestrator_url()}")
        else:
            self.report({"ERROR"}, f"Cannot reach {get_orchestrator_url()}")
        return {"FINISHED"}


class TETHER_OT_SubmitJob(bpy.types.Operator):
    bl_idname      = "tether.submit_job"
    bl_label       = "Submit to Tether"
    bl_description = "Pack textures, upload scene, and create a render job"

    def execute(self, context):
        if not REQUESTS_AVAILABLE:
            self.report({"ERROR"}, "requests library not installed")
            return {"CANCELLED"}

        scene = context.scene
        props = scene.tether_job

        if not is_connected():
            self.report({"ERROR"}, f"Cannot reach orchestrator at {get_orchestrator_url()}")
            return {"CANCELLED"}

        # Run submission in a background thread to avoid freezing Blender UI
        thread = threading.Thread(
            target=self._submit_thread,
            args=(context,),
            daemon=True,
        )
        thread.start()
        props.status_message = "Preparing scene..."
        props.job_status     = "uploading"
        return {"FINISHED"}

    def _submit_thread(self, context):
        scene = context.scene
        props = scene.tether_job
        prefs = get_prefs()

        try:
            # 1. Save a copy of the current .blend with packed textures
            props.status_message = "Packing textures..."
            with tempfile.TemporaryDirectory() as tmpdir:
                packed_path = Path(tmpdir) / "tether_scene.blend"

                # Save current file state first
                if bpy.data.filepath:
                    bpy.ops.wm.save_mainfile(filepath=bpy.data.filepath)

                # Pack all external data
                if props.pack_textures:
                    bpy.ops.file.pack_all()

                # Save packed version
                bpy.ops.wm.save_as_mainfile(
                    filepath = str(packed_path),
                    copy     = True,       # save as copy, don't change current filepath
                )

                props.status_message = "Uploading scene..."

                # 2. Upload the packed .blend
                with packed_path.open("rb") as f:
                    r = requests.post(
                        f"{get_orchestrator_url()}/upload",
                        files   = {"file": ("tether_scene.blend", f, "application/octet-stream")},
                        headers = build_headers(),
                        timeout = 300,
                    )
                r.raise_for_status()
                upload_result = r.json()
                file_name     = upload_result["file_name"]

                props.status_message = "Creating job..."

                # 3. Create the job
                payload = {
                    "file_name"         : file_name,
                    "frame_start"       : scene.frame_start,
                    "frame_end"         : scene.frame_end,
                    "chunk_size"        : props.chunk_size,
                    "priority"          : props.priority,
                    "replication_factor": props.replication_factor,
                    "output_fps"        : round(scene.render.fps / scene.render.fps_base),
                    "stitch_output"     : props.stitch_output,
                }
                r = requests.post(
                    f"{get_orchestrator_url()}/jobs",
                    json    = payload,
                    headers = build_headers(),
                    timeout = 30,
                )
                r.raise_for_status()
                job = r.json()

                # 4. Store job ID and start polling
                def update_ui():
                    props.job_id       = job["job_id"]
                    props.job_status   = job["status"]
                    props.job_progress = 0.0
                    props.status_message = f"Job {job['job_id'][:8]} created — {len(job['tasks'])} tasks"
                    props.is_polling   = True
                    return None  # don't repeat

                bpy.app.timers.register(update_ui, first_interval=0.0)

                # 5. Start status polling
                self._poll_job_status(props, job["job_id"])

        except Exception as exc:
            def show_error():
                props.status_message = f"Error: {exc}"
                props.job_status     = "failed"
                return None
            bpy.app.timers.register(show_error, first_interval=0.0)

    def _poll_job_status(self, props, job_id: str):
        """Poll the orchestrator every 5 seconds and update Blender UI via timer."""
        url = f"{get_orchestrator_url()}/jobs/{job_id}"

        def poll():
            if not props.is_polling:
                return None  # stop

            try:
                r     = requests.get(url, headers=build_headers(), timeout=10)
                r.raise_for_status()
                job   = r.json()
                tasks = job.get("tasks", [])
                done  = sum(1 for t in tasks if t["status"] == "complete")
                total = len(tasks) or 1

                def update():
                    props.job_status   = job["status"]
                    props.job_progress = done / total
                    props.status_message = (
                        f"{job['status'].upper()} — {done}/{total} chunks"
                        + (" — Assembling video..." if job["status"] == "stitching" else "")
                    )
                    if job["status"] in ("complete", "failed"):
                        props.is_polling = False
                    return None

                bpy.app.timers.register(update, first_interval=0.0)

                if job["status"] in ("complete", "failed"):
                    return None  # stop polling
                return 5.0  # repeat after 5 seconds

            except Exception as exc:
                def show_err():
                    props.status_message = f"Poll error: {exc}"
                    return None
                bpy.app.timers.register(show_err, first_interval=0.0)
                return 10.0  # retry after 10 seconds

        bpy.app.timers.register(poll, first_interval=5.0)


class TETHER_OT_CancelPolling(bpy.types.Operator):
    bl_idname  = "tether.cancel_polling"
    bl_label   = "Stop Watching"
    bl_description = "Stop monitoring job progress"

    def execute(self, context):
        context.scene.tether_job.is_polling = False
        context.scene.tether_job.status_message = "Monitoring stopped"
        return {"FINISHED"}


class TETHER_OT_OpenDashboard(bpy.types.Operator):
    bl_idname  = "tether.open_dashboard"
    bl_label   = "Open Dashboard"
    bl_description = "Open the Tether web dashboard in a browser"

    def execute(self, context):
        import webbrowser
   
        addon_dir  = Path(__file__).parent
        dash_path  = addon_dir / "dashboard.html"
        if dash_path.exists():
            webbrowser.open(f"file://{dash_path}")
        else:
            webbrowser.open(f"{get_orchestrator_url()}/docs")
        return {"FINISHED"}


# Panel
class TETHER_PT_RenderPanel(bpy.types.Panel):
    bl_label       = "Tether Render Farm"
    bl_idname      = "TETHER_PT_render_panel"
    bl_space_type  = "PROPERTIES"
    bl_region_type = "WINDOW"
    bl_context     = "render"
    bl_options     = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        scene  = context.scene
        props  = scene.tether_job
        prefs  = get_prefs()

        if not REQUESTS_AVAILABLE:
            layout.label(text="requests not installed", icon="ERROR")
            layout.label(text="Run: pip install requests")
            return

        # Connection
        box = layout.box()
        row = box.row()
        row.label(text="Orchestrator:", icon="WORLD")
        row.label(text=prefs.orchestrator_url)
        box.operator("tether.test_connection", icon="LINKED")

        layout.separator()

        # Frame Range
        box = layout.box()
        box.label(text="Frame Range", icon="TIME")
        row = box.row()
        row.prop(scene, "frame_start", text="Start")
        row.prop(scene, "frame_end",   text="End")

        # Job Settings
        box = layout.box()
        box.label(text="Job Settings", icon="SETTINGS")
        box.prop(props, "chunk_size")
        box.prop(props, "priority")
        box.prop(props, "replication_factor")
        row = box.row()
        row.prop(props, "pack_textures")
        row.prop(props, "stitch_output")

        layout.separator()

        # Submit Button with Job Summary
        total_frames = scene.frame_end - scene.frame_start + 1
        chunks       = -(-total_frames // props.chunk_size)   # ceiling division
        layout.label(text=f"{total_frames} frames → {chunks} chunks × {props.replication_factor} replica(s)")

        row = layout.row()
        row.scale_y = 1.8
        row.operator("tether.submit_job", icon="RENDER_ANIMATION")

        # Status
        if props.status_message:
            layout.separator()
            box = layout.box()
            box.label(text=props.status_message, icon="INFO")

            if props.job_id:
                box.label(text=f"Job ID: {props.job_id[:16]}...")
                row = box.row()
                row.prop(props, "job_progress", text="Progress", slider=True)
                if props.is_polling:
                    box.operator("tether.cancel_polling", icon="X")

        layout.separator()
        layout.operator("tether.open_dashboard", icon="URL")


# Registration
classes = (
    TetherPreferences,
    TetherJobProperties,
    TETHER_OT_TestConnection,
    TETHER_OT_SubmitJob,
    TETHER_OT_CancelPolling,
    TETHER_OT_OpenDashboard,
    TETHER_PT_RenderPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.tether_job = bpy.props.PointerProperty(type=TetherJobProperties)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    del bpy.types.Scene.tether_job


if __name__ == "__main__":
    register()
bl_info = {
    "name": "Time Spent Tracker",
    "author": "JJemu",
    "version": (0, 3, 0),
    "blender": (5, 0, 0),
    "location": "Status Bar",
    "description": "Tracks session, total, per-project (folder) and per-file time usage. Counts only when focused and not idle.",
    "category": "System",
}

import bpy
from bpy.app.handlers import persistent
import time
import os
import json
import csv

START_TIME = None
LAST_TICK_TS = 0.0
TOTAL_SECONDS = 0.0
PROJECT_SECONDS = {}
FILE_SECONDS = {}
LAST_SAVE_TS = 0.0
SAVE_INTERVAL = 60.0
TIMER_PERIOD = 1.0
CURRENT_FILE = "(Unsaved)"
CURRENT_PROJECT = "(Unsaved)"
INITIALIZED = False
WINDOW_FOCUSED = True
_MONITOR_STARTED = False
LAST_INPUT_TS = 0.0
IDLE_TIMEOUT_DEFAULT = 30.0

def _safe_getattr(obj, name, default=None):
    try:
        return getattr(obj, name)
    except Exception:
        return default

def _safe_filepath() -> str:
    try:
        p = _safe_getattr(bpy.data, "filepath", "") or ""
        return p
    except Exception:
        return ""

def _iter_areas():
    wm = _safe_getattr(bpy.context, "window_manager")
    if not wm:
        return
    windows = _safe_getattr(wm, "windows", [])
    for window in windows or []:
        screen = _safe_getattr(window, "screen")
        if not screen:
            continue
        for area in _safe_getattr(screen, "areas", []) or []:
            yield area

def _format_hms(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def _get_save_path() -> str:
    conf_dir = bpy.utils.user_resource('CONFIG', path="time_spent_tracker", create=True)
    return os.path.join(conf_dir, "time_spent.json")

def _get_csv_path() -> str:
    conf_dir = bpy.utils.user_resource('CONFIG', path="time_spent_tracker", create=True)
    return os.path.join(conf_dir, "time_spent_export.csv")

def _normalize_path(p: str) -> str:
    return os.path.normpath(os.path.abspath(p)) if p and p != "" else ""

def _current_file() -> str:
    p = _safe_filepath()
    if not p:
        return "(Unsaved)"
    return _normalize_path(p)

def _current_project() -> str:
    p = _safe_filepath()
    if not p:
        return "(Unsaved)"
    folder = _normalize_path(os.path.dirname(p))
    return folder or "(Unsaved)"

def _ensure_keys(file_key: str, project_key: str):
    if file_key not in FILE_SECONDS:
        FILE_SECONDS[file_key] = 0.0
    if project_key not in PROJECT_SECONDS:
        PROJECT_SECONDS[project_key] = 0.0

def _load_data():
    global TOTAL_SECONDS, PROJECT_SECONDS, FILE_SECONDS
    path = _get_save_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            TOTAL_SECONDS = float(data.get("total_seconds", 0.0))
            PROJECT_SECONDS = {k: float(v) for k, v in data.get("projects", {}).items()}
            FILE_SECONDS = {k: float(v) for k, v in data.get("files", {}).items()}
        except Exception:
            TOTAL_SECONDS = 0.0
            PROJECT_SECONDS = {}
            FILE_SECONDS = {}
    else:
        TOTAL_SECONDS = 0.0
        PROJECT_SECONDS = {}
        FILE_SECONDS = {}

def _save_data():
    path = _get_save_path()
    data = {
        "total_seconds": float(TOTAL_SECONDS),
        "projects": {k: float(v) for k, v in PROJECT_SECONDS.items()},
        "files": {k: float(v) for k, v in FILE_SECONDS.items()},
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass

def _save_checkpoint_if_due(now: float):
    global LAST_SAVE_TS
    if (now - LAST_SAVE_TS) >= SAVE_INTERVAL:
        _save_data()
        LAST_SAVE_TS = now

def _get_idle_timeout_seconds() -> float:
    try:
        addon = bpy.context.preferences.addons.get(__name__)
        if addon:
            secs = float(getattr(addon.preferences, "idle_timeout_seconds", IDLE_TIMEOUT_DEFAULT))
            return max(0.0, secs)
    except Exception:
        pass
    return IDLE_TIMEOUT_DEFAULT

def _is_idle(now: float) -> bool:
    timeout = _get_idle_timeout_seconds()
    if timeout <= 0.0:
        return False
    return (now - LAST_INPUT_TS) >= timeout

def _tick_accumulate(now: float):
    global LAST_TICK_TS, TOTAL_SECONDS
    if LAST_TICK_TS <= 0.0:
        LAST_TICK_TS = now
        return
    dt = max(0.0, now - LAST_TICK_TS)
    LAST_TICK_TS = now
    if not WINDOW_FOCUSED or _is_idle(now) or dt == 0.0:
        return
    file_key = CURRENT_FILE
    project_key = CURRENT_PROJECT
    _ensure_keys(file_key, project_key)
    TOTAL_SECONDS += dt
    PROJECT_SECONDS[project_key] += dt
    FILE_SECONDS[file_key] += dt

def _current_session_seconds() -> float:
    if START_TIME is None:
        return 0.0
    return max(0.0, time.time() - START_TIME)

def _top_n(mapping: dict, n=5):
    return sorted(mapping.items(), key=lambda kv: kv[1], reverse=True)[:n]

def draw_statusbar(self, context):
    session = _format_hms(_current_session_seconds())
    total = _format_hms(TOTAL_SECONDS)
    now = time.time()
    focused = WINDOW_FOCUSED
    idle = _is_idle(now)
    row = self.layout.row(align=True)
    row.separator_spacer()
    focus_txt = "●" if focused else "○"
    idle_txt = " ⏸" if idle else ""
    row.label(text=f"{focus_txt}{idle_txt} Session: {session} | Total: {total}")

class TST_OT_MonitorActivity(bpy.types.Operator):
    bl_idname = "tst.monitor_activity"
    bl_label = "Monitor Blender Focus and Activity"
    bl_options = {'INTERNAL'}
    _timer = None

    def _bump_activity(self):
        global LAST_INPUT_TS
        LAST_INPUT_TS = time.time()

    def modal(self, context, event):
        global WINDOW_FOCUSED
        et = event.type
        ev = event.value
        if et == 'WINDOW_ACTIVATE':
            WINDOW_FOCUSED = True
            self._bump_activity()
        elif et == 'WINDOW_DEACTIVATE':
            WINDOW_FOCUSED = False
        if et in {'MOUSEMOVE', 'INBETWEEN_MOUSEMOVE', 'TRACKPADPAN', 'TRACKPADZOOM'}:
            self._bump_activity()
        if et in {'LEFTMOUSE', 'RIGHTMOUSE', 'MIDDLEMOUSE',
                  'BUTTON4MOUSE', 'BUTTON5MOUSE',
                  'WHEELUPMOUSE', 'WHEELDOWNMOUSE', 'WHEELINMOUSE', 'WHEELOUTMOUSE'} and ev in {'PRESS', 'RELEASE', 'CLICK', 'DOUBLE_CLICK'}:
            self._bump_activity()
        if ev in {'PRESS', 'RELEASE'} and et not in {'TIMER'}:
            self._bump_activity()
        if et in {'TIMER', 'WINDOW_ACTIVATE', 'WINDOW_DEACTIVATE'}:
            return {'PASS_THROUGH'}
        return {'PASS_THROUGH'}

    def cancel(self, context):
        wm = _safe_getattr(context, "window_manager")
        if wm and self._timer:
            try:
                wm.event_timer_remove(self._timer)
            except Exception:
                pass
        self._timer = None

    def invoke(self, context, event=None):
        wm = _safe_getattr(context, "window_manager")
        if not wm:
            return {'CANCELLED'}
        try:
            self._timer = wm.event_timer_add(0.5, window=context.window)
        except Exception:
            self._timer = wm.event_timer_add(0.5)
        wm.modal_handler_add(self)
        self._bump_activity()
        return {'RUNNING_MODAL'}

def _ensure_monitor_running():
    global _MONITOR_STARTED
    if _MONITOR_STARTED:
        return
    try:
        bpy.ops.tst.monitor_activity('INVOKE_DEFAULT')
        _MONITOR_STARTED = True
    except Exception:
        _MONITOR_STARTED = False

def _lazy_init_if_needed():
    global INITIALIZED, START_TIME, LAST_TICK_TS, LAST_SAVE_TS, LAST_INPUT_TS
    if INITIALIZED:
        return
    _refresh_current_context()
    now = time.time()
    if START_TIME is None:
        START_TIME = now
        LAST_TICK_TS = START_TIME
        LAST_SAVE_TS = START_TIME
    if LAST_INPUT_TS <= 0.0:
        LAST_INPUT_TS = now
    INITIALIZED = True

def timer_tick():
    _lazy_init_if_needed()
    _ensure_monitor_running()
    now = time.time()
    _tick_accumulate(now)
    for area in _iter_areas():
        if area.type in {"STATUSBAR", "VIEW_3D", "TEXT_EDITOR", "OUTLINER"}:
            try:
                area.tag_redraw()
            except Exception:
                pass
    _save_checkpoint_if_due(now)
    return TIMER_PERIOD

class TST_Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__
    idle_timeout_seconds: bpy.props.IntProperty(
        name="Idle Timeout (seconds)",
        description="Pause tracking after this many seconds without input (0 disables idle detection)",
        min=0,
        max=3600,
        default=30,
    )

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        col.label(text="Time Spent Tracker", icon='TIME')
        col.prop(self, "idle_timeout_seconds")
        now = time.time()
        col.label(text=f"Focus: {'Focused' if WINDOW_FOCUSED else 'Not Focused'}")
        col.label(text=f"Idle: {'Yes' if _is_idle(now) else 'No'}")
        col.label(text=f"Current session: {_format_hms(_current_session_seconds())}")
        col.label(text=f"Total: {_format_hms(TOTAL_SECONDS)}")
        col.separator()
        col.label(text=f"Current project:", icon='FILE_FOLDER')
        col.label(text=f"  {CURRENT_PROJECT}")
        col.label(text=f"  Time: {_format_hms(PROJECT_SECONDS.get(CURRENT_PROJECT, 0.0))}")
        col.label(text=f"Current file:", icon='FILE_BLEND')
        col.label(text=f"  {CURRENT_FILE}")
        col.label(text=f"  Time: {_format_hms(FILE_SECONDS.get(CURRENT_FILE, 0.0))}")
        col.separator()
        col.label(text="Top Projects:", icon='SORTTIME')
        for p, secs in _top_n(PROJECT_SECONDS, 5):
            col.label(text=f"{_format_hms(secs)}  –  {p}")
        col.separator()
        row = col.row(align=True)
        row.operator("tst.export_csv", text="Export CSV", icon='EXPORT')
        row.operator("tst.reset_total", text="Reset All Times", icon='TRASH')

class TST_OT_ResetTotal(bpy.types.Operator):
    bl_idname = "tst.reset_total"
    bl_label = "Reset All Times"
    bl_description = "Reset total, per-project, and per-file time to zero"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        global TOTAL_SECONDS, PROJECT_SECONDS, FILE_SECONDS, START_TIME, LAST_TICK_TS, LAST_SAVE_TS, LAST_INPUT_TS
        TOTAL_SECONDS = 0.0
        PROJECT_SECONDS = {}
        FILE_SECONDS = {}
        now = time.time()
        START_TIME = now
        LAST_TICK_TS = now
        LAST_SAVE_TS = now
        LAST_INPUT_TS = now
        _save_data()
        self.report({'INFO'}, "All times reset.")
        return {'FINISHED'}

class TST_OT_ExportCSV(bpy.types.Operator):
    bl_idname = "tst.export_csv"
    bl_label = "Export CSV"
    bl_description = "Export per-project and per-file totals to a CSV in your config folder"
    bl_options = {'REGISTER'}

    def execute(self, context):
        path = _get_csv_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", newline='', encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Type", "Key", "Seconds", "HH:MM:SS"])
                writer.writerow(["Summary", "TOTAL", int(TOTAL_SECONDS), _format_hms(TOTAL_SECONDS)])
                writer.writerow([])
                writer.writerow(["Projects", "—", "—", "—"])
                for k, v in sorted(PROJECT_SECONDS.items(), key=lambda kv: kv[1], reverse=True):
                    writer.writerow(["Project", k, int(v), _format_hms(v)])
                writer.writerow([])
                writer.writerow(["Files", "—", "—", "—"])
                for k, v in sorted(FILE_SECONDS.items(), key=lambda kv: kv[1], reverse=True):
                    writer.writerow(["File", k, int(v), _format_hms(v)])
            self.report({'INFO'}, f"Exported to {path}")
        except Exception as e:
            self.report({'ERROR'}, f"Failed to export CSV: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

def _refresh_current_context():
    global CURRENT_FILE, CURRENT_PROJECT
    CURRENT_FILE = _current_file()
    CURRENT_PROJECT = _current_project()
    _ensure_keys(CURRENT_FILE, CURRENT_PROJECT)

@persistent
def on_blend_save_pre(dummy):
    _save_data()

@persistent
def on_blend_save_post(dummy):
    _refresh_current_context()

@persistent
def on_load_post(dummy):
    _lazy_init_if_needed()
    _refresh_current_context()
    _ensure_monitor_running()
    for area in _iter_areas():
        try:
            area.tag_redraw()
        except Exception:
            pass

classes = (
    TST_Preferences,
    TST_OT_ResetTotal,
    TST_OT_ExportCSV,
    TST_OT_MonitorActivity,
)

def register():
    global START_TIME, LAST_TICK_TS, LAST_SAVE_TS, INITIALIZED, _MONITOR_STARTED, LAST_INPUT_TS
    for cls in classes:
        bpy.utils.register_class(cls)
    _load_data()
    try:
        bpy.types.STATUSBAR_HT_header.append(draw_statusbar)
    except Exception:
        bpy.types.VIEW3D_HT_header.append(draw_statusbar)
    now = time.time()
    START_TIME = now
    LAST_TICK_TS = now
    LAST_SAVE_TS = now
    LAST_INPUT_TS = now
    INITIALIZED = False
    _MONITOR_STARTED = False
    bpy.app.timers.register(timer_tick, first_interval=TIMER_PERIOD, persistent=True)
    bpy.app.handlers.save_pre.append(on_blend_save_pre)
    bpy.app.handlers.save_post.append(on_blend_save_post)
    bpy.app.handlers.load_post.append(on_load_post)

def unregister():
    global TOTAL_SECONDS
    try:
        _tick_accumulate(time.time())
    except Exception:
        pass
    _save_data()
    try:
        bpy.types.STATUSBAR_HT_header.remove(draw_statusbar)
    except Exception:
        try:
            bpy.types.VIEW3D_HT_header.remove(draw_statusbar)
        except Exception:
            pass
    try:
        if on_blend_save_pre in bpy.app.handlers.save_pre:
            bpy.app.handlers.save_pre.remove(on_blend_save_pre)
        if on_blend_save_post in bpy.app.handlers.save_post:
            bpy.app.handlers.save_post.remove(on_blend_save_post)
        if on_load_post in bpy.app.handlers.load_post:
            bpy.app.handlers.load_post.remove(on_load_post)
    except Exception:
        pass
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

if __name__ == "__main__":
    register()

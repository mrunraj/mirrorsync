#!/usr/bin/env python3
"""MirrorSync — friendly one-way folder mirroring for Windows.

True mirror fidelity (adds, updates, AND deletions) powered by robocopy,
with a safety net: deleted/changed-away files go to a recycle folder for
N days instead of vanishing. Writes a machine-readable heartbeat JSON
(_last-sync.json) after every run so downstream automations can verify
the mirror is fresh.

Features
- Tray app (pystray) with a simple tkinter window: presets, run-now, log viewer.
- Presets: source -> destination, multiple daily times, weekday selection,
  excluded subfolders, delete mode, recycle retention.
- Catch-up: if the PC was asleep/off at a scheduled time, the slot runs the
  next time the app is up (same day).
- Engine: robocopy on Windows (battle-tested); pure-Python fallback elsewhere
  (used for tests / non-Windows).
- Heartbeat: same schema as the classic "K-Mirror Sync" robocopy task, so
  existing vault watchdogs keep working unchanged.
- CLI: `python mirrorsync.py --run-all` runs every enabled preset once,
  headless (usable from Task Scheduler).

MIT License.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

APP_NAME = "MirrorSync"
HEARTBEAT_DIRNAME = "_Mirror-Sync"
HEARTBEAT_FILENAME = "_last-sync.json"
RECYCLE_DIRNAME = "_MirrorSync-Recycle"
SKIP_FILES = {"thumbs.db", "desktop.ini"}  # Windows junk, never mirrored
SCHED_POLL_SECONDS = 20

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------

def config_dir():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
    else:
        base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    d = os.path.join(base, APP_NAME)
    os.makedirs(os.path.join(d, "logs"), exist_ok=True)
    return d


def _json_load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _json_save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def default_preset(name="New preset"):
    return {
        "name": name,
        "source": "",
        "dest": "",
        "times": ["08:00", "12:30", "16:00"],
        "days": [0, 1, 2, 3, 4],          # Mon..Fri (Mon=0)
        "excludes": [],                     # subfolder names or relative paths
        "delete_mode": "recycle",          # recycle | permanent | copy-only
        "retention_days": 30,
        "heartbeat": True,
        "enabled": True,
    }


class Store:
    def __init__(self):
        self.dir = config_dir()
        self.presets_path = os.path.join(self.dir, "presets.json")
        self.state_path = os.path.join(self.dir, "state.json")
        self.log_dir = os.path.join(self.dir, "logs")
        self.presets = _json_load(self.presets_path, [])
        self.state = _json_load(self.state_path, {})  # {preset: {date: [slots]}}

    def save_presets(self):
        _json_save(self.presets_path, self.presets)

    def save_state(self):
        # keep only today's + yesterday's slot records
        keep = {datetime.now().strftime("%Y-%m-%d"),
                (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")}
        for p in list(self.state):
            self.state[p] = {d: s for d, s in self.state[p].items() if d in keep}
        _json_save(self.state_path, self.state)

    def slot_done(self, preset_name, date_str, slot):
        return slot in self.state.get(preset_name, {}).get(date_str, [])

    def mark_slot(self, preset_name, date_str, slot):
        self.state.setdefault(preset_name, {}).setdefault(date_str, []).append(slot)
        self.save_state()


# --------------------------------------------------------------------------
# Sync engine
# --------------------------------------------------------------------------

def _log_line(logf, msg):
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    logf.write(line + "\n")
    logf.flush()


def _is_excluded(rel, excludes):
    parts = rel.replace("\\", "/").split("/")
    for ex in excludes:
        exn = ex.replace("\\", "/").strip("/")
        if not exn:
            continue
        if rel.replace("\\", "/") == exn or rel.replace("\\", "/").startswith(exn + "/"):
            return True
        if exn in parts:  # bare folder name matches anywhere (robocopy /XD behavior)
            return True
    return False


def _walk_files(root, excludes, skip_names):
    """Yield relpaths of files under root, honoring excludes and skip dirs."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        rel_dir = "" if rel_dir == "." else rel_dir
        dirnames[:] = [d for d in dirnames
                       if d not in skip_names
                       and not _is_excluded(os.path.join(rel_dir, d), excludes)]
        for fn in filenames:
            if fn.lower() in SKIP_FILES:
                continue
            rel = os.path.join(rel_dir, fn) if rel_dir else fn
            if not _is_excluded(rel, excludes):
                yield rel


def _python_copy(src, dst, excludes, logf):
    """Fallback copy engine: copy new/changed files (size or mtime differ)."""
    copied = 0
    skip = {RECYCLE_DIRNAME, HEARTBEAT_DIRNAME}
    for rel in _walk_files(src, excludes, skip):
        s = os.path.join(src, rel)
        d = os.path.join(dst, rel)
        try:
            st = os.stat(s)
            if os.path.exists(d):
                dt = os.stat(d)
                if dt.st_size == st.st_size and int(dt.st_mtime) >= int(st.st_mtime):
                    continue
            os.makedirs(os.path.dirname(d) or dst, exist_ok=True)
            shutil.copy2(s, d)
            copied += 1
            _log_line(logf, "copied  %s" % rel)
        except Exception as e:
            _log_line(logf, "ERROR copying %s: %s" % (rel, e))
    return copied, 1 if copied else 0


def _robocopy(src, dst, excludes, log_path, logf):
    # NOTE: we deliberately do NOT use robocopy's /LOG option. Microsoft Store
    # Python virtualizes %APPDATA%, so a log path that works for Python may not
    # exist for the child robocopy process (-> exit 16). Instead we capture
    # robocopy's output ourselves and write it into our own log file.
    cmd = ["robocopy", src, dst, "/E", "/COPY:DAT", "/DCOPY:DAT",
           "/R:2", "/W:5", "/NP", "/NDL"]
    for ex in excludes:
        cmd += ["/XD", ex]
    cmd += ["/XD", RECYCLE_DIRNAME, HEARTBEAT_DIRNAME]
    cmd += ["/XF", "Thumbs.db", "desktop.ini"]
    _log_line(logf, "robocopy: %s -> %s" % (src, dst))
    p = subprocess.run(cmd, creationflags=CREATE_NO_WINDOW,
                       capture_output=True, text=True)
    out = (p.stdout or "") + (p.stderr or "")
    lines = [ln.rstrip() for ln in out.splitlines() if ln.strip()]
    for ln in lines[-80:]:
        logf.write("    " + ln + "\n")
    logf.flush()
    return p.returncode  # 0-7 = success family; >=8 = failure


def _mirror_deletions(src, dst, preset, logf):
    """Move (or delete) files that exist in dst but no longer in src."""
    mode = preset.get("delete_mode", "recycle")
    if mode == "copy-only":
        return 0
    excludes = preset.get("excludes", [])
    skip = {RECYCLE_DIRNAME, HEARTBEAT_DIRNAME}
    src_set = set(_walk_files(src, excludes, skip))
    removed = 0
    recycle_root = os.path.join(os.path.dirname(dst.rstrip("\\/")) or dst,
                                RECYCLE_DIRNAME,
                                datetime.now().strftime("%Y-%m-%d_%H%M"))
    for rel in list(_walk_files(dst, excludes, skip)):
        if rel in src_set:
            continue
        d = os.path.join(dst, rel)
        try:
            if mode == "recycle":
                target = os.path.join(recycle_root, rel)
                os.makedirs(os.path.dirname(target) or recycle_root, exist_ok=True)
                shutil.move(d, target)
                _log_line(logf, "recycled  %s" % rel)
            else:
                os.remove(d)
                _log_line(logf, "deleted  %s" % rel)
            removed += 1
        except Exception as e:
            _log_line(logf, "ERROR removing %s: %s" % (rel, e))
    # prune now-empty directories in dst
    for dirpath, dirnames, filenames in os.walk(dst, topdown=False):
        if dirpath == dst:
            continue
        rel = os.path.relpath(dirpath, dst)
        if _is_excluded(rel, excludes) or RECYCLE_DIRNAME in rel or HEARTBEAT_DIRNAME in rel:
            continue
        try:
            if not os.listdir(dirpath) and not os.path.isdir(os.path.join(src, rel)):
                os.rmdir(dirpath)
        except OSError:
            pass
    return removed


def _purge_recycle(dst, retention_days, logf):
    root = os.path.join(os.path.dirname(dst.rstrip("\\/")) or dst, RECYCLE_DIRNAME)
    if not os.path.isdir(root):
        return
    cutoff = time.time() - retention_days * 86400
    for entry in os.listdir(root):
        p = os.path.join(root, entry)
        try:
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
                _log_line(logf, "purged recycle batch %s (older than %sd)" % (entry, retention_days))
        except OSError:
            pass


def _wake_source(src, logf):
    """Wake a sleeping source before giving up: mapped network drives (K:) show
    'disconnected' until touched, so ask Windows to reconnect the remembered
    mapping. Returns True if the source is reachable."""
    if os.path.isdir(src):
        return True
    drive = os.path.splitdrive(src)[0]
    if IS_WINDOWS and len(drive) == 2 and drive[1] == ":":
        _log_line(logf, "source offline — trying to reconnect drive %s" % drive)
        try:
            q = subprocess.run(["net", "use", drive], capture_output=True, text=True,
                               creationflags=CREATE_NO_WINDOW, timeout=30)
            remote = None
            for line in q.stdout.splitlines():
                line = line.strip()
                if line.lower().startswith("remote name"):
                    remote = line.split(None, 2)[-1].strip()
            if remote and remote.startswith("\\\\"):
                subprocess.run(["net", "use", drive, remote], capture_output=True,
                               text=True, creationflags=CREATE_NO_WINDOW, timeout=60)
                _log_line(logf, "reconnect attempted: %s -> %s" % (drive, remote))
        except Exception as e:
            _log_line(logf, "reconnect attempt failed: %s" % e)
        time.sleep(3)
    return os.path.isdir(src)


def _write_heartbeat(preset, status, exit_code, log_path, note):
    if not preset.get("heartbeat", True):
        return
    dst = preset["dest"]
    hb_dir = os.path.join(os.path.dirname(dst.rstrip("\\/")) or dst, HEARTBEAT_DIRNAME)
    os.makedirs(hb_dir, exist_ok=True)
    data = {
        "lastRun": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "status": status,
        "exitCode": exit_code,
        "source": preset["source"],
        "dest": dst,
        "log": log_path,
        "note": note,
        "machine": socket.gethostname(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER", ""),
        "tool": APP_NAME,
    }
    _json_save(os.path.join(hb_dir, HEARTBEAT_FILENAME), data)


def run_sync(preset, store, trigger="manual", status_cb=None):
    """Run one preset. Returns (ok, note)."""
    name = preset.get("name", "preset")
    src, dst = preset.get("source", ""), preset.get("dest", "")
    log_path = os.path.join(store.log_dir,
                            "sync-%s_%s.log" % (datetime.now().strftime("%Y-%m-%d_%H%M%S"),
                                                "".join(c for c in name if c.isalnum())))
    ok, note = False, ""
    with open(log_path, "a", encoding="utf-8") as logf:
        _log_line(logf, "=== %s run (%s): %s ===" % (APP_NAME, trigger, name))
        if status_cb:
            status_cb("Running: %s ..." % name)
        if not src or not _wake_source(src, logf):
            note = "Source not available: %s" % src
            _log_line(logf, "SKIP - " + note)
            _write_heartbeat(preset, "FAILED", -1, log_path, note)
            if status_cb:
                status_cb("FAILED: %s (source unavailable)" % name)
            return False, note
        try:
            os.makedirs(dst, exist_ok=True)
            if IS_WINDOWS and shutil.which("robocopy"):
                rc = _robocopy(src, dst, preset.get("excludes", []), log_path, logf)
                copy_ok = rc < 8
            else:
                _, rc = _python_copy(src, dst, preset.get("excludes", []), logf)
                copy_ok = True
            removed = _mirror_deletions(src, dst, preset, logf)
            _purge_recycle(dst, int(preset.get("retention_days", 30)), logf)
            ok = copy_ok
            note = ("Changes mirrored" if rc in (1, 2, 3) else "No changes") if ok else \
                   "robocopy failed (exit %s)" % rc
            if removed:
                note += "; %d file(s) %s" % (removed,
                                             "recycled" if preset.get("delete_mode") == "recycle"
                                             else "deleted")
            _log_line(logf, "result: %s (exit %s)" % (note, rc))
            _write_heartbeat(preset, "OK" if ok else "FAILED", rc, log_path, note)
        except Exception as e:
            note = "ERROR: %s" % e
            _log_line(logf, note)
            _write_heartbeat(preset, "FAILED", -1, log_path, note)
            ok = False
    if status_cb:
        status_cb(("Done: %s — %s" if ok else "FAILED: %s — %s") % (name, note))
    return ok, note


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

class Scheduler(threading.Thread):
    def __init__(self, store, status_cb=None):
        super().__init__(daemon=True)
        self.store = store
        self.status_cb = status_cb
        self.stop_flag = threading.Event()
        self.run_lock = threading.Lock()

    def due_slots(self, preset, now):
        if not preset.get("enabled", True):
            return []
        if now.weekday() not in preset.get("days", [0, 1, 2, 3, 4]):
            return []
        today = now.strftime("%Y-%m-%d")
        hhmm_now = now.strftime("%H:%M")
        due = []
        for slot in preset.get("times", []):
            if slot <= hhmm_now and not self.store.slot_done(preset["name"], today, slot):
                due.append(slot)
        return due

    def run(self):
        while not self.stop_flag.wait(SCHED_POLL_SECONDS):
            now = datetime.now()
            for preset in list(self.store.presets):
                for slot in self.due_slots(preset, now):
                    # mark first so a crash doesn't retry-loop the same slot
                    self.store.mark_slot(preset["name"], now.strftime("%Y-%m-%d"), slot)
                    with self.run_lock:
                        run_sync(preset, self.store,
                                 trigger="scheduled %s (catch-up ok)" % slot,
                                 status_cb=self.status_cb)


# --------------------------------------------------------------------------
# Start with Windows
# --------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def get_autostart():
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except OSError:
        return False


def set_autostart(enable):
    if not IS_WINDOWS:
        return
    import winreg
    if getattr(sys, "frozen", False):
        cmd = '"%s" --tray' % sys.executable
    else:
        cmd = '"%s" "%s" --tray' % (sys.executable.replace("python.exe", "pythonw.exe"),
                                    os.path.abspath(__file__))
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# --------------------------------------------------------------------------
# UI (imported lazily so the engine is testable headless)
# --------------------------------------------------------------------------

def make_tray_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(20, 108, 148, 255))
    # two mirrored arrows
    d.polygon([(14, 26), (38, 26), (38, 18), (52, 30), (38, 42), (38, 34), (14, 34)],
              fill=(255, 255, 255, 255))
    return img


class App:
    DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    def __init__(self, store, start_hidden=False):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.store = store
        self.tray = None
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("760x560")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.status_var = tk.StringVar(value="Ready.")
        self._build_ui()
        self.refresh_list()
        self.scheduler = Scheduler(store, status_cb=self.set_status)
        self.scheduler.start()
        self._start_tray()
        if start_hidden and self.tray:
            self.root.withdraw()
        self._tick_log()

    # ---- UI construction -------------------------------------------------
    def _build_ui(self):
        tk, ttk = self.tk, self.ttk
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="both", expand=True)

        left = ttk.Frame(top)
        left.pack(side="left", fill="both", expand=True)
        ttk.Label(left, text="Sync presets", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.listbox = tk.Listbox(left, height=8, activestyle="dotbox")
        self.listbox.pack(fill="both", expand=True, pady=4)

        btns = ttk.Frame(left)
        btns.pack(fill="x")
        for label, cmd in [("Add", self.add_preset), ("Edit", self.edit_preset),
                           ("Remove", self.remove_preset), ("Run now", self.run_selected),
                           ("Run all enabled", self.run_all)]:
            ttk.Button(btns, text=label, command=cmd).pack(side="left", padx=2)

        self.autostart_var = tk.BooleanVar(value=get_autostart())
        ttk.Checkbutton(left, text="Start with Windows (hidden in tray)",
                        variable=self.autostart_var,
                        command=lambda: set_autostart(self.autostart_var.get())
                        ).pack(anchor="w", pady=6)

        ttk.Label(left, text="Activity", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.log_text = tk.Text(left, height=12, state="disabled", font=("Consolas", 9))
        self.log_text.pack(fill="both", expand=True, pady=4)

        bar = ttk.Frame(self.root, padding=(8, 2))
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.status_var).pack(side="left")
        ttk.Button(bar, text="Quit %s" % APP_NAME, command=self.quit_app).pack(side="right")

    def refresh_list(self):
        self.listbox.delete(0, "end")
        for p in self.store.presets:
            days = ",".join(self.DAYS[d] for d in p.get("days", []))
            flag = "" if p.get("enabled", True) else "  [disabled]"
            self.listbox.insert(
                "end", "%s   %s -> %s   @ %s (%s)%s" %
                (p["name"], p["source"], p["dest"], ", ".join(p.get("times", [])), days, flag))

    def set_status(self, msg):
        try:
            self.root.after(0, lambda: self.status_var.set(msg))
        except Exception:
            pass

    def _tick_log(self):
        """Tail the newest log file into the activity pane."""
        try:
            logs = sorted(os.listdir(self.store.log_dir), reverse=True)
            if logs:
                path = os.path.join(self.store.log_dir, logs[0])
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.readlines()[-60:]
                self.log_text.configure(state="normal")
                self.log_text.delete("1.0", "end")
                self.log_text.insert("1.0", "".join(tail))
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except Exception:
            pass
        self.root.after(3000, self._tick_log)

    # ---- Preset dialog ---------------------------------------------------
    def _preset_dialog(self, preset):
        tk, ttk = self.tk, self.ttk
        from tkinter import filedialog, messagebox
        win = tk.Toplevel(self.root)
        win.title("Preset")
        win.grab_set()
        pad = {"padx": 6, "pady": 3}
        entries = {}

        def row(r, label):
            ttk.Label(win, text=label).grid(row=r, column=0, sticky="e", **pad)

        def browse_into(var):
            d = filedialog.askdirectory()
            if d:
                var.set(os.path.normpath(d))

        fields = [("name", "Name"), ("source", "Source folder"), ("dest", "Destination folder"),
                  ("times", "Daily times (HH:MM, comma-sep)"), ("excludes", "Exclude folders (comma-sep)"),
                  ("retention_days", "Recycle retention (days)")]
        for r, (key, label) in enumerate(fields):
            row(r, label)
            val = preset.get(key, "")
            if key in ("times", "excludes"):
                val = ", ".join(val)
            var = tk.StringVar(value=str(val))
            entries[key] = var
            ttk.Entry(win, textvariable=var, width=52).grid(row=r, column=1, sticky="we", **pad)
            if key in ("source", "dest"):
                ttk.Button(win, text="Browse...",
                           command=lambda v=var: browse_into(v)).grid(row=r, column=2, **pad)

        row(6, "Days")
        days_frame = ttk.Frame(win)
        days_frame.grid(row=6, column=1, sticky="w", **pad)
        day_vars = []
        for i, d in enumerate(self.DAYS):
            v = tk.BooleanVar(value=i in preset.get("days", [0, 1, 2, 3, 4]))
            day_vars.append(v)
            ttk.Checkbutton(days_frame, text=d, variable=v).pack(side="left")

        row(7, "Deletions")
        mode_var = tk.StringVar(value=preset.get("delete_mode", "recycle"))
        mode_box = ttk.Combobox(win, textvariable=mode_var, state="readonly", width=50,
                                values=["recycle", "permanent", "copy-only"])
        mode_box.grid(row=7, column=1, sticky="w", **pad)
        ttk.Label(win, text="recycle = mirror w/ 30-day undo · permanent = true mirror · "
                            "copy-only = never remove").grid(row=8, column=1, sticky="w", **pad)

        hb_var = tk.BooleanVar(value=preset.get("heartbeat", True))
        ttk.Checkbutton(win, text="Write heartbeat (_Mirror-Sync\\_last-sync.json next to destination)",
                        variable=hb_var).grid(row=9, column=1, sticky="w", **pad)
        en_var = tk.BooleanVar(value=preset.get("enabled", True))
        ttk.Checkbutton(win, text="Enabled", variable=en_var).grid(row=10, column=1, sticky="w", **pad)

        result = {}

        def save():
            name = entries["name"].get().strip() or "preset"
            src = entries["source"].get().strip()
            dst = entries["dest"].get().strip()
            if not src or not dst:
                messagebox.showerror(APP_NAME, "Source and destination are required.", parent=win)
                return
            if os.path.normcase(os.path.abspath(dst)).startswith(
                    os.path.normcase(os.path.abspath(src)) + os.sep):
                messagebox.showerror(APP_NAME, "Destination cannot be inside the source.", parent=win)
                return
            times = []
            for t in entries["times"].get().split(","):
                t = t.strip()
                if not t:
                    continue
                try:
                    datetime.strptime(t, "%H:%M")
                    times.append(t)
                except ValueError:
                    messagebox.showerror(APP_NAME, "Bad time '%s' — use 24-hour HH:MM." % t, parent=win)
                    return
            try:
                retention = max(1, int(entries["retention_days"].get() or 30))
            except ValueError:
                retention = 30
            result.update({
                "name": name, "source": src, "dest": dst,
                "times": sorted(set(times)) or ["08:00"],
                "days": [i for i, v in enumerate(day_vars) if v.get()] or [0, 1, 2, 3, 4],
                "excludes": [e.strip() for e in entries["excludes"].get().split(",") if e.strip()],
                "delete_mode": mode_var.get(),
                "retention_days": retention,
                "heartbeat": hb_var.get(),
                "enabled": en_var.get(),
            })
            win.destroy()

        ttk.Button(win, text="Save", command=save).grid(row=11, column=1, sticky="e", **pad)
        win.wait_window()
        return result or None

    # ---- Actions ---------------------------------------------------------
    def add_preset(self):
        res = self._preset_dialog(default_preset())
        if res:
            self.store.presets.append(res)
            self.store.save_presets()
            self.refresh_list()

    def _selected_index(self):
        sel = self.listbox.curselection()
        return sel[0] if sel else None

    def edit_preset(self):
        i = self._selected_index()
        if i is None:
            return
        res = self._preset_dialog(self.store.presets[i])
        if res:
            self.store.presets[i] = res
            self.store.save_presets()
            self.refresh_list()

    def remove_preset(self):
        i = self._selected_index()
        if i is None:
            return
        del self.store.presets[i]
        self.store.save_presets()
        self.refresh_list()

    def _run_async(self, presets, trigger):
        def worker():
            for p in presets:
                run_sync(p, self.store, trigger=trigger, status_cb=self.set_status)
        threading.Thread(target=worker, daemon=True).start()

    def run_selected(self):
        i = self._selected_index()
        if i is not None:
            self._run_async([self.store.presets[i]], "manual")

    def run_all(self):
        self._run_async([p for p in self.store.presets if p.get("enabled", True)], "manual (all)")

    # ---- Tray ------------------------------------------------------------
    def _start_tray(self):
        try:
            import pystray
            menu = pystray.Menu(
                pystray.MenuItem("Open %s" % APP_NAME, lambda: self.root.after(0, self.show_window),
                                 default=True),
                pystray.MenuItem("Run all enabled now", lambda: self.run_all()),
                pystray.MenuItem("Quit", lambda: self.root.after(0, self.quit_app)),
            )
            self.tray = pystray.Icon(APP_NAME, make_tray_image(), APP_NAME, menu)
            threading.Thread(target=self.tray.run, daemon=True).start()
        except Exception:
            self.tray = None  # no tray libs — window close minimizes instead

    def show_window(self):
        self.root.deiconify()
        self.root.lift()

    def hide_to_tray(self):
        if self.tray:
            self.root.withdraw()
            self.set_status("Hidden to tray — schedules keep running.")
        else:
            self.root.iconify()

    def quit_app(self):
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.scheduler.stop_flag.set()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main():
    store = Store()
    args = sys.argv[1:]
    if "--run-all" in args:
        rc = 0
        for p in store.presets:
            if p.get("enabled", True):
                ok, note = run_sync(p, store, trigger="cli")
                print("%s: %s" % (p["name"], note))
                rc = rc or (0 if ok else 1)
        sys.exit(rc)
    App(store, start_hidden="--tray" in args).run()


if __name__ == "__main__":
    main()

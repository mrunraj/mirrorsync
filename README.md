# MirrorSync

A friendly Windows tray app that keeps a **true mirror** of a folder (e.g. a mapped
**K:\ drive** or network share → a **OneDrive/SharePoint** folder) — with a safety net
and a heartbeat that downstream automations can trust.

## Why another sync tool?

Most simple sync tools only *copy missing files*. That's not a mirror: files that were
updated or deleted at the source silently go stale at the destination. MirrorSync is
built for feeding **AI knowledge bases and automations** that need the destination to
faithfully reflect the source:

- **True mirror** — new files copied, changed files updated, deleted files removed.
- **Recycle safety net** — by default, removed files aren't destroyed; they're moved to
  a `_MirrorSync-Recycle\<timestamp>\` folder next to the destination and purged after
  30 days. Fidelity *and* an undo button.
- **Heartbeat JSON** — after every run, writes `_Mirror-Sync\_last-sync.json` next to
  the destination (`lastRun`, `status`, `exitCode`, `note`, `machine`, `user`), so a
  watchdog or AI agent can verify the mirror is fresh before trusting it.
- **Catch-up after sleep** — a missed scheduled time runs the next moment the app is
  up that day. No more silent skips because the laptop was closed.
- **Multiple daily times per preset** (e.g. 8:00 / 12:30 / 16:00) and weekday selection.
- **Battle-tested engine** — uses Windows `robocopy` for the copy phase.
- **Tray app** — closing the window hides it; syncs keep running. Optional start with
  Windows.

## Run it

Requires Python 3.9+ on Windows. From this folder:

```
pip install --user pystray Pillow
python mirrorsync.py
```

(Or `pythonw mirrorsync.py` for no console window; `--tray` starts hidden.)

Click **Add** to create a preset:

| Field | Meaning |
|---|---|
| Source | Folder to copy *from* (e.g. `K:\Projects\...`) |
| Destination | Folder to mirror *into* (e.g. a OneDrive-synced library folder) |
| Daily times | 24-hour `HH:MM`, comma-separated — each runs once per day |
| Days | Which weekdays the schedule applies |
| Exclude folders | Subfolder names/paths to skip (like robocopy `/XD`) |
| Deletions | `recycle` (mirror w/ undo, default) · `permanent` (true mirror) · `copy-only` (never remove) |
| Recycle retention | Days before recycled batches are purged |
| Heartbeat | Write `_Mirror-Sync\_last-sync.json` next to the destination |

## Headless mode (Task Scheduler)

```
python mirrorsync.py --run-all
```

Runs every enabled preset once and exits — usable directly from Windows Task Scheduler
if you prefer that over the tray app.

## Build a standalone .exe

```
pip install --user pyinstaller pystray Pillow
powershell -ExecutionPolicy Bypass -File build.ps1
```

Output: `dist\MirrorSync.exe` (single file, no Python needed to run it).

## Where settings live

`%APPDATA%\MirrorSync\` — `presets.json`, `state.json` (per-day slot tracking for
catch-up), and `logs\`. These stay on your machine and are not part of this repo.

## Heartbeat schema

```json
{
  "lastRun":  "2026-09-24T08:16:50",
  "status":   "OK",
  "exitCode": 1,
  "source":   "K:\\...",
  "dest":     "C:\\...\\K-Mirror",
  "log":      "%APPDATA%\\MirrorSync\\logs\\sync-....log",
  "note":     "Changes mirrored",
  "machine":  "HOSTNAME",
  "user":     "username",
  "tool":     "MirrorSync"
}
```

A downstream agent should treat the mirror as **stale** if this file is missing,
`status` is `FAILED`, or `lastRun` is older than expected for the schedule.

## License

MIT — see `LICENSE`.

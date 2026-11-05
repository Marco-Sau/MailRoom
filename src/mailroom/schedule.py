"""Automatic runs: a schedule (days and times), the engine, the look-back window and the
allowed hours, stored in data/schedule.json (this Mac only).

When Automatic is saved, MailRoom installs a per-user LaunchAgent,
~/Library/LaunchAgents/com.mailroom.autorun.plist, so macOS starts `mailroom autorun`
at the chosen times even when the app window is closed. Manual removes it. Nothing else on
the Mac is changed. The run itself is the same as pressing Run: drafts only, nothing is sent.

macOS facts that shape this:
* launchd runs a missed time once when the Mac wakes from sleep; it does not run times that
  passed while the Mac was shut down or you were logged out.
* the allowed-hours window is checked when the run starts, so a catch-up after waking at
  night is skipped.
"""
from __future__ import annotations

import json
import os
import plistlib
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .settings import settings

LABEL = "com.mailroom.autorun"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_TIMES = 24
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

DEFAULT: dict[str, Any] = {
    "enabled": False,
    "job_id": "",
    "lookbackHours": 0,              # 0 = the standard windows (Settings → Look-back windows)
    "days": [1, 2, 3, 4, 5],         # ISO weekdays, 1 = Monday
    "times": ["08:00"],
    "window": {"enabled": False, "start": "07:00", "end": "21:00"},
    "notify": True,
}


class ScheduleError(ValueError):
    pass


# ------------------------------------------------------------------ storage
def config_path() -> Path:
    return settings.data_dir / "schedule.json"


def last_path() -> Path:
    d = settings.data_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d / "autorun-last.json"


def history_path() -> Path:
    return last_path().with_name("autorun-history.json")


def seen_path() -> Path:
    return last_path().with_name("autorun-seen.json")


HISTORY_KEEP = 30


def log_path() -> Path:
    d = settings.data_dir / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "autorun.log"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def load() -> dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT))
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for k in DEFAULT:
        if k in raw:
            cfg[k] = raw[k]
    if not isinstance(cfg.get("window"), dict):
        cfg["window"] = dict(DEFAULT["window"])
    return cfg


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    from .jobs import load_jobs
    cfg = load()
    cfg.update({k: raw[k] for k in DEFAULT if k in raw})
    jobs = {j.job_id for j in load_jobs()}
    if cfg["enabled"] or cfg["job_id"]:
        if cfg["job_id"] not in jobs:
            raise ScheduleError("Choose the engine for automatic runs.")
    try:
        lb = int(cfg["lookbackHours"] or 0)
    except (TypeError, ValueError):
        raise ScheduleError("Look back must be a number of hours.") from None
    if not 0 <= lb <= 168:
        raise ScheduleError("Look back must be between 1 and 168 hours (or Standard).")
    days = sorted({int(d) for d in cfg["days"] or []})
    if any(d < 1 or d > 7 for d in days):
        raise ScheduleError("Days must be Monday to Sunday.")
    times = sorted({str(t).strip() for t in cfg["times"] or [] if str(t).strip()})
    bad = [t for t in times if not _HHMM.match(t)]
    if bad:
        raise ScheduleError(f"Times must look like 08:30 ({', '.join(bad)}).")
    if len(times) > MAX_TIMES:
        raise ScheduleError(f"At most {MAX_TIMES} times a day.")
    if cfg["enabled"] and not days:
        raise ScheduleError("Pick at least one day.")
    if cfg["enabled"] and not times:
        raise ScheduleError("Add at least one time.")
    w = {**DEFAULT["window"], **(cfg.get("window") or {})}
    w["enabled"] = bool(w["enabled"])
    for k in ("start", "end"):
        if not _HHMM.match(str(w[k])):
            raise ScheduleError("Allowed hours must look like 07:00.")
    if w["enabled"]:
        if _mins(w["start"]) >= _mins(w["end"]):
            raise ScheduleError("Allowed hours: the start must be before the end, on the same day.")
        out = [t for t in times if not _mins(w["start"]) <= _mins(t) <= _mins(w["end"])]
        if out:
            raise ScheduleError(f"{', '.join(out)} is outside the allowed hours {w['start']}-{w['end']}.")
    return {"enabled": bool(cfg["enabled"]), "job_id": cfg["job_id"], "lookbackHours": lb, "days": days,
            "times": times, "window": {"enabled": w["enabled"], "start": w["start"], "end": w["end"]},
            "notify": bool(cfg.get("notify", True))}


# ------------------------------------------------------------------ launchd
def _python() -> str:
    venv = settings.root / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def build_plist(cfg: dict[str, Any]) -> dict[str, Any]:
    env = {"PYTHONPATH": str(settings.root / "src")}
    if os.environ.get("MAILROOM_DATA_DIR"):
        env["MAILROOM_DATA_DIR"] = os.environ["MAILROOM_DATA_DIR"]
    slots = []
    for d in cfg["days"]:
        for t in cfg["times"]:
            h, m = t.split(":")
            slots.append({"Weekday": d % 7, "Hour": int(h), "Minute": int(m)})   # launchd: 0 = Sunday
    return {
        "Label": LABEL,
        "ProgramArguments": [_python(), "-m", "mailroom.cli", "autorun"],
        "WorkingDirectory": str(settings.root),
        "EnvironmentVariables": env,
        "StartCalendarInterval": slots,
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
        "ProcessType": "Background",
        "RunAtLoad": False,
        # the job only launches a detached worker (see launch()); never kill it when the
        # schedule is saved again (bootout) or the launcher exits
        "AbandonProcessGroup": True,
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=30)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(cfg: dict[str, Any]) -> None:
    if sys.platform != "darwin":
        raise ScheduleError("Automatic runs in the background need macOS (launchd).")
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", f"{_domain()}/{LABEL}")            # not loaded yet: ignored
    with open(p, "wb") as fh:
        plistlib.dump(build_plist(cfg), fh)
    r = _launchctl("bootstrap", _domain(), str(p))
    if r.returncode != 0:
        raise ScheduleError(f"macOS did not accept the background job: {(r.stderr or r.stdout).strip()[:300]}")


def uninstall() -> None:
    if sys.platform != "darwin":
        return
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    plist_path().unlink(missing_ok=True)


def installed() -> dict[str, Any]:
    p = plist_path()
    out = {"file": p.exists(), "loaded": False, "current": False}
    if not out["file"]:
        return out
    if sys.platform == "darwin":
        out["loaded"] = _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0
    try:
        with open(p, "rb") as fh:
            have = plistlib.load(fh)
        out["current"] = have == build_plist(load())
    except Exception:  # noqa: BLE001 - unreadable plist: reinstall on Save
        out["current"] = False
    return out


def kick() -> None:
    """Start the background job once now (macOS starts it as it would at a scheduled time)."""
    r = _launchctl("kickstart", f"{_domain()}/{LABEL}")
    if r.returncode != 0:
        raise ScheduleError(f"could not start the background job: {(r.stderr or r.stdout).strip()[:300]}")


# ------------------------------------------------------------------ public
def save(raw: dict[str, Any]) -> dict[str, Any]:
    cfg = validate(raw)
    if cfg["enabled"]:
        install(cfg)
    else:
        uninstall()
    config_path().write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return state()


def next_runs(cfg: dict[str, Any], now: datetime | None = None, n: int = 3) -> list[str]:
    if not cfg["enabled"] or not cfg["days"] or not cfg["times"]:
        return []
    now = now or datetime.now()
    out: list[datetime] = []
    for add in range(0, 8):
        day = (now + timedelta(days=add)).date()
        if day.isoweekday() not in cfg["days"]:
            continue
        for t in cfg["times"]:
            h, m = map(int, t.split(":"))
            dt = datetime(day.year, day.month, day.day, h, m)
            if dt > now:
                out.append(dt)
    return [d.isoformat(timespec="minutes") for d in sorted(out)[:n]]


def last_run() -> dict[str, Any] | None:
    try:
        return json.loads(last_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def history(n: int = HISTORY_KEEP) -> list[dict[str, Any]]:
    """Automatic runs, newest first: succeeded, failed and skipped."""
    try:
        h = json.loads(history_path().read_text(encoding="utf-8"))
        h = h if isinstance(h, list) else []
    except (OSError, ValueError):
        last = last_run()                     # runs recorded before the history existed
        h = [last] if last else []
    return h[:n]


def _seen_at() -> str:
    try:
        return str(json.loads(seen_path().read_text(encoding="utf-8")).get("at") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def unseen(n: int = 5) -> dict[str, Any]:
    """Automatic runs that finished since you last pressed Got it in the app."""
    seen = _seen_at()
    new = [h for h in history() if str(h.get("finished") or h.get("at", "")) > seen]
    return {"count": len(new), "items": new[:n]}


def mark_seen() -> dict[str, Any]:
    stamps = [str(x.get("finished") or x.get("at", "")) for x in history()]
    seen_path().write_text(json.dumps({"at": max(stamps) if stamps else datetime.now().isoformat(timespec="seconds")}),
                           encoding="utf-8")
    return unseen()


def state() -> dict[str, Any]:
    cfg = load()
    return {"config": cfg, "installed": installed(), "next": next_runs(cfg), "last": last_run(),
            "history": history(8), "unseen": unseen(),
            "platform_ok": sys.platform == "darwin", "plist": str(plist_path()), "log": str(log_path())}


# ------------------------------------------------------------------ the background run
def _in_window(cfg: dict[str, Any], now: datetime) -> bool:
    w = cfg.get("window") or {}
    if not w.get("enabled"):
        return True
    m = now.hour * 60 + now.minute
    return _mins(w["start"]) <= m <= _mins(w["end"])


def note_interrupted(row: dict[str, Any]) -> None:
    """An automatic run whose process was stopped before it could record its result."""
    try:
        at = datetime.fromisoformat(row["started_at"]).astimezone().replace(tzinfo=None).isoformat(timespec="seconds")
    except (KeyError, TypeError, ValueError):
        at = datetime.now().isoformat(timespec="seconds")
    h = history()
    for x in h:
        if x.get("run_id") == row["id"]:
            x.update(status="interrupted", finished=datetime.now().isoformat(timespec="seconds"))
            break
    else:
        h.append({"status": "interrupted", "run_id": row["id"], "job": row.get("job_id"), "at": at,
                  "finished": datetime.now().isoformat(timespec="seconds"),
                  "error": "the background job was closed before the run finished"})
    h.sort(key=lambda x: str(x.get("at", "")), reverse=True)
    history_path().write_text(json.dumps(h[:HISTORY_KEEP]), encoding="utf-8")


def _record(**info: Any) -> None:
    info.setdefault("at", datetime.now().isoformat(timespec="seconds"))
    info.setdefault("finished", max(datetime.now().isoformat(timespec="seconds"), str(info["at"])))
    last_path().write_text(json.dumps(info), encoding="utf-8")
    h = [x for x in history() if x.get("at") != info["at"]]
    history_path().write_text(json.dumps([info, *h][:HISTORY_KEEP]), encoding="utf-8")


def _notify(text: str) -> None:
    if sys.platform != "darwin":
        return
    safe = text.replace("\\", "").replace('"', "'")[:200]
    subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "MailRoom"'],
                   capture_output=True, timeout=15)


def _trim_log(limit: int = 1_000_000) -> None:
    p = log_path()
    try:
        if p.stat().st_size > limit:
            tail = p.read_bytes()[-limit // 4:]
            p.write_bytes(tail)
    except OSError:
        pass


def _summary(stats: dict[str, Any]) -> str:
    parts = []
    for k, label in (("reply_drafts", "reply drafts"), ("event_notes", "event notes"), ("booking_notes", "booking notes")):
        if stats.get(k):
            parts.append(f"{stats[k]} {label}")
    return ", ".join(parts) or "nothing new"


def launch() -> int:
    """What the LaunchAgent runs: start the real run as a detached worker and return at once.

    Saving the schedule reloads the LaunchAgent (launchctl bootout + bootstrap), and bootout
    stops the job's process. Running the work in its own session keeps a run alive through that.
    """
    with open(log_path(), "ab") as log:
        subprocess.Popen([_python(), "-m", "mailroom.cli", "autorun", "--worker"], cwd=str(settings.root),
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                         env={**os.environ, "PYTHONPATH": str(settings.root / "src")})
    return 0


def _graceful_stop() -> None:
    """Logout or shutdown sends SIGTERM: stop like the Stop button, so the run records its result."""
    import signal

    from .runners import control

    def handler(signum, frame):  # noqa: ARG001
        control._stop.set()

    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:  # not the main thread (tests)
        pass


def autorun(now: datetime | None = None) -> int:
    """The automatic run (the detached worker). Exit code 0 also for skips, so launchd never retries."""
    from . import db, run_lock
    from .jobs import get_job
    from .runners import get_runner

    _trim_log()
    _graceful_stop()
    now = now or datetime.now()
    stamp = now.isoformat(timespec="seconds")
    cfg = load()
    if not cfg["enabled"]:
        print(f"{stamp} skipped: automatic runs are off")
        return 0
    if not _in_window(cfg, now):
        w = cfg["window"]
        print(f"{stamp} skipped: outside the allowed hours {w['start']}-{w['end']}")
        _record(status="skipped", reason=f"outside the allowed hours {w['start']}-{w['end']}", job=cfg["job_id"], at=stamp)
        return 0
    try:
        spec = get_job(cfg["job_id"])
    except KeyError:
        print(f"{stamp} skipped: engine {cfg['job_id']!r} no longer exists")
        _record(status="failed", reason="the chosen engine no longer exists: choose it again", job=cfg["job_id"], at=stamp)
        return 0
    if not run_lock.try_acquire("automatic", job=spec.job_id, runner=spec.runner):
        other = run_lock.held_elsewhere() or {}
        print(f"{stamp} skipped: another run is in progress ({other.get('owner', 'app')})")
        _record(status="skipped", reason="another run was in progress", job=spec.job_id, at=stamp)
        return 0
    try:
        spec.hours = int(cfg["lookbackHours"]) or None
        spec.name = f"{spec.name} (automatic)"
        db.audit("schedule", "autorun_started", spec.job_id, f"look back {spec.hours or 'standard'}")
        print(f"{stamp} starting {spec.job_id} (look back {spec.hours or 'standard'})", flush=True)
        result = get_runner(spec.runner).run(spec)
        summ = _summary(result.stats or {})
        _record(status=result.status, run_id=result.run_id, job=spec.job_id, at=stamp,
                summary=summ, error=(result.error or "")[:300])
        print(f"{datetime.now().isoformat(timespec='seconds')} {result.status}: {summ}"
              + (f" · {result.error[:300]}" if result.error else ""), flush=True)
        if cfg.get("notify", True):
            name = spec.name.replace(" (automatic)", "")
            _notify(f"Automatic run ({name}) {result.status}: {summ}. Drafts only, nothing sent."
                    if result.status == "succeeded" else f"Automatic run ({name}) {result.status}. Open MailRoom → Runs.")
        return 0
    except Exception as exc:  # noqa: BLE001 - recorded and shown in the Run tab
        _record(status="failed", job=spec.job_id, at=stamp, error=f"{type(exc).__name__}: {exc}"[:300])
        print(f"{stamp} failed: {type(exc).__name__}: {exc}", flush=True)
        if cfg.get("notify", True):
            _notify("Automatic run failed. Open MailRoom → Run for details.")
        return 0
    finally:
        run_lock.release()

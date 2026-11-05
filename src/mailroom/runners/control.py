"""The run currently in flight, and the Stop request for it.

Only one run happens at a time (routes_jobs and run_lock hold locks), so a single slot is
enough. Runners call `begin()` / `end()` and poll `stop_requested()`; the API's
Stop button calls `request_stop()`. A run in another process (the background automatic
run) is stopped through a file, data/run/stop.request, which its runner sees on its next poll.
"""
from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_current: dict[str, Any] = {}
_stop = threading.Event()


def _stop_file():
    from ..settings import settings
    return settings.data_dir / "run" / "stop.request"


def _clear_stop_file() -> None:
    try:
        _stop_file().unlink(missing_ok=True)
    except OSError:
        pass


class Stopped(Exception):
    """Raised inside a runner when the user pressed Stop."""


def begin(run_id: int, runner: str, **info: Any) -> None:
    with _lock:
        _current.clear()
        _current.update({"run_id": run_id, "runner": runner, **info})
        _stop.clear()
    _clear_stop_file()
    try:
        from .. import run_lock
        run_lock.update(run_id=run_id, runner=runner)
    except Exception:  # noqa: BLE001 - the lock note is informational
        pass


def update(**info: Any) -> None:
    with _lock:
        _current.update(info)


def end() -> None:
    with _lock:
        _current.clear()
        _stop.clear()
    _clear_stop_file()


def current() -> dict[str, Any]:
    with _lock:
        return dict(_current)


def request_stop() -> dict[str, Any]:
    with _lock:
        if _current:
            _stop.set()
            return dict(_current)
    from .. import run_lock
    other = run_lock.held_elsewhere()
    if not other:
        return {}
    f = _stop_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(str(other.get("pid", "")), encoding="utf-8")
    return {"run_id": other.get("run_id"), "runner": other.get("runner") or other.get("owner"), "external": True}


def stop_requested() -> bool:
    if _stop.is_set():
        return True
    if not _current:
        return False
    try:
        return _stop_file().exists()
    except OSError:
        return False


def check() -> None:
    if _stop.is_set():
        raise Stopped("stopped by you")

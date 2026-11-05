"""One run at a time across processes: the app window and the background automatic run.

An advisory `flock` on data/run/run.lock. The kernel drops the lock when the holding process
exits, so a crash never leaves a stale lock behind. The file also records who holds it.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows: no cross-process lock, in-process lock only
    fcntl = None  # type: ignore[assignment]

from .settings import settings

_fh = None


def path():
    d = settings.data_dir / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d / "run.lock"


def try_acquire(owner: str, **info: Any) -> bool:
    """Take the lock without waiting. False when another process holds it."""
    global _fh
    if fcntl is None:
        return True
    if _fh is not None:
        return False
    fh = open(path(), "a+", encoding="utf-8")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False
    fh.seek(0)
    fh.truncate()
    fh.write(json.dumps({"pid": os.getpid(), "owner": owner, "since": time.time(), **info}))
    fh.flush()
    _fh = fh
    return True


def update(**info: Any) -> None:
    """Add details (the run id) to the lock this process holds."""
    if _fh is None:
        return
    try:
        _fh.seek(0)
        cur = json.loads(_fh.read() or "{}")
    except ValueError:
        cur = {}
    _fh.seek(0)
    _fh.truncate()
    _fh.write(json.dumps({**cur, **info}))
    _fh.flush()


def holding() -> bool:
    return _fh is not None


def release() -> None:
    global _fh
    if _fh is None:
        return
    try:
        _fh.seek(0)
        _fh.truncate()
        fcntl.flock(_fh, fcntl.LOCK_UN)
    finally:
        _fh.close()
        _fh = None


def held_elsewhere() -> dict[str, Any] | None:
    """Who holds the lock, when it is another process; None when free or held by this process."""
    if fcntl is None or _fh is not None:
        return None
    p = path()
    if not p.exists():
        return None
    with open(p, "a+", encoding="utf-8") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            try:
                return json.loads(fh.read() or "{}") or {"owner": "another process"}
            except ValueError:
                return {"owner": "another process"}
        fcntl.flock(fh, fcntl.LOCK_UN)
        return None

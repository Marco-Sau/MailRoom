"""macOS desktop entry point.

Runs the FastAPI server on a loopback port inside this process and shows the UI
in a native WKWebView window (pywebview). If pywebview is unavailable the app
degrades to the default browser rather than failing to start - the server is the
product, the window is a shell around it.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from .settings import settings

log = logging.getLogger("mailroom.desktop")
WINDOW = {"width": 1180, "height": 840, "min": (820, 600)}


def _pick_port(preferred: int) -> int:
    """Use the configured port when free, otherwise let the OS choose one."""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", candidate))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free loopback port")


class _Server(uvicorn.Server):
    """uvicorn installs signal handlers that only work on the main thread."""

    def install_signal_handlers(self) -> None:  # noqa: D102
        pass


def _wait_until_up(url: str, timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:  # noqa: BLE001 - server still binding
            time.sleep(0.25)
    return False


def _already_running() -> str | None:
    """Return the URL of a live instance, if one is answering."""
    rt = settings.data_dir / "runtime.json"
    if not rt.exists():
        return None
    try:
        url = json.loads(rt.read_text(encoding="utf-8"))["url"]
        with urllib.request.urlopen(url + "/healthz", timeout=1) as r:
            return url if r.status == 200 else None
    except Exception:  # noqa: BLE001 - stale file, dead process, changed port
        return None


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    running = _already_running()
    if running:
        log.info("MailRoom is already running at %s", running)
        webbrowser.open(running)
        return 0

    port = _pick_port(settings.port)
    url = f"http://127.0.0.1:{port}"

    config = uvicorn.Config("mailroom.main:app", host="127.0.0.1", port=port,
                            log_level="info", access_log=False)
    server = _Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    if not _wait_until_up(url):
        log.error("server did not come up on %s", url)
        return 1

    # Let the CLI and any external tooling find the running instance.
    (settings.data_dir / "runtime.json").write_text(
        json.dumps({"pid": os.getpid(), "port": port, "url": url}), encoding="utf-8")
    log.info("MailRoom serving on %s", url)

    try:
        import webview  # type: ignore
    except ImportError:
        log.warning("pywebview not installed; falling back to the default browser")
        webbrowser.open(url)
        try:
            while thread.is_alive():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    webview.create_window("MailRoom", url,
                          width=WINDOW["width"], height=WINDOW["height"],
                          min_size=WINDOW["min"], text_select=True)
    webview.start()          # blocks until the window is closed
    server.should_exit = True
    thread.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

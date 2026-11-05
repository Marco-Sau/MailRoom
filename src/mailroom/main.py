from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import db
from .api import routes_drafts, routes_jobs, routes_recaps, routes_review, routes_runs, routes_schedule, routes_settings
from .settings import settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app() -> FastAPI:
    db.init_db()
    try:                                    # rows left 'running' by a closed app or a stopped background job
        from . import run_state
        run_state.reconcile()
    except Exception:  # noqa: BLE001
        pass
    app = FastAPI(title="MailRoom", version="0.1.0",
                  description="Local control panel for AI-assisted email triage.")
    app.include_router(routes_jobs.router)
    app.include_router(routes_runs.router)
    app.include_router(routes_drafts.router)
    app.include_router(routes_settings.router)
    app.include_router(routes_recaps.router)
    app.include_router(routes_review.router)
    app.include_router(routes_schedule.router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    if settings.web_dir.exists():
        app.mount("/static", StaticFiles(directory=settings.web_dir), name="static")

        @app.get("/")
        def index():
            # The desktop webview caches aggressively; version the assets by
            # mtime so an edited app.js/styles.css is always picked up.
            html = (settings.web_dir / "index.html").read_text(encoding="utf-8")
            for asset in ("app.js", "styles.css"):
                v = int((settings.web_dir / asset).stat().st_mtime)
                html = html.replace(f"/static/{asset}\"", f"/static/{asset}?v={v}\"")
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return app


app = create_app()

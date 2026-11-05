"""`mailroom` entry point: serve the UI, or run a job headless."""
from __future__ import annotations

import argparse
import json
import sys
import webbrowser

from . import db
from .jobs import get_job, load_jobs
from .runners import get_runner
from .settings import settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="mailroom")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the local UI")
    s.add_argument("--port", type=int, default=settings.port)
    s.add_argument("--no-open", action="store_true")

    r = sub.add_parser("run", help="run one job headless")
    r.add_argument("job_id")

    sub.add_parser("jobs", help="list configured jobs")
    a = sub.add_parser("autorun", help="the scheduled automatic run (started by the LaunchAgent)")
    a.add_argument("--worker", action="store_true", help="run now in this process (the LaunchAgent starts one)")

    w = sub.add_parser("n8n-webhook", help="add a Webhook trigger to an n8n workflow")
    w.add_argument("workflow", help="workflow id, or its name")
    w.add_argument("--path", default="gmail-assistant")
    w.add_argument("--activate", action="store_true")
    w.add_argument("--apply", action="store_true",
                   help="actually change it; without this you get a dry run")

    args = ap.parse_args(argv)
    db.init_db()

    if args.cmd == "jobs":
        for j in load_jobs():
            print(f"{j.job_id:24} {j.runner:9} {j.provider or '-':10} {j.name}")
        return 0

    if args.cmd == "n8n-webhook":
        from .connectors import n8n as n8n_conn
        if not args.apply:
            print(json.dumps(n8n_conn.plan_webhook_trigger(args.workflow, args.path), indent=2))
            print("\n(dry run — re-run with --apply to make the change)")
            return 0
        print(json.dumps(n8n_conn.apply_webhook_trigger(
            args.workflow, args.path, activate=args.activate), indent=2))
        return 0

    if args.cmd == "autorun":
        from . import schedule
        return schedule.autorun() if args.worker else schedule.launch()

    if args.cmd == "run":
        from . import run_lock
        if not run_lock.try_acquire("cli", job=args.job_id):
            print("another run is in progress", file=sys.stderr)
            return 2
        spec = get_job(args.job_id)
        result = get_runner(spec.runner).run(spec)
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return 0 if result.status == "succeeded" else 1

    import uvicorn
    url = f"http://{settings.host}:{args.port}"
    if not args.no_open:
        webbrowser.open(url)
    uvicorn.run("mailroom.main:app", host=settings.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

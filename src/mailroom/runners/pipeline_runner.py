"""Native runner: the app owns the orchestration and swaps the LLM behind it."""
from __future__ import annotations

from datetime import datetime, timezone

from .. import db
from ..models import RunResult, RunSpec
from ..providers import get_provider
from ..rules import Rules
from ..pipeline.steps import STEPS, RunContext, step_persist
from . import control
from .base import Runner

DEFAULT_STEPS = ["fetch", "classify", "prioritize", "calendar", "draft", "sheets"]


class PipelineRunner(Runner):
    name = "pipeline"

    def run(self, spec: RunSpec) -> RunResult:
        rules = Rules.load()
        provider = get_provider(spec.provider or "anthropic", spec.model)
        run_id = db.start_run(spec.job_id, spec.name, "pipeline", provider.name, provider.model)
        started = datetime.now(timezone.utc)
        ctx = RunContext(spec=spec, rules=rules, provider=provider, run_id=run_id)
        ctx.say(f"run {run_id} started: {spec.name}")
        control.begin(run_id, "pipeline")

        try:
            for name in (spec.steps or DEFAULT_STEPS):
                control.check()            # Stop takes effect between steps
                fn = STEPS.get(name)
                if fn is None:
                    ctx.say(f"unknown step '{name}', skipped")
                    continue
                if name == "sheets":
                    # Persist locally before any external write, so a Sheets
                    # failure never loses the triage work.
                    step_persist(ctx)
                fn(ctx)
            if "sheets" not in (spec.steps or DEFAULT_STEPS):
                step_persist(ctx)

            ctx.stats["tokens_in"] = provider.usage.input_tokens
            ctx.stats["tokens_out"] = provider.usage.output_tokens
            db.finish_run(run_id, "succeeded", ctx.stats, ctx.log_lines)
            db.audit("pipeline", "run_succeeded", str(run_id), spec.name)
            return RunResult(run_id=run_id, job_id=spec.job_id, status="succeeded",
                             started_at=started, finished_at=datetime.now(timezone.utc),
                             stats=ctx.stats, log=ctx.log_lines)
        except control.Stopped:
            ctx.say("STOPPED by you. Work done before this point is kept; nothing was sent.")
            step_persist(ctx)
            db.finish_run(run_id, "cancelled", ctx.stats, ctx.log_lines, "stopped by you")
            db.audit("user", "run_stopped", str(run_id), spec.name)
            return RunResult(run_id=run_id, job_id=spec.job_id, status="cancelled",
                             started_at=started, finished_at=datetime.now(timezone.utc),
                             stats=ctx.stats, error="stopped by you", log=ctx.log_lines)
        except Exception as exc:  # noqa: BLE001
            ctx.say(f"FAILED: {exc}")
            db.finish_run(run_id, "failed", ctx.stats, ctx.log_lines, str(exc))
            db.audit("pipeline", "run_failed", str(run_id), str(exc)[:500])
            return RunResult(run_id=run_id, job_id=spec.job_id, status="failed",
                             started_at=started, finished_at=datetime.now(timezone.utc),
                             stats=ctx.stats, error=str(exc), log=ctx.log_lines)
        finally:
            control.end()

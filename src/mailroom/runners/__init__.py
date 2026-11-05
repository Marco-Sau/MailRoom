"""Runner registry: one entry per execution backend."""
from __future__ import annotations

from .assistant_runner import AssistantRunner
from .base import Runner
from .n8n_runner import N8nRunner
from .pipeline_runner import PipelineRunner

RUNNERS: dict[str, type[Runner]] = {
    "pipeline": PipelineRunner,
    "n8n": N8nRunner,
    "assistant": AssistantRunner,
}


def get_runner(name: str) -> Runner:
    if name not in RUNNERS:
        raise KeyError(f"unknown runner '{name}'. Known: {', '.join(RUNNERS)}")
    return RUNNERS[name]()

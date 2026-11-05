from __future__ import annotations

import abc

from ..models import RunResult, RunSpec


class Runner(abc.ABC):
    name: str

    @abc.abstractmethod
    def run(self, spec: RunSpec) -> RunResult:
        ...

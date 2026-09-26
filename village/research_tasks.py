"""Versioned synthetic task manifest for offline capability comparisons."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Iterable


@dataclass(frozen=True)
class ResearchTask:
    task_id: str
    category: str
    prompt: str
    success_criterion: str
    max_tokens: int = 512

    def validate(self) -> None:
        if not all(str(value).strip() for value in (self.task_id, self.category, self.prompt, self.success_criterion)):
            raise ValueError("task fields are required")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")


def validate_manifest(tasks: Iterable[ResearchTask]) -> list[dict]:
    records, seen = [], set()
    for task in tasks:
        task.validate()
        if task.task_id in seen:
            raise ValueError(f"duplicate task id: {task.task_id}")
        seen.add(task.task_id)
        records.append(asdict(task))
    if not records:
        raise ValueError("task manifest must not be empty")
    return records

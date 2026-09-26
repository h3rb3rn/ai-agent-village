"""Small, dependency-free helpers for reproducible Village experiments."""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict
from typing import Any


@dataclass(frozen=True)
class ExperimentCondition:
    condition_id: str
    model_revision: str
    prompt_revision: str
    runtime_revision: str
    token_budget: int
    intervention: str = "none"

    def validate(self) -> None:
        if not self.condition_id.strip() or not self.model_revision.strip() or not self.prompt_revision.strip() or not self.runtime_revision.strip():
            raise ValueError("condition and revision identifiers are required")
        if self.token_budget <= 0:
            raise ValueError("token_budget must be positive")

    def to_record(self) -> dict[str, Any]:
        self.validate()
        return asdict(self)


def wilson_interval(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Return a conservative 95% Wilson interval with an explicit denominator."""
    if trials <= 0 or successes < 0 or successes > trials or z <= 0:
        raise ValueError("invalid successes/trials/z")
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)

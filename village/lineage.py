"""Reproducible lineage records and resource gates for child agents/models."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class LineageArtifact:
    artifact_id: str
    parent_id: str
    artifact_type: str
    revision: str
    dataset_ref: str
    evaluation_ref: str
    license_ref: str
    status: str = "proposed"

    def validate(self) -> None:
        required = (self.artifact_id, self.parent_id, self.artifact_type, self.revision, self.dataset_ref, self.evaluation_ref, self.license_ref)
        if any(not str(value).strip() for value in required): raise ValueError("lineage references are required")
        if self.status not in {"proposed", "evaluated", "adopted", "rejected", "retired"}: raise ValueError("invalid lineage status")

    def record(self) -> dict[str, Any]:
        self.validate(); return asdict(self)


def child_gate(*, lineage: LineageArtifact, gpu_budget_minutes: int, max_children: int, completed_prerequisites: set[str]) -> dict[str, Any]:
    lineage.validate(); required = {"P13", "P19", "P24", "P25", "P26"}; missing = sorted(required - set(completed_prerequisites))
    if missing: raise PermissionError(f"lineage gate prerequisites missing: {', '.join(missing)}")
    if gpu_budget_minutes <= 0 or max_children < 1: raise ValueError("positive GPU budget and child quota required")
    return {"artifact": lineage.record(), "action": "planned_only", "gpu_budget_minutes": gpu_budget_minutes, "max_children": max_children}

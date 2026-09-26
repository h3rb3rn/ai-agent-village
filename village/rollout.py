"""Fail-closed planning primitives for staged Village rollouts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CanaryPlan:
    agent_id: str
    release_revision: str
    operator_approved: bool = False
    village_paused: bool = True

    def validate(self) -> None:
        if not self.agent_id.strip() or not self.release_revision.strip():
            raise ValueError("agent_id and release_revision are required")
        if not self.village_paused:
            raise ValueError("rollout plan must be prepared while village is paused")
        if not self.operator_approved:
            raise PermissionError("explicit operator approval is required before rollout")


def prepare_canary(plan: CanaryPlan) -> dict[str, str]:
    plan.validate()
    return {"stage": "canary", "agent_id": plan.agent_id, "release_revision": plan.release_revision, "action": "planned_only"}


REQUIRED_CHECKPOINTS = ("message_ack", "task_progress", "artifact", "recovery", "memory", "peer_reproduction", "restart_resume")


def validate_checkpoints(checkpoints: Iterable[dict]) -> list[str]:
    names = []
    for checkpoint in checkpoints:
        name = str(checkpoint.get("name", ""))
        if name not in REQUIRED_CHECKPOINTS:
            raise ValueError(f"unknown checkpoint: {name}")
        if not checkpoint.get("request_id") or not checkpoint.get("run_id"):
            raise ValueError(f"checkpoint {name} lacks request_id/run_id")
        names.append(name)
    if tuple(names) != REQUIRED_CHECKPOINTS:
        raise ValueError("checkpoints must contain the required ordered scenario")
    return names


def validate_evidence_bundle(checkpoints: Iterable[dict]) -> dict[str, int]:
    """Validate synthetic checkpoint evidence without executing any action."""
    rows = list(checkpoints)
    validate_checkpoints(rows)
    missing = [row["name"] for row in rows if not row.get("evidence_ref")]
    if missing:
        raise ValueError(f"missing evidence references: {', '.join(missing)}")
    return {"checkpoints": len(rows), "evidence_references": len(rows), "execution": 0}

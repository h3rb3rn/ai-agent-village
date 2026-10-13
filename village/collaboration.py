"""Bounded cooperation checkpoints for research-oriented resident cycles.

The policy is intentionally process-focused: it does not reward a particular
answer and it never fabricates a peer response.  It only asks an agent to
orient in shared memory, consult a peer, and record an outcome before starting
another expensive or mutating step.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Any


CHECKPOINT_ACTIONS = {
    "orient": {"memory_search"},
    "consult": {"board_message"},
    "record": {"memory_remember"},
}


def _timestamp(row: Mapping[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(row.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return 0.0


@dataclass(frozen=True)
class CooperationCheckpoint:
    """A deterministic, inspectable next social/knowledge action."""

    stage: str
    required_action: str | None
    rationale: str
    peer_id: str | None = None
    pressure: int = 0
    hard: bool = False

    @property
    def satisfied(self) -> bool:
        return self.required_action is None

    def to_record(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "required_action": self.required_action,
            "rationale": self.rationale,
            "peer_id": self.peer_id,
            "pressure": self.pressure,
            "satisfied": self.satisfied,
            "enforcement": "immediate" if self.hard else "soft_until_three_misses",
        }


def assess(
    events: Iterable[Mapping[str, Any]],
    agent_id: str,
    *,
    has_active_task: bool = False,
    peer_id: str | None = None,
    now: float | None = None,
    window_seconds: int = 6 * 60 * 60,
    require_shared_record: bool = False,
) -> CooperationCheckpoint:
    """Assess the next checkpoint from observable local event history.

    A checkpoint is only considered during an active project.  Recent
    successful actions satisfy the corresponding stage; malformed output and
    unrelated peer traffic never do.  The result is advisory until the runtime
    has observed three consecutive misses, preventing deadlocks in small LLMs.
    """
    import time

    current = time.time() if now is None else float(now)
    recent = [
        row for row in events
        if row.get("agent") == agent_id and current - _timestamp(row) <= window_seconds
    ]
    if not has_active_task:
        return CooperationCheckpoint("idle", None, "No active project requires a checkpoint.")

    searches = [x for x in recent if x.get("event") == "memory_result" and "action=memory_search" in str(x.get("detail", "")) and "result=success" in str(x.get("detail", ""))]
    consultations = [
        x for x in recent
        if (x.get("event") == "board_message" and "to=ALL" not in str(x.get("detail", "")))
        or (x.get("event") == "direct_message" and "to=ALL" not in str(x.get("detail", "")))
    ]
    records = [
        x for x in recent
        if x.get("event") == "memory_result" and "action=memory_remember" in str(x.get("detail", "")) and "result=success" in str(x.get("detail", ""))
        and (not require_shared_record or "scope=shared" in str(x.get("detail", "")))
    ]
    work = [x for x in recent if x.get("event") in {"command_result", "job_finished", "research_result", "task_result"}]

    # A record after the latest piece of work closes the current checkpoint.
    latest_work = max((_timestamp(x) for x in work), default=0.0)
    latest_record = max((_timestamp(x) for x in records), default=0.0)
    if not searches:
        return CooperationCheckpoint("orient", "memory_search", "Search private/shared memory before repeating or extending the project.", hard=require_shared_record)
    if peer_id and not consultations:
        return CooperationCheckpoint("consult", "board_message", "Ask one named peer a concrete, reproducible question before proceeding.", peer_id=peer_id)
    if latest_work > latest_record:
        rationale = ("Share the measured result, failure, or reusable lesson with the community before the next "
                    "work step: memory_remember with scope=shared." if require_shared_record else
                    "Record the measured result, failure, or reusable lesson before the next work step.")
        return CooperationCheckpoint("record", "memory_remember", rationale, hard=require_shared_record)
    return CooperationCheckpoint("complete", None, "Knowledge and cooperation checkpoints are current.")


def is_checkpoint_action(checkpoint: CooperationCheckpoint, action: str) -> bool:
    """Return whether an action satisfies the currently requested checkpoint."""
    return checkpoint.required_action is None or action in CHECKPOINT_ACTIONS.get(checkpoint.stage, {checkpoint.required_action})



# P155 (live 2026-10-12/13): every addressed message became `direct_ack_target`, and the prompt
# asked to acknowledge it first. An acknowledgement is itself an addressed message, so two
# residents acknowledged each other forever (86% of 471 messages in three fixed pairs, 45%
# announcements, the same "recipe will follow" for 22 hours). A message that only
# acknowledges, thanks or announces - or repeats what the sender already said - asks nothing,
# so it no longer obliges a reply. It stays visible in the snapshot; only the obligation goes.
ACK_OPENING = re.compile(
    r"^\W*(?:[\w.-]+\s*[,:]\s*)?(?:acknowledged|acknowledge|ack\b|i acknowledge|i understand|i understood|understood|"
    r"thanks|thank you|received|noted|got it|i see|i'll|i will|will do|standing by|sounds good|confirmed|ok\b|okay)",
    re.I)
REPEAT_SIMILARITY = 0.7
_WORD = re.compile(r"[a-z0-9_./-]{3,}")


def _words(text: str) -> set:
    return set(_WORD.findall(text.lower()))


def similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def needs_no_reply(text: str, earlier: Iterable[str] = ()) -> bool:
    """True for a message that asks nothing: no question, and it either opens as an
    acknowledgement/announcement or repeats an earlier message of the same sender."""
    text = str(text or "")
    if "?" in text:
        return False
    if ACK_OPENING.match(text):
        return True
    return any(similarity(text, e) >= REPEAT_SIMILARITY for e in earlier)

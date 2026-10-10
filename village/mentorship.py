"""P119: small mentor pairs. A learner who holds an active task gets a mentor note, and the
mentor is asked for one concrete step. Measured by completed tasks per learner.

The pairs were chosen from completion evidence (task_revisions, action=complete, 2026-10-10):
06-operator 25, 02-explorer 24, 07-methodologist 7; learners with few completions are the targets.
"""
from __future__ import annotations

from typing import Dict, List, Optional

MENTOR_PAIRS: Dict[str, str] = {
    "04-artisan": "06-operator",
    "08-logician": "07-methodologist",
}


def mentor_for(learner: str) -> Optional[str]:
    return MENTOR_PAIRS.get(learner)


def mentor_note(learner: str, task_id: str, title: str) -> str:
    return (f"Mentoring: your learner {learner} holds task {task_id} ('{title}'). "
            "Send them one concrete next step in a direct message (board_message with recipient set to "
            "their exact ID). Keep it to two sentences and something they can check.")


def completions(revisions: List[dict], learner: str, since: str = "") -> int:
    """Completed-task revisions by one resident since a timestamp (ISO prefix)."""
    return sum(1 for r in revisions
               if r.get("actor") == learner and r.get("action") == "complete"
               and str(r.get("timestamp", "")) >= since)

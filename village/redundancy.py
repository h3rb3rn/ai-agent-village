"""P103: redundancy and efficiency measures from the board event log.

Pure functions over already-recorded events, so the same numbers can be produced for
any window and compared before and after a change (P99-P102). Nothing here writes to
the village; the report is read-only.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable


def report(events: Iterable[Dict[str, Any]], since: str = "") -> Dict[str, Any]:
    """Summarise redundancy for events at or after `since` (ISO timestamp prefix)."""
    meeting_total = 0
    meeting_pairs = set()
    meeting_per_agent: Counter = Counter()
    dm_total = 0
    dm_per_pair: Counter = Counter()
    rounds: set = set()
    budget_exhausted = 0
    invalid_total = 0
    dm_blocked = 0
    for e in events:
        ts = str(e.get("timestamp", ""))
        if since and ts < since:
            continue
        ev = e.get("event", "")
        agent = e.get("agent", "")
        detail = str(e.get("detail", ""))
        if ev == "meeting_result" and detail.startswith("meeting_id="):
            meeting_total += 1
            meeting_id = detail.split(";")[0].split("=", 1)[1]
            meeting_pairs.add((meeting_id, agent))
            meeting_per_agent[agent] += 1
        elif ev == "meeting_opened":
            rounds.add(detail[:60] or ts[:10])
        elif ev == "direct_message":
            dm_total += 1
            to = detail.split(";")[0].replace("to=", "", 1)
            dm_per_pair[(agent, to)] += 1
        elif ev == "dm_duplicate_blocked":
            dm_blocked += 1
        elif ev == "invalid_decision":
            invalid_total += 1
            if "output budget" in detail:
                budget_exhausted += 1
    repeat_share = 0.0 if not meeting_total else round(1 - len(meeting_pairs) / meeting_total, 3)
    return {
        "meeting_reports": meeting_total,
        "distinct_meeting_reports": len(meeting_pairs),
        "repeated_report_share": repeat_share,
        "meeting_reports_per_agent": dict(meeting_per_agent.most_common()),
        "direct_messages": dm_total,
        "direct_messages_per_pair_max": max(dm_per_pair.values()) if dm_per_pair else 0,
        "duplicate_messages_blocked": dm_blocked,
        "meeting_rounds_opened": len(rounds),
        "invalid_decisions": invalid_total,
        "budget_exhausted_share": round(budget_exhausted / invalid_total, 3) if invalid_total else 0.0,
    }

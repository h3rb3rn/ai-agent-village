"""Read-only quality metrics computed from the public activity and inference event lists.

Results are for the operator only. They must never be written to the Board, prompts,
memory or any agent-visible state (no observation feedback loop).
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List


def _ratio(num: float, den: float):
    return round(num / den, 3) if den else None


def _prompt_tokens(row: Dict[str, Any]):
    match = re.search(r"prompt_eval_count\"?:\s*(\d+)", str(row.get("detail", "")))
    return int(match.group(1)) if match else None


def compute(activity: Iterable[Dict[str, Any]], inference: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    activity, inference = list(activity), list(inference)
    started = [r for r in inference if r.get("event") == "inference_started"]
    finished = [r for r in inference if r.get("event") == "inference_finished"]
    wakeups = [r for r in inference if r.get("event") == "event_wakeup"]
    exceptions = [r for r in inference if r.get("event") == "runtime_exception"]
    n = len(started)
    kinds = Counter(r.get("event") for r in activity)
    board = [r for r in activity if r.get("event") == "board_message"]
    directs = [r for r in activity if r.get("event") == "direct_message"]
    directed_board = [r for r in board if "to=ALL" not in str(r.get("detail", ""))]
    messages = len(board) + len(directs)
    direct_total = len(directs) + len(directed_board)
    speakers = {r.get("agent") for r in board + directs if r.get("agent")}
    memory_writers = {r.get("agent") for r in activity
                      if (r.get("event") == "memory_result" and "action=memory_remember" in str(r.get("detail", "")) and "result=success" in str(r.get("detail", "")))
                      or r.get("event") == "memory_auto"}
    verified = [r for r in activity if r.get("event") == "artifact_operation"
                and re.search(r"op=verify;.*status=(reproduced|verified|adopted)", str(r.get("detail", "")))]
    prompt_tokens = [t for t in (_prompt_tokens(r) for r in finished) if t]
    per_agent: Dict[str, Dict[str, int]] = defaultdict(lambda: {"inferences": 0, "invalid": 0})
    for r in started: per_agent[r.get("agent")]["inferences"] += 1
    for r in activity:
        if r.get("event") == "invalid_decision": per_agent[r.get("agent")]["invalid"] += 1
    return {
        "inferences": n,
        "invalid_decision_per_inference": _ratio(kinds["invalid_decision"], n),
        "wakeups_per_inference": _ratio(len(wakeups), n),
        "runtime_exceptions": len(exceptions),
        "board_messages": len(board), "direct_messages": direct_total,
        "direct_message_ratio": _ratio(direct_total, messages),
        "agents_with_message": len(speakers),
        "agents_with_memory_write": len(memory_writers),
        "verified_artifacts": len(verified),
        "task_results": kinds["task_result"],
        "repeated_action_blocks": kinds["escalation"],
        "collaboration_nudges": kinds["collaboration_nudge"], "collaboration_gates": kinds["collaboration_gate"],
        "median_prompt_tokens": int(statistics.median(prompt_tokens)) if prompt_tokens else None,
        "per_agent": {k: dict(v) for k, v in sorted(per_agent.items(), key=lambda kv: str(kv[0]))},
    }


def load(source: str) -> List[Dict[str, Any]]:
    """Load a JSON list from a file path or an http(s) URL."""
    if source.startswith(("http://", "https://")):
        import urllib.request
        with urllib.request.urlopen(source, timeout=15) as response:
            return json.load(response)
    with open(source, encoding="utf-8") as handle:
        return json.load(handle)

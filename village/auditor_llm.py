"""Open-ended review layer for the auditor, using an independent, much larger
model (qwen3.6:35b on N11-M10, 192.168.155.231:11434) that is not one of the
nine resident models being audited - so it never judges itself.

Unlike village/auditor.py's deterministic signatures (fixed catalogue, zero
hallucination risk, but blind to anything not explicitly coded), this can spot
open-ended, semantic mistakes - e.g. a circular, non-diagnostic board message -
that no regex over event text could recognize. That openness is also the
risk: a 35B model can still misjudge. Two safeguards, on top of what
village/auditor.py already enforces for every finding regardless of origin:
  1. A minimum confidence threshold (self-reported by the judge model, not
     independently verified - treated as a coarse filter, not proof).
  2. Every LLM finding still goes through the SAME AuditStore.route(): a
     single low-confidence misjudgement stays private to one agent; it can
     only become shared community knowledge if the same category recurs for a
     second, different agent - exactly like a deterministic finding.

Confirmed operating parameters (2026-09-27, real calls against the live
endpoint, not assumed): num_ctx=190000 (operator-fixed final value), think=false (thinking otherwise
consumes the entire output budget before any answer, mirroring the
Methodologist's own budget-exhaustion failure mode), keep_alive=96h (the
operator's proven value - a fresh load on this M10 multi-GPU host takes
60-150s depending on context size, so the model must stay resident between
audit cycles, not reload each time). Once warm, one judged call took ~27-40s.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from village.auditor import AuditFinding

DEFAULT_URL = "http://192.168.155.231:11434"
DEFAULT_MODEL = "qwen3.6:35b"
NUM_CTX = 190000  # operator-fixed final value, do not change again
KEEP_ALIVE = "96h"
DEFAULT_TIMEOUT_SECONDS = 300  # generation alone measured at ~20-40s; large margin for a cold/slow cycle
CONFIDENCE_THRESHOLD = 0.7
MAX_CANDIDATES_PER_CYCLE = 5  # bounded: ~30-40s each, serialized, must fit well inside the cycle interval

SYSTEM_PROMPT = (
    "You are a strict, evidence-only reviewer of a small local LLM resident agent's "
    "output in a multi-agent research system. Judge only from the given text. Never "
    "invent facts not present in it. Respond with the required JSON only."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_issue": {"type": "boolean"},
        "category": {"type": "string", "maxLength": 60},
        "problem": {"type": "string", "maxLength": 600},
        "solution": {"type": "string", "maxLength": 600},
        "confidence": {"type": "number"},
    },
    "required": ["has_issue", "category", "problem", "solution", "confidence"],
}


class JudgeError(Exception):
    pass


@dataclass(frozen=True)
class ReviewCandidate:
    """One piece of already-recorded resident output worth a second opinion."""

    agent: str
    model: str
    source_event_id: str
    kind: str  # short label for the prompt, e.g. "rejected output" or "board message"
    text: str


def select_candidates(events: Iterable[Mapping[str, Any]], *, already_classified: Iterable[str] = (),
                      known_format_reasons: frozenset = frozenset(), limit: int = MAX_CANDIDATES_PER_CYCLE
                      ) -> List[ReviewCandidate]:
    """Pick a bounded set of events the deterministic signatures did not already
    explain, worth spending a slow LLM call on. Never re-reviews an event id the
    caller already processed (by either layer)."""
    seen = set(already_classified)
    candidates: List[ReviewCandidate] = []
    for e in events:
        if len(candidates) >= limit:
            break
        event_id = str(e.get("event_id", ""))
        if not event_id or event_id in seen:
            continue
        kind = e.get("event")
        detail = str(e.get("detail", ""))
        if kind == "invalid_decision_detail":
            reason = detail.split(";", 1)[0].removeprefix("reason=").strip()
            if reason in known_format_reasons:
                continue  # village/auditor.py's format_violation signature already explains this
            preview = detail.split("preview=", 1)[-1] if "preview=" in detail else detail
            candidates.append(ReviewCandidate(str(e.get("agent", "")), str(e.get("model", "")),
                                              event_id, "a rejected response", preview[:1500]))
        elif kind == "board_message":
            message = detail.split("message=", 1)[-1] if "message=" in detail else detail
            candidates.append(ReviewCandidate(str(e.get("agent", "")), str(e.get("model", "")),
                                              event_id, "a public board message", message[:1500]))
    return candidates


def build_request(candidate: ReviewCandidate, *, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
                  num_predict: int = 500) -> urllib.request.Request:
    endpoint = url.rstrip("/") + "/api/chat"
    user_prompt = (
        f"Agent {candidate.agent} (model {candidate.model}) produced {candidate.kind}:\n\n"
        f'"""{candidate.text}"""\n\n'
        "Identify the concrete mistake and the correct fix. If there is no real mistake, "
        "set has_issue to false. Set confidence 0-1 for how sure you are."
    )
    payload = {
        "model": model, "stream": False, "keep_alive": KEEP_ALIVE, "think": False,
        "format": RESPONSE_SCHEMA,
        "options": {"num_ctx": NUM_CTX, "num_predict": num_predict, "temperature": 0.1},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    return urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"),
                                  headers={"Content-Type": "application/json"})


def _parse(raw: Dict[str, Any]) -> Dict[str, Any]:
    if raw.get("done_reason") == "length":
        raise JudgeError("output budget exhausted before an answer (check think=false took effect)")
    content = (raw.get("message") or {}).get("content", "")
    try:
        verdict = json.loads(content)
    except (TypeError, ValueError) as exc:
        raise JudgeError(f"judge did not return valid JSON: {exc}") from exc
    for field in ("has_issue", "category", "problem", "solution", "confidence"):
        if field not in verdict:
            raise JudgeError(f"judge response missing required field: {field}")
    return verdict


def review(candidate: ReviewCandidate, *, opener: Callable = urllib.request.urlopen,
          url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
          confidence_threshold: float = CONFIDENCE_THRESHOLD,
          timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Optional[AuditFinding]:
    """Ask the judge model about one candidate; return a finding only above the
    confidence threshold. Never raises for an ordinary "no issue" verdict; raises
    JudgeError only for a genuinely broken exchange (network, parsing, budget)."""
    request = build_request(candidate, url=url, model=model)
    try:
        with opener(request, timeout=timeout) as response:
            raw = json.load(response)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise JudgeError(f"judge request failed: {exc}") from exc
    verdict = _parse(raw)
    if not verdict["has_issue"]:
        return None
    try:
        confidence = float(verdict["confidence"])
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < confidence_threshold:
        return None
    category = f"llm_review:{str(verdict['category']).strip().lower().replace(' ', '_')[:40]}"
    return AuditFinding(
        category=category, agent=candidate.agent, model=candidate.model,
        source_event_id=candidate.source_event_id,
        problem=str(verdict["problem"])[:600], solution=str(verdict["solution"])[:600],
        rejected_example=candidate.text[:1500], corrected_example=str(verdict["solution"])[:600],
    )

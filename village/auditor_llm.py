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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from village.auditor import FORMAT_REASONS, AuditFinding, AuditStore, deliver, scan

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
    "invent facts not present in it. Give ONE final verdict immediately, do not "
    "deliberate or second-guess yourself in the output. If nothing is clearly wrong, "
    "set has_issue to false right away. Keep problem and solution to one short "
    "sentence each. Respond with the required JSON only."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "has_issue": {"type": "boolean"},
        "category": {"type": "string", "maxLength": 60},
        "problem": {"type": "string", "maxLength": 300},
        "solution": {"type": "string", "maxLength": 300},
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


def _retrieve_context(memory_reader: Optional[Callable[[str], List[Dict[str, Any]]]],
                      query: str) -> str:
    """Best-effort retrieval from the village's shared knowledgebase to give the
    judge more than its frozen weights - e.g. a corrected pattern the village
    already learned. A search failure (offline gateway, timeout, bad response)
    must never abort the review; it just proceeds without extra context."""
    if memory_reader is None or not query.strip():
        return ""
    try:
        hits = memory_reader(query) or []
    except Exception:
        return ""
    snippets = [str(h.get("content", "")).strip()[:300] for h in hits[:3] if h.get("content")]
    if not snippets:
        return ""
    bullet_list = "\n".join(f"- {s}" for s in snippets)
    return (
        "\n\nRelevant existing entries from the village's shared knowledgebase "
        "(context only - it may be incomplete, outdated or unrelated; weigh it, "
        "do not treat it as automatically correct):\n" + bullet_list
    )


def build_request(candidate: ReviewCandidate, *, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL,
                  num_predict: int = 500,
                  memory_reader: Optional[Callable[[str], List[Dict[str, Any]]]] = None) -> urllib.request.Request:
    endpoint = url.rstrip("/") + "/api/chat"
    context_block = _retrieve_context(memory_reader, candidate.text[:500])
    user_prompt = (
        f"Agent {candidate.agent} (model {candidate.model}) produced {candidate.kind}:\n\n"
        f'"""{candidate.text}"""\n\n'
        "Identify the concrete mistake and the correct fix. If there is no real mistake, "
        "set has_issue to false. Set confidence 0-1 for how sure you are."
        f"{context_block}"
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
          timeout: int = DEFAULT_TIMEOUT_SECONDS,
          memory_reader: Optional[Callable[[str], List[Dict[str, Any]]]] = None) -> Optional[AuditFinding]:
    """Ask the judge model about one candidate; return a finding only above the
    confidence threshold. Never raises for an ordinary "no issue" verdict; raises
    JudgeError only for a genuinely broken exchange (network, parsing, budget).

    ``memory_reader``, if given, augments the judge's frozen weights with a
    lexical/semantic lookup against the village's own shared knowledgebase
    (e.g. a correction pattern the village already learned) - not fine-tuning,
    just retrieval-augmented context for this one judgement."""
    request = build_request(candidate, url=url, model=model, memory_reader=memory_reader)
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


# --- combined cycle: deterministic first, LLM only for what it could not explain

def full_audit_cycle(events: List[Mapping[str, Any]], store: AuditStore, *, coordination_store,
                     memory_writer: Callable[[Dict[str, Any]], Any],
                     memory_reader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
                     opener: Callable = urllib.request.urlopen, url: str = DEFAULT_URL,
                     model: str = DEFAULT_MODEL, llm_enabled: bool = True) -> Dict[str, int]:
    """Run the primary, free, deterministic signatures first; only spend a slow,
    costly qwen3.6:35b call on events that catalogue could not explain, and never
    on a format_violation reason the deterministic layer already owns. Every
    finding - deterministic or LLM - is routed and rate-limited identically via
    the same AuditStore, so a single low-confidence LLM misjudgement can never
    reach shared community knowledge on its own.
    """
    stats = {"deterministic_findings": 0, "deterministic_delivered": 0,
             "llm_candidates": 0, "llm_findings": 0, "llm_delivered": 0, "llm_errors": 0}

    deterministic_findings = scan(events)
    stats["deterministic_findings"] = len(deterministic_findings)
    claimed_event_ids = set()
    for finding in deterministic_findings:
        claimed_event_ids.add(finding.source_event_id)
        scope = store.route(finding, source="deterministic")
        if scope is None:
            continue
        deliver(finding, scope, coordination_store=coordination_store, memory_writer=memory_writer)
        store.mark_delivered(finding.category, finding.source_event_id)
        stats["deterministic_delivered"] += 1

    if not llm_enabled:
        store.record_cycle(stats)
        return stats

    candidates = select_candidates(events, already_classified=claimed_event_ids,
                                   known_format_reasons=FORMAT_REASONS)
    stats["llm_candidates"] = len(candidates)
    for candidate in candidates:
        try:
            finding = review(candidate, opener=opener, url=url, model=model, memory_reader=memory_reader)
        except JudgeError:
            stats["llm_errors"] += 1
            continue
        if finding is None:
            continue
        stats["llm_findings"] += 1
        scope = store.route(finding, source="llm")
        if scope is None:
            continue
        deliver(finding, scope, coordination_store=coordination_store, memory_writer=memory_writer)
        store.mark_delivered(finding.category, finding.source_event_id)
        stats["llm_delivered"] += 1
    store.record_cycle(stats)
    return stats


# --- periodic service loop, mirroring village/firewatch.py::run() -----------

def run(interval: float = 300.0,
        agent_events: Optional[Path] = None,
        db_path: Optional[Path] = None,
        *, coordination_store, memory_writer,
        memory_reader: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
        opener: Callable = urllib.request.urlopen, url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL, llm_enabled: bool = True) -> None:
    """Run full_audit_cycle() forever against newly-appended resident events.

    Reads the same agent-events.jsonl the runtime and village/firewatch.py
    already write to; a persisted cursor (AuditStore.cursor/advance_cursor)
    ensures each event is only ever considered once, even across restarts.
    Never restarts or throttles an agent itself - it only explains and routes.
    """
    from village.firewatch import _read_events, AGENT_EVENTS as _DEFAULT_AGENT_EVENTS

    agent_events = agent_events or _DEFAULT_AGENT_EVENTS
    db_path = db_path or Path("/var/lib/ai-village/telemetry/audit.sqlite3")
    store = AuditStore(db_path)
    while True:
        events = _read_events(agent_events)
        cursor = store.cursor()
        new_events = [e for e in events if str(e.get("timestamp", "")) > cursor] if cursor else events
        if new_events:
            full_audit_cycle(new_events, store, coordination_store=coordination_store,
                             memory_writer=memory_writer, memory_reader=memory_reader,
                             opener=opener, url=url, model=model, llm_enabled=llm_enabled)
            store.advance_cursor(max(str(e.get("timestamp", "")) for e in new_events))
        time.sleep(max(5.0, interval))

"""Deterministic error auditor: detects known resident mistakes from recorded
events, explains the problem and the correct approach, and routes the finding
either to the mistaken agent privately or into shared community knowledge.

No LLM call, no network beyond the existing memory gateway, no fabrication:
every finding is derived from an event the runtime already recorded (a block,
an escalation, a rejected envelope), never a guess about intent. Relevance is
decided structurally: a mistake category seen from two or more DIFFERENT
agents is a community pattern (scope=shared); one seen from a single agent
stays private to them (scope=private, delivered as a direct inbox message).

Every finding is also persisted as a structured (rejected, corrected) example
in a dedicated SQLite log, independent of the in-village delivery. That table
is the intended source for later supervised/preference fine-tuning export
(see scripts/export-finetune-dataset.py) - the pedagogical text an agent reads
now and the machine-readable training pair are two views of the same fact,
not duplicated authoring.
"""
from __future__ import annotations

import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

SCHEMA_VERSION = 1
RATE_LIMIT_SECONDS = 3600  # at most one correction per (category, agent) per hour


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AuditFinding:
    """One detected, evidence-backed mistake, ready to explain and to log."""

    category: str
    agent: str
    source_event_id: str
    problem: str
    solution: str
    rejected_example: str = ""
    corrected_example: str = ""
    model: str = ""

    def to_record(self) -> Dict[str, Any]:
        return {
            "category": self.category, "agent": self.agent, "model": self.model,
            "source_event_id": self.source_event_id, "problem": self.problem,
            "solution": self.solution, "rejected_example": self.rejected_example[:2000],
            "corrected_example": self.corrected_example[:2000],
        }


# --- detection: pure functions over already-recorded events, no I/O ---------

_FOREIGN_HOME_RE = re.compile(r"target_agent=([^;]+);\s*command_prefix=(.*)$")
_ESCALATION_RE = re.compile(r"repeated action blocked:\s*(.+)$")
_FORMAT_REASONS = {
    "multiple action blocks", "incomplete village-action block",
    "incomplete legacy action object", "unknown action or malformed arguments",
    "missing action argument",
}
_FORMAT_DETAIL_RE = re.compile(r"reason=(.+?);\s*preview=(.*)$", re.S)

VALID_ENVELOPE_EXAMPLE = '{"name":"board_message","arguments":{"message":"your text","recipient":"exact-peer-id or ALL"}}'


def _agent_model(event: Mapping[str, Any]) -> str:
    return str(event.get("model") or "")


def detect_foreign_home_access(events: Iterable[Mapping[str, Any]]) -> List[AuditFinding]:
    findings = []
    for e in events:
        if e.get("event") != "foreign_home_blocked":
            continue
        match = _FOREIGN_HOME_RE.search(str(e.get("detail", "")))
        if not match:
            continue
        target, command = match.group(1).strip(), match.group(2).strip()
        agent = str(e.get("agent", ""))
        findings.append(AuditFinding(
            category="foreign_home_access", agent=agent, model=_agent_model(e),
            source_event_id=str(e.get("event_id", "")),
            problem=(f"You ran a command targeting {target}'s private home directory. "
                     "Every resident's home is a separate 0700 Unix account: no one can read, "
                     "write, or execute anything there except that resident, even to help."),
            solution=(f"Ask {target} directly to run the steps themselves in their own account, "
                      "or ask them to state the general recipe. Never address a peer's private path "
                      "from your own account."),
            rejected_example=command,
            corrected_example=f"board_message to {target}: \"Can you run this in your own account and tell me the result?\"",
        ))
    return findings


def detect_repeated_action(events: Iterable[Mapping[str, Any]]) -> List[AuditFinding]:
    findings = []
    for e in events:
        if e.get("event") != "escalation":
            continue
        match = _ESCALATION_RE.search(str(e.get("detail", "")))
        action = match.group(1).strip() if match else "an action"
        agent = str(e.get("agent", ""))
        findings.append(AuditFinding(
            category="repeated_action", agent=agent, model=_agent_model(e),
            source_event_id=str(e.get("event_id", "")),
            problem=(f"You repeated {action} with the same arguments after it was already blocked, "
                     "without a new observation justifying another attempt."),
            solution=("Read the previous result first. Change one concrete thing - a different "
                      "argument, a named peer question, or a different action entirely - instead "
                      "of retrying the identical call."),
            rejected_example=action,
            corrected_example="Read last_action_feedback, then choose a different action or different arguments.",
        ))
    return findings


def detect_format_violation(events: Iterable[Mapping[str, Any]]) -> List[AuditFinding]:
    findings = []
    for e in events:
        if e.get("event") != "invalid_decision_detail":
            continue
        match = _FORMAT_DETAIL_RE.search(str(e.get("detail", "")))
        if not match:
            continue
        reason, preview = match.group(1).strip(), match.group(2).strip()
        if reason not in _FORMAT_REASONS:
            continue
        agent = str(e.get("agent", ""))
        findings.append(AuditFinding(
            category="format_violation", agent=agent, model=_agent_model(e),
            source_event_id=str(e.get("event_id", "")),
            problem=f"Your response was rejected ({reason}): it did not parse into exactly one action.",
            solution=("End your answer with exactly one JSON object naming one action and its "
                      "arguments. Never emit two objects, an unclosed block, or prose mixed into "
                      f"the JSON. Minimal valid shape: {VALID_ENVELOPE_EXAMPLE}"),
            rejected_example=preview[:1500],
            corrected_example=VALID_ENVELOPE_EXAMPLE,
        ))
    return findings


SIGNATURES: Dict[str, Callable[[Iterable[Mapping[str, Any]]], List[AuditFinding]]] = {
    "foreign_home_access": detect_foreign_home_access,
    "repeated_action": detect_repeated_action,
    "format_violation": detect_format_violation,
}


def scan(events: Iterable[Mapping[str, Any]]) -> List[AuditFinding]:
    """Run every known signature over the same event batch."""
    events = list(events)
    findings: List[AuditFinding] = []
    for detector in SIGNATURES.values():
        findings.extend(detector(events))
    return findings


# --- persistence, relevance routing and rate limiting -----------------------

class AuditStore:
    """Structured, durable log of findings plus the community/private routing state.

    The same table serves two purposes: the in-village delivery decision (has
    this category been seen from more than one agent?) and, independently, a
    ready-made (rejected, corrected) example for later fine-tuning export.
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS audit_log (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, category TEXT NOT NULL,
                    agent TEXT NOT NULL, model TEXT, source_event_id TEXT NOT NULL,
                    scope TEXT NOT NULL, problem TEXT NOT NULL, solution TEXT NOT NULL,
                    rejected_example TEXT, corrected_example TEXT, delivered INTEGER NOT NULL DEFAULT 0
                )"""
            )
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_source ON audit_log(source_event_id, category)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_category_agent ON audit_log(category, agent)")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS audit_cursor (
                    id INTEGER PRIMARY KEY CHECK (id = 1), last_timestamp TEXT NOT NULL
                )"""
            )

    def cursor(self) -> str:
        with self._conn() as conn:
            row = conn.execute("SELECT last_timestamp FROM audit_cursor WHERE id=1").fetchone()
            return row["last_timestamp"] if row else ""

    def advance_cursor(self, timestamp: str) -> None:
        if not timestamp:
            return
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO audit_cursor(id, last_timestamp) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET last_timestamp=excluded.last_timestamp "
                "WHERE excluded.last_timestamp > audit_cursor.last_timestamp",
                (timestamp,),
            )

    def known_agents_for(self, category: str) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute("SELECT DISTINCT agent FROM audit_log WHERE category=?", (category,)).fetchall()
            return [r["agent"] for r in rows]

    def last_written_at(self, category: str, agent: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT created_at FROM audit_log WHERE category=? AND agent=? ORDER BY created_at DESC LIMIT 1",
                (category, agent),
            ).fetchone()
            return row["created_at"] if row else None

    def route(self, finding: AuditFinding, *, now: Optional[str] = None) -> Optional[str]:
        """Decide scope for a finding and persist it; return the scope, or None if
        rate-limited (already corrected this agent for this category recently)."""
        now = now or utc_now()
        last = self.last_written_at(finding.category, finding.agent)
        if last and _seconds_between(last, now) < RATE_LIMIT_SECONDS:
            return None
        known = set(self.known_agents_for(finding.category)) | {finding.agent}
        scope = "shared" if len(known) >= 2 else "private"
        record_id = str(uuid.uuid4())
        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO audit_log(
                        id, created_at, category, agent, model, source_event_id, scope,
                        problem, solution, rejected_example, corrected_example, delivered
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                    (record_id, now, finding.category, finding.agent, finding.model,
                     finding.source_event_id, scope, finding.problem, finding.solution,
                     finding.rejected_example, finding.corrected_example),
                )
        except sqlite3.IntegrityError:
            return None  # this exact (source_event_id, category) was already audited
        return scope

    def mark_delivered(self, category: str, source_event_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE audit_log SET delivered=1 WHERE category=? AND source_event_id=?",
                (category, source_event_id),
            )

    def export_finetune_pairs(self) -> List[Dict[str, Any]]:
        """All findings with both a rejected and a corrected example, in a
        provider-neutral (rejected, corrected) preference-pair shape."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM audit_log WHERE rejected_example != '' AND corrected_example != '' ORDER BY created_at"
            ).fetchall()
            return [
                {
                    "category": r["category"], "agent": r["agent"], "model": r["model"],
                    "context": r["problem"], "rejected": r["rejected_example"],
                    "chosen": r["corrected_example"], "created_at": r["created_at"],
                }
                for r in rows
            ]


def _seconds_between(a: str, b: str) -> float:
    try:
        return (datetime.fromisoformat(b.replace("Z", "+00:00")) - datetime.fromisoformat(a.replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


# --- delivery: wires findings into the existing inbox / memory-gateway paths -

def deliver(finding: "AuditFinding", scope: str, *, coordination_store, memory_writer) -> None:
    """Deliver one routed finding.

    Private: a direct inbox message from the system sender 'village-auditor' to
    the mistaken agent (reuses the existing CoordinationStore inbox - the same
    path a peer consultation uses, so it surfaces as an unread direct message
    on the agent's next cycle).
    Shared: a memory-gateway write with scope=shared, kind='correction' (reuses
    the existing memory search path, becomes community knowledge for everyone).

    `memory_writer` is a callable(payload: dict) -> dict, e.g. a bound HTTP call
    to POST /v1/memories with the auditor's own credential; kept injectable so
    detection/routing stays testable without a live gateway.
    """
    text = (f"[village-auditor] {finding.problem} {finding.solution}").strip()
    if scope == "private":
        coordination_store.post_inbox_message(
            source="direct", sender="village-auditor", recipient=finding.agent,
            content=text[:4000],
        )
    else:
        memory_writer({
            "agent": "village-auditor", "scope": "shared", "kind": "correction",
            "content": text[:4000], "source_event": f"audit:{finding.category}:{finding.source_event_id}",
            "confidence": 1.0,
        })


def audit_cycle(events, store: "AuditStore", *, coordination_store, memory_writer) -> int:
    """Scan, route and deliver every new finding once; returns the count delivered."""
    delivered = 0
    for finding in scan(events):
        scope = store.route(finding)
        if scope is None:
            continue
        deliver(finding, scope, coordination_store=coordination_store, memory_writer=memory_writer)
        store.mark_delivered(finding.category, finding.source_event_id)
        delivered += 1
    return delivered

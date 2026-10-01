"""AI Village self-improvement: fine-tuning governance (P76).

Operator directive (2026-09-30): "Ein Primaeres Goal eines Jedes Agents
soll die eigene Verbesserung sein. Agents sollen die Moeglichkeit haben
Ihr eigenes Base LLM finezutunen und spaeter gegen das derzeit verwendete
LLM auszutauschen. Ressourcen muessen aber beruecksichtigt werden. Bis auf
King hat jeder Agent eine 8 GB Nvidia Tesla M60 GPU. Zum Finetuning stehen
die lokalen M10 GPUs zur Verfuegung aus der gleichen Generation."

Scope, deliberately: this module owns the GOVERNANCE layer only - who is
running/evaluating/requesting what, and which of the four local M10 GPUs
(confirmed idle and reachable by every resident's own Unix user, see
docs/evidence/P76.md) is claimed by whom. It does not execute training
itself - a resident starts its own real job via the existing
start_job/job_status/cancel_job actions (already the general mechanism
for any long-running background work) and records progress here. Most
importantly, this module never writes the host .env or touches a live
OLLAMA_AGENT_*_MODEL setting: AGENTS.md is explicit that model/GPU
settings need explicit operator authorization, so the furthest an
in-village action can go is a King-endorsed swap *request* - applying it
stays a deliberate, human, off-band operation with the same
backup/consistency-check/restart discipline already used for every
earlier model change this session (see docs/evidence/P72.md's Nachtrag).
"""
from __future__ import annotations
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Confirmed via nvidia-smi -L on N06-M10 (P76): 4 local Tesla M10 GPUs
# (index 0-3), same generation as the M60s each resident's inference
# already runs on, currently idle and reachable by every resident's own
# Unix user (video/render/ai-village-gpu group membership already in
# place - no further host provisioning needed for THIS package).
GPU_COUNT = 4

RUN_STATUSES = ("proposed", "running", "completed", "failed", "abandoned")
SWAP_STATUSES = ("pending", "king_approved", "rejected", "applied")
REVIEW_AGENT = "01-king"

# P90 (operator-chosen Gazette game prize: "GPU-/Finetuning-Vorrang"):
# the winner of the daily Gazette game gets first refusal on one
# currently-IDLE M10 GPU for this many hours - reserve_gpu_priority()
# below only ever reserves capacity nobody is using at the moment it is
# granted, so this can never preempt or interrupt a peer's running job.
GPU_PRIORITY_RESERVATION_HOURS = 24.0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FinetuneStore:
    """SQLite-backed fine-tune run/evaluation/swap-request tracker,
    reusing coordination.sqlite3 like every other store."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS finetune_runs(
                id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, base_model TEXT NOT NULL,
                method TEXT NOT NULL, dataset_description TEXT NOT NULL, gpu_index INTEGER,
                status TEXT NOT NULL DEFAULT 'proposed', job_reference TEXT, output_path TEXT,
                notes TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                completed_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS finetune_evaluations(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES finetune_runs(id) ON DELETE CASCADE,
                evaluator TEXT NOT NULL, metric_name TEXT NOT NULL, metric_value REAL NOT NULL,
                baseline_value REAL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS finetune_swap_requests(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES finetune_runs(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL, requested_by TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                reviewed_by TEXT, reviewed_at TEXT, review_note TEXT, requested_at TEXT NOT NULL)""")
            # One row per GPU index - present/absent-of-claimant IS the
            # claim state, simpler than a separate boolean plus stale-row
            # cleanup. Pre-seeded so claim_gpu() only ever UPDATEs.
            c.execute("""CREATE TABLE IF NOT EXISTS finetune_gpu_claims(
                gpu_index INTEGER PRIMARY KEY, claimed_by TEXT, run_id TEXT, claimed_at TEXT)""")
            for i in range(GPU_COUNT):
                c.execute("INSERT OR IGNORE INTO finetune_gpu_claims(gpu_index) VALUES(?)", (i,))
            # P90: a reservation only ever narrows which FREE index a
            # non-holder's claim_gpu() may land on - it is deleted the
            # moment its GPU is actually claimed (by anyone) or once it
            # expires, and never touches an already-claimed GPU, so it
            # carries zero risk to any running job.
            c.execute("""CREATE TABLE IF NOT EXISTS finetune_gpu_reservations(
                gpu_index INTEGER PRIMARY KEY, reserved_for TEXT NOT NULL,
                reserved_at TEXT NOT NULL, expires_at TEXT NOT NULL)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_finetune_runs_agent ON finetune_runs(agent_id, status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_finetune_swap_status ON finetune_swap_requests(status)")
            c.commit()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _purge_expired_reservations(self, c) -> None:
        c.execute("DELETE FROM finetune_gpu_reservations WHERE expires_at < ?", (now(),))

    def claim_gpu(self, agent: str, run_id: str, preferred_index: Optional[int] = None) -> int:
        """Claims one free M10 GPU index for this run, preferring
        preferred_index if it is actually free. Raises if none are free -
        the resident is expected to wait/check back, never to guess an
        already-claimed index and collide with a peer's job.

        P90: a free index currently reserved (see reserve_gpu_priority())
        for a DIFFERENT agent is skipped - the caller may still claim any
        OTHER free GPU, it just cannot jump the reservation. A reservation
        for the calling agent itself, or an expired one, is no obstacle."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._purge_expired_reservations(c)
            free = [r["gpu_index"] for r in c.execute(
                "SELECT gpu_index FROM finetune_gpu_claims WHERE claimed_by IS NULL ORDER BY gpu_index"
            ).fetchall()]
            reserved_for = {r["gpu_index"]: r["reserved_for"] for r in c.execute(
                "SELECT gpu_index, reserved_for FROM finetune_gpu_reservations"
            ).fetchall()}
            usable = [i for i in free if reserved_for.get(i, agent) == agent]
            if not usable:
                if free:
                    raise ValueError(f"the only free M10 GPU(s) ({free}) are reserved for the current "
                                     "Gazette game winner's priority claim - try a different one once it "
                                     "frees up, or wait for the reservation to expire")
                raise ValueError("no free M10 GPU right now - all 4 are claimed; check back or ask who can release one")
            index = preferred_index if preferred_index in usable else usable[0]
            c.execute(
                "UPDATE finetune_gpu_claims SET claimed_by=?, run_id=?, claimed_at=? WHERE gpu_index=?",
                (agent, run_id, now(), index),
            )
            # Claimed (by anyone, including the reservation's own holder) -
            # the reservation has served its purpose either way.
            c.execute("DELETE FROM finetune_gpu_reservations WHERE gpu_index=?", (index,))
            c.commit()
        return index

    def reserve_gpu_priority(self, agent: str, hours: float = GPU_PRIORITY_RESERVATION_HOURS) -> Optional[int]:
        """P90 (Gazette game prize: 'GPU-/Finetuning-Vorrang'): reserves one
        currently-IDLE M10 GPU exclusively for ``agent`` for ``hours`` -
        never touches a GPU that is already claimed, so this can never
        preempt or interrupt a peer's running job. Returns the reserved
        index, or None if every GPU was already claimed at grant time (and
        nothing is reserved in that case - a reservation is a head start on
        idle capacity, not a queued entitlement)."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            self._purge_expired_reservations(c)
            free = {r["gpu_index"] for r in c.execute(
                "SELECT gpu_index FROM finetune_gpu_claims WHERE claimed_by IS NULL"
            ).fetchall()}
            already_reserved = {r["gpu_index"] for r in c.execute(
                "SELECT gpu_index FROM finetune_gpu_reservations"
            ).fetchall()}
            available = sorted(free - already_reserved)
            if not available:
                c.commit()
                return None
            index = available[0]
            expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
            c.execute(
                "INSERT OR REPLACE INTO finetune_gpu_reservations(gpu_index,reserved_for,reserved_at,expires_at) "
                "VALUES(?,?,?,?)",
                (index, agent, now(), expires),
            )
            c.commit()
        return index

    def release_gpu(self, gpu_index: int, agent: str) -> None:
        with self._conn() as c:
            row = c.execute("SELECT claimed_by FROM finetune_gpu_claims WHERE gpu_index=?", (gpu_index,)).fetchone()
            if row is None:
                raise ValueError(f"unknown GPU index: {gpu_index}")
            if row["claimed_by"] and row["claimed_by"] != agent:
                raise ValueError(f"GPU {gpu_index} is claimed by {row['claimed_by']}, not {agent}")
            c.execute("UPDATE finetune_gpu_claims SET claimed_by=NULL, run_id=NULL, claimed_at=NULL WHERE gpu_index=?",
                     (gpu_index,))
            c.commit()

    def gpu_status(self) -> List[Dict[str, Any]]:
        with self._conn() as c:
            self._purge_expired_reservations(c)
            c.commit()
            rows = [dict(r) for r in c.execute("SELECT * FROM finetune_gpu_claims ORDER BY gpu_index")]
            reservations = {r["gpu_index"]: dict(r) for r in c.execute(
                "SELECT * FROM finetune_gpu_reservations"
            ).fetchall()}
        for row in rows:
            reservation = reservations.get(row["gpu_index"])
            row["reserved_for"] = reservation["reserved_for"] if reservation else None
            row["reservation_expires_at"] = reservation["expires_at"] if reservation else None
        return rows

    def propose_run(self, agent: str, base_model: str, method: str, dataset_description: str,
                    preferred_gpu_index: Optional[int] = None, notes: str = "") -> Dict[str, Any]:
        base_model = str(base_model or "").strip()[:200]
        method = str(method or "").strip()[:80]
        dataset_description = str(dataset_description or "").strip()[:1000]
        if not base_model or not method or not dataset_description:
            raise ValueError("propose_run requires base_model, method and dataset_description")
        run_id = f"ft_{uuid.uuid4().hex[:12]}"
        ts = now()
        gpu_index = self.claim_gpu(agent, run_id, preferred_gpu_index)
        with self._conn() as c:
            c.execute(
                "INSERT INTO finetune_runs(id,agent_id,base_model,method,dataset_description,gpu_index,"
                "status,notes,started_at,updated_at) VALUES(?,?,?,?,?,?,'proposed',?,?,?)",
                (run_id, agent, base_model, method, dataset_description, gpu_index, str(notes)[:500], ts, ts),
            )
            c.commit()
        return self.get_run(run_id)  # type: ignore

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM finetune_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["evaluations"] = [
                dict(r) for r in c.execute(
                    "SELECT * FROM finetune_evaluations WHERE run_id=? ORDER BY created_at", (run_id,)
                )
            ]
            return result

    def update_status(self, run_id: str, agent: str, status: str, job_reference: str = "",
                      output_path: str = "", notes: str = "") -> Dict[str, Any]:
        if status not in RUN_STATUSES:
            raise ValueError(f"unknown status: {status}")
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"unknown finetune run: {run_id}")
        if run["agent_id"] != agent:
            raise ValueError(f"only {run['agent_id']} may update run {run_id}")
        ts = now()
        completed_at = ts if status in ("completed", "failed", "abandoned") else run.get("completed_at")
        with self._conn() as c:
            c.execute(
                "UPDATE finetune_runs SET status=?, job_reference=COALESCE(NULLIF(?,''),job_reference), "
                "output_path=COALESCE(NULLIF(?,''),output_path), "
                "notes=CASE WHEN ?<>'' THEN notes || CASE WHEN notes<>'' THEN ' | ' ELSE '' END || ? ELSE notes END, "
                "updated_at=?, completed_at=? WHERE id=?",
                (status, job_reference, output_path, notes, notes, ts, completed_at, run_id),
            )
            c.commit()
        if status in ("completed", "failed", "abandoned") and run.get("gpu_index") is not None:
            # Best-effort: a run that is done no longer needs its GPU -
            # never raises if it was already released/reclaimed by hand.
            try:
                self.release_gpu(run["gpu_index"], agent)
            except ValueError:
                pass
        return self.get_run(run_id)  # type: ignore

    def record_evaluation(self, run_id: str, evaluator: str, metric_name: str, metric_value: float,
                          baseline_value: Optional[float] = None, notes: str = "") -> Dict[str, Any]:
        if not self.get_run(run_id):
            raise ValueError(f"unknown finetune run: {run_id}")
        metric_name = str(metric_name or "").strip()[:80]
        if not metric_name:
            raise ValueError("record_evaluation requires metric_name")
        with self._conn() as c:
            c.execute(
                "INSERT INTO finetune_evaluations(id,run_id,evaluator,metric_name,metric_value,baseline_value,"
                "notes,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"eval_{uuid.uuid4().hex[:12]}", run_id, evaluator, metric_name, float(metric_value),
                 float(baseline_value) if baseline_value is not None else None, str(notes)[:500], now()),
            )
            c.commit()
        return self.get_run(run_id)  # type: ignore

    def request_swap(self, run_id: str, agent: str) -> Dict[str, Any]:
        """The furthest an in-village action ever goes: a structured,
        visible request that this run's output should replace the
        currently-deployed model. Never applies anything itself - see
        module docstring. Requires at least one recorded evaluation, so
        King reviews an actual comparison, not a bare claim."""
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"unknown finetune run: {run_id}")
        if run["agent_id"] != agent:
            raise ValueError(f"only {run['agent_id']} may request a swap for run {run_id}")
        if run["status"] != "completed":
            raise ValueError(f"run {run_id} is not completed yet (status={run['status']})")
        if not run["evaluations"]:
            raise ValueError(f"run {run_id} has no recorded evaluation yet - record_evaluation first")
        request_id = f"swap_{uuid.uuid4().hex[:12]}"
        with self._conn() as c:
            c.execute(
                "INSERT INTO finetune_swap_requests(id,run_id,agent_id,requested_by,status,requested_at) "
                "VALUES(?,?,?,?,'pending',?)",
                (request_id, run_id, run["agent_id"], agent, now()),
            )
            c.commit()
        return self.get_swap_request(request_id)  # type: ignore

    def get_swap_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM finetune_swap_requests WHERE id=?", (request_id,)).fetchone()
            return dict(row) if row else None

    def review_swap(self, request_id: str, reviewer: str, decision: str, note: str = "") -> Dict[str, Any]:
        if decision not in ("approve", "reject"):
            raise ValueError(f"unknown decision: {decision}")
        request = self.get_swap_request(request_id)
        if not request:
            raise ValueError(f"unknown swap request: {request_id}")
        if request["status"] != "pending":
            raise ValueError(f"swap request {request_id} already decided (status={request['status']})")
        with self._conn() as c:
            c.execute(
                "UPDATE finetune_swap_requests SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                ("king_approved" if decision == "approve" else "rejected", reviewer, now(),
                 str(note).strip()[:400], request_id),
            )
            c.commit()
        return self.get_swap_request(request_id)  # type: ignore

    def pending_swap_requests(self, limit: int = 10) -> List[Dict[str, Any]]:
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM finetune_swap_requests WHERE status='pending' ORDER BY requested_at LIMIT ?",
                (limit,),
            )]

    def king_approved_swaps_awaiting_operator(self, limit: int = 10) -> List[Dict[str, Any]]:
        """King-endorsed but not yet applied - the honest hand-off point
        to a human operator (see module docstring); never auto-applied."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM finetune_swap_requests WHERE status='king_approved' ORDER BY reviewed_at LIMIT ?",
                (limit,),
            )]

    def list_for_agent(self, agent: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM finetune_runs WHERE agent_id=? ORDER BY started_at DESC LIMIT ?", (agent, limit)
            ).fetchall()]
        return [self.get_run(i) for i in ids]  # type: ignore

    def has_ever_engaged(self, agent: str) -> bool:
        """Whether this agent has ever proposed a run - the deterministic
        signal for the (advisory-only, see web/runtime.py) self-improvement
        reminder: a one-time "you have never engaged with this primary
        goal" nudge, not a recurring daily obligation (GPU scarcity - 4
        M10s for 9 residents - makes a hard daily gate actively
        counterproductive, unlike the calendar/gazette obligations)."""
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM finetune_runs WHERE agent_id=? LIMIT 1", (agent,)).fetchone()
            return row is not None

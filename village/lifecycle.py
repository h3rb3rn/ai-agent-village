"""Inference lifecycle management, persistent request tracking, and state reconciliation.

Tracks full request lifecycle: queued -> requesting -> completed/failed/cancelled/unknown.
Ensures stale generations do not trigger actions and guarantees no duplicate executions.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
import urllib.error
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def utc_now() -> str:
    """Return current ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


class InferenceState(str, Enum):
    """Lifecycle states for an inference request."""

    QUEUED = "queued"
    REQUESTING = "requesting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass
class InferenceRecord:
    """Persistent inference request record."""

    request_id: str
    agent_id: str
    generation: int
    model: str
    provider: str
    state: str
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_ms: Optional[int] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    error_class: Optional[str] = None
    error_detail: Optional[str] = None
    gpu_verified: bool = False
    action_executed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert record to dictionary."""
        return asdict(self)


def classify_error(exc: Exception) -> Tuple[str, str]:
    """Classify an inference exception into a standardized error category and detail.

    Args:
        exc: Caught exception.

    Returns:
        Tuple[str, str]: (error_class, detailed_message)
    """
    if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
        return "timeout", str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        code = exc.code
        body = ""
        try:
            body = exc.read(512).decode("utf-8", errors="replace")
        except Exception:
            pass
        if code == 401:
            return "http_401_unauthorized", body or str(exc)
        if code == 403:
            return "http_403_forbidden", body or str(exc)
        if code == 429:
            return "http_429_rate_limit", body or str(exc)
        if 500 <= code < 600:
            return f"http_{code}_server_error", body or str(exc)
        return f"http_{code}_error", body or str(exc)
    if isinstance(exc, urllib.error.URLError):
        reason = str(exc.reason).lower()
        if "connection refused" in reason:
            return "connection_refused", str(exc)
        if "connection reset" in reason:
            return "connection_reset", str(exc)
        return "network_error", str(exc)
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json_response", str(exc)
    return "unknown_error", str(exc)


class InferenceTracker:
    """Manages persistent SQLite storage and active state pointers for inference requests."""

    def __init__(self, db_path: Path, agent_id: str):
        """Initialize tracker with SQLite backing store.

        Args:
            db_path: Path to SQLite database file.
            agent_id: Unique agent identifier.
        """
        self.db_path = Path(db_path)
        self.agent_id = str(agent_id)
        self.active_path = self.db_path.parent / "active_request.json"
        self._init_db()

    @contextlib.contextmanager
    def _conn(self):
        """Context manager providing an auto-closing SQLite connection."""
        conn = sqlite3.connect(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create database tables and indices if not already present."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inference_requests (
                    request_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    duration_ms INTEGER,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    error_class TEXT,
                    error_detail TEXT,
                    gpu_verified INTEGER NOT NULL DEFAULT 0,
                    action_executed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inf_agent ON inference_requests(agent_id, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_inf_state ON inference_requests(state)"
            )
            conn.commit()

    def _write_active_pointer(self, record: Optional[InferenceRecord]) -> None:
        """Atomically update active_request.json with the current status."""
        try:
            tmp = self.active_path.with_suffix(".tmp")
            data = record.to_dict() if record else {}
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self.active_path)
        except OSError:
            pass

    def reconcile_stale_requests(self) -> int:
        """Reconcile requests left in 'queued' or 'requesting' from crashed or restarted processes.

        Transitions incomplete requests to 'unknown' to prevent UI from reporting 'thinking' indefinitely.

        Returns:
            int: Number of reconciled stale requests.
        """
        now_str = utc_now()
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE inference_requests
                SET state = ?, completed_at = ?, error_class = ?, error_detail = ?
                WHERE agent_id = ? AND state IN (?, ?)
                """,
                (
                    InferenceState.UNKNOWN.value,
                    now_str,
                    "interrupted_or_crashed",
                    "Process restarted or crashed before request completed",
                    self.agent_id,
                    InferenceState.QUEUED.value,
                    InferenceState.REQUESTING.value,
                ),
            )
            reconciled = cursor.rowcount
            conn.commit()

        if reconciled > 0:
            self._write_active_pointer(None)
        return reconciled

    def create_request(
        self,
        model: str,
        provider: str,
        generation: int,
    ) -> InferenceRecord:
        """Create and persist a new inference request before network dispatch.

        Args:
            model: Name of the model requested.
            provider: Provider type ('ollama' or 'openai').
            generation: Monotonic generation / version of agent configuration.

        Returns:
            InferenceRecord: Created record with status QUEUED.
        """
        request_id = f"req_{self.agent_id}_{int(time.time() * 1000)}"
        record = InferenceRecord(
            request_id=request_id,
            agent_id=self.agent_id,
            generation=generation,
            model=model,
            provider=provider,
            state=InferenceState.QUEUED.value,
            created_at=utc_now(),
        )
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO inference_requests (
                    request_id, agent_id, generation, model, provider, state, created_at,
                    gpu_verified, action_executed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
                """,
                (
                    record.request_id,
                    record.agent_id,
                    record.generation,
                    record.model,
                    record.provider,
                    record.state,
                    record.created_at,
                ),
            )
            conn.commit()

        self._write_active_pointer(record)
        return record

    def start_request(self, request_id: str) -> Optional[InferenceRecord]:
        """Transition request state to REQUESTING.

        Args:
            request_id: Request identifier.

        Returns:
            Optional[InferenceRecord]: Updated record.
        """
        started_at = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE inference_requests
                SET state = ?, started_at = ?
                WHERE request_id = ?
                """,
                (InferenceState.REQUESTING.value, started_at, request_id),
            )
            conn.commit()

        record = self.get_request(request_id)
        if record:
            self._write_active_pointer(record)
        return record

    def complete_request(
        self,
        request_id: str,
        duration_ms: int,
        prompt_tokens: Optional[int] = None,
        completion_tokens: Optional[int] = None,
        gpu_verified: bool = False,
    ) -> Optional[InferenceRecord]:
        """Transition request state to COMPLETED and record performance metrics.

        Args:
            request_id: Request identifier.
            duration_ms: Total duration in milliseconds.
            prompt_tokens: Optional count of input tokens.
            completion_tokens: Optional count of generated tokens.
            gpu_verified: True only if explicit backend verification confirms GPU execution.

        Returns:
            Optional[InferenceRecord]: Updated record.
        """
        completed_at = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE inference_requests
                SET state = ?, completed_at = ?, duration_ms = ?,
                    prompt_tokens = ?, completion_tokens = ?, gpu_verified = ?
                WHERE request_id = ?
                """,
                (
                    InferenceState.COMPLETED.value,
                    completed_at,
                    duration_ms,
                    prompt_tokens,
                    completion_tokens,
                    1 if gpu_verified else 0,
                    request_id,
                ),
            )
            conn.commit()

        record = self.get_request(request_id)
        if record:
            self._write_active_pointer(record)
        return record

    def fail_request(
        self,
        request_id: str,
        duration_ms: int,
        error_class: str,
        error_detail: str,
    ) -> Optional[InferenceRecord]:
        """Transition request state to FAILED with classified error diagnostic.

        Args:
            request_id: Request identifier.
            duration_ms: Total duration before failure.
            error_class: Categorized error string.
            error_detail: Descriptive error information.

        Returns:
            Optional[InferenceRecord]: Updated record.
        """
        completed_at = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE inference_requests
                SET state = ?, completed_at = ?, duration_ms = ?,
                    error_class = ?, error_detail = ?
                WHERE request_id = ?
                """,
                (
                    InferenceState.FAILED.value,
                    completed_at,
                    duration_ms,
                    error_class,
                    error_detail[:4000],
                    request_id,
                ),
            )
            conn.commit()

        record = self.get_request(request_id)
        if record:
            self._write_active_pointer(record)
        return record

    def cancel_request(
        self, request_id: str, reason: str = "cancelled"
    ) -> Optional[InferenceRecord]:
        """Transition request state to CANCELLED.

        Args:
            request_id: Request identifier.
            reason: Cancellation reason.

        Returns:
            Optional[InferenceRecord]: Updated record.
        """
        completed_at = utc_now()
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE inference_requests
                SET state = ?, completed_at = ?, error_class = ?, error_detail = ?
                WHERE request_id = ?
                """,
                (
                    InferenceState.CANCELLED.value,
                    completed_at,
                    "cancelled",
                    reason[:1000],
                    request_id,
                ),
            )
            conn.commit()

        record = self.get_request(request_id)
        if record:
            self._write_active_pointer(record)
        return record

    def request_cancellation(
        self, request_id: str, reason: str = "cancellation_requested"
    ) -> Optional[InferenceRecord]:
        """Mark request as CANCEL_REQUESTED while awaiting upstream response or disconnect.

        Args:
            request_id: Request identifier.
            reason: Cancellation reason.

        Returns:
            Optional[InferenceRecord]: Updated record.
        """
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE inference_requests
                SET state = ?, error_detail = ?
                WHERE request_id = ? AND state IN (?, ?)
                """,
                (
                    InferenceState.CANCEL_REQUESTED.value,
                    reason[:1000],
                    request_id,
                    InferenceState.QUEUED.value,
                    InferenceState.REQUESTING.value,
                ),
            )
            conn.commit()

        record = self.get_request(request_id)
        if record:
            self._write_active_pointer(record)
        return record

    def confirm_cancellation(
        self,
        request_id: str,
        backend_confirmed: bool = False,
        detail: str = "",
    ) -> Optional[InferenceRecord]:
        """Confirm cancellation of a request.

        If backend explicitly confirmed cancellation, marks CANCELLED.
        If backend is remote or unverified, marks UNKNOWN (running remote).

        Args:
            request_id: Request identifier.
            backend_confirmed: True if upstream server acknowledged cancellation.
            detail: Diagnostic or operator decision detail.

        Returns:
            Optional[InferenceRecord]: Updated record.
        """
        now_str = utc_now()
        target_state = (
            InferenceState.CANCELLED.value
            if backend_confirmed
            else InferenceState.UNKNOWN.value
        )
        error_class = (
            "cancelled"
            if backend_confirmed
            else "remote_backend_unverified"
        )
        full_detail = (
            detail
            if backend_confirmed
            else f"Cancellation requested but backend verification unconfirmed: {detail}"
        )

        with self._conn() as conn:
            conn.execute(
                """
                UPDATE inference_requests
                SET state = ?, completed_at = ?, error_class = ?, error_detail = ?
                WHERE request_id = ?
                """,
                (
                    target_state,
                    now_str,
                    error_class,
                    full_detail[:4000],
                    request_id,
                ),
            )
            conn.commit()

        record = self.get_request(request_id)
        if record:
            self._write_active_pointer(record)
        return record

    def mark_action_executed(self, request_id: str, current_generation: int) -> bool:
        """Atomically verify generation and execute action permission for a completed request.

        Prevents late responses from old generations after resume/reconfiguration,
        and prevents duplicate action execution for the same request.

        Args:
            request_id: Identifier of the completed request.
            current_generation: Active generation sequence number.

        Returns:
            bool: True if execution was authorized and marked; False if rejected.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                """
                UPDATE inference_requests
                SET action_executed = 1
                WHERE request_id = ?
                  AND generation = ?
                  AND state = ?
                  AND action_executed = 0
                """,
                (request_id, current_generation, InferenceState.COMPLETED.value),
            )
            authorized = cursor.rowcount == 1
            conn.commit()

        return authorized

    def get_request(self, request_id: str) -> Optional[InferenceRecord]:
        """Fetch request record by ID.

        Args:
            request_id: Request identifier.

        Returns:
            Optional[InferenceRecord]: Retrieved record or None.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM inference_requests WHERE request_id = ?", (request_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return InferenceRecord(
                request_id=row["request_id"],
                agent_id=row["agent_id"],
                generation=row["generation"],
                model=row["model"],
                provider=row["provider"],
                state=row["state"],
                created_at=row["created_at"],
                started_at=row["started_at"],
                completed_at=row["completed_at"],
                duration_ms=row["duration_ms"],
                prompt_tokens=row["prompt_tokens"],
                completion_tokens=row["completion_tokens"],
                error_class=row["error_class"],
                error_detail=row["error_detail"],
                gpu_verified=bool(row["gpu_verified"]),
                action_executed=bool(row["action_executed"]),
            )

    def get_recent_requests(self, limit: int = 10) -> List[InferenceRecord]:
        """Fetch most recent request records.

        Args:
            limit: Maximum count to return.

        Returns:
            List[InferenceRecord]: List of recent records.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM inference_requests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )
            rows = cursor.fetchall()
            return [
                InferenceRecord(
                    request_id=r["request_id"],
                    agent_id=r["agent_id"],
                    generation=r["generation"],
                    model=r["model"],
                    provider=r["provider"],
                    state=r["state"],
                    created_at=r["created_at"],
                    started_at=r["started_at"],
                    completed_at=r["completed_at"],
                    duration_ms=r["duration_ms"],
                    prompt_tokens=r["prompt_tokens"],
                    completion_tokens=r["completion_tokens"],
                    error_class=r["error_class"],
                    error_detail=r["error_detail"],
                    gpu_verified=bool(r["gpu_verified"]),
                    action_executed=bool(r["action_executed"]),
                )
                for r in rows
            ]

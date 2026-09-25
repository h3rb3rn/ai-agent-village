"""Durable projection queue (Transactional Outbox) for AI Village Memory.

Guarantees:
- Authoritative SQLite transactions write mutation and outbox event in the same transaction.
- Out-of-process / asynchronous projection workers decouple agent HTTP requests from downstream sinks.
- Independent backend state tracking (Chroma, Neo4j, etc.) with sequence progress, lag, and error isolation.
- Idempotent replays, repeatable upserts, and explicit tombstones for deletions and scope transitions.
- Exponential backoff on backend failure; no tight retry loops.
- Full rebuild capability without modifying or erasing primary SQLite data.
"""

from __future__ import annotations

import abc
import contextlib
import json
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("memory.projection")


def utc_now() -> str:
    """Return ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


def record_outbox_event(
    conn: sqlite3.Connection,
    memory_id: str,
    operation: str,
    payload: Dict[str, Any],
) -> int:
    """Record an outbox projection event inside the caller's active database transaction.

    Args:
        conn: Open SQLite connection inside an active transaction.
        memory_id: Memory identifier.
        operation: 'upsert', 'delete', or 'scope_change'.
        payload: Full serialized snapshot or tombstone.

    Returns:
        int: Generated sequence ID.
    """
    now_str = utc_now()
    payload_str = json.dumps(payload, ensure_ascii=False)
    cursor = conn.execute(
        """
        INSERT INTO memory_outbox (memory_id, operation, payload, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (memory_id, operation, payload_str, now_str),
    )
    return cursor.lastrowid or 0


class ProjectionBackend(abc.ABC):
    """Abstract interface for external memory projection backends (e.g. Chroma, Neo4j)."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Unique identifier for this backend (e.g. 'chroma', 'neo4j')."""
        pass

    @abc.abstractmethod
    def upsert(self, memory: Dict[str, Any]) -> None:
        """Upsert a memory record into the projection sink.

        Args:
            memory: Dictionary representing the memory item.
        """
        pass

    @abc.abstractmethod
    def delete(self, memory_id: str, tombstone: Dict[str, Any]) -> None:
        """Delete or tombstone a memory record from the projection sink.

        Args:
            memory_id: Memory identifier.
            tombstone: Metadata concerning the deletion or scope transition.
        """
        pass

    @abc.abstractmethod
    def clear(self) -> None:
        """Reset or clear all indexed data in this backend for a full rebuild."""
        pass

    def is_healthy(self) -> bool:
        """Check if backend is reachable and operating normally."""
        return True


class InMemorySink(ProjectionBackend):
    """Reference in-memory projection sink used for tests and verification."""

    def __init__(self, name: str = "test_sink", fail_on_write: bool = False):
        self._name = name
        self.records: Dict[str, Dict[str, Any]] = {}
        self.tombstones: Dict[str, Dict[str, Any]] = {}
        self.fail_on_write = fail_on_write

    @property
    def name(self) -> str:
        return self._name

    def upsert(self, memory: Dict[str, Any]) -> None:
        if self.fail_on_write:
            raise ConnectionError(f"Backend '{self._name}' is offline/simulated failure")
        mem_id = memory["id"]
        self.records[mem_id] = dict(memory)
        self.tombstones.pop(mem_id, None)

    def delete(self, memory_id: str, tombstone: Dict[str, Any]) -> None:
        if self.fail_on_write:
            raise ConnectionError(f"Backend '{self._name}' is offline/simulated failure")
        self.records.pop(memory_id, None)
        self.tombstones[memory_id] = dict(tombstone)

    def clear(self) -> None:
        self.records.clear()
        self.tombstones.clear()


class ProjectionWorker:
    """Worker engine that polls the transactional outbox and projects items to backends."""

    def __init__(self, db_path: Path):
        """Initialize projection worker with database path.

        Args:
            db_path: Path to authoritative memory SQLite database.
        """
        self.db_path = Path(db_path)
        self.backends: Dict[str, ProjectionBackend] = {}
        self._next_retry: Dict[str, float] = {}
        self._init_tables()

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        try:
            yield conn
        finally:
            conn.close()

    def _init_tables(self) -> None:
        """Ensure outbox and projection state tables exist."""
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_outbox (
                    sequence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    memory_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_outbox_seq ON memory_outbox(sequence_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS projection_states (
                    backend TEXT PRIMARY KEY,
                    last_sequence_id INTEGER NOT NULL DEFAULT 0,
                    last_projected_at TEXT,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    status TEXT NOT NULL DEFAULT 'active'
                )
                """
            )
            conn.commit()

    def register_backend(self, backend: ProjectionBackend) -> None:
        """Register a projection backend."""
        self.backends[backend.name] = backend
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO projection_states (backend, last_sequence_id, status)
                VALUES (?, 0, 'active')
                """,
                (backend.name,),
            )
            conn.commit()

    def get_status(self) -> Dict[str, Any]:
        """Retrieve current queue length, latest sequence, and status per backend.

        Returns:
            Dict[str, Any]: Projection queue and backend status report.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            max_seq = conn.execute("SELECT coalesce(max(sequence_id), 0) FROM memory_outbox").fetchone()[0]
            states = conn.execute("SELECT * FROM projection_states").fetchall()

            backends_info = {}
            for row in states:
                b_name = row["backend"]
                last_seq = row["last_sequence_id"]
                lag = max(0, max_seq - last_seq)
                backends_info[b_name] = {
                    "last_sequence_id": last_seq,
                    "lag": lag,
                    "error_count": row["error_count"],
                    "last_error": row["last_error"],
                    "last_projected_at": row["last_projected_at"],
                    "status": row["status"],
                }

            return {
                "max_sequence_id": max_seq,
                "backends": backends_info,
            }

    def process_backend(self, backend_name: str, batch_size: int = 50) -> int:
        """Process a batch of pending outbox events for a specific backend.

        Handles error backoff, idempotence, and sequence progression.

        Args:
            backend_name: Name of registered backend.
            batch_size: Maximum outbox items to process in this batch.

        Returns:
            int: Number of items successfully projected.
        """
        backend = self.backends.get(backend_name)
        if not backend:
            raise ValueError(f"Unknown projection backend: {backend_name}")

        now_mono = time.monotonic()
        if now_mono < self._next_retry.get(backend_name, 0.0):
            # In backoff period, skip processing for now
            return 0

        with self._conn() as conn:
            conn.row_factory = sqlite3.Row

            state = conn.execute(
                "SELECT * FROM projection_states WHERE backend = ?", (backend_name,)
            ).fetchone()
            if not state:
                conn.execute(
                    "INSERT INTO projection_states (backend, last_sequence_id) VALUES (?, 0)",
                    (backend_name,),
                )
                conn.commit()
                last_seq = 0
                error_count = 0
            else:
                last_seq = state["last_sequence_id"]
                error_count = state["error_count"]

            items = conn.execute(
                """
                SELECT sequence_id, memory_id, operation, payload, created_at
                FROM memory_outbox
                WHERE sequence_id > ?
                ORDER BY sequence_id ASC LIMIT ?
                """,
                (last_seq, batch_size),
            ).fetchall()

            if not items:
                return 0

            projected_count = 0
            for item in items:
                seq_id = item["sequence_id"]
                mem_id = item["memory_id"]
                op = item["operation"]
                payload = json.loads(item["payload"])

                try:
                    if op == "upsert":
                        backend.upsert(payload)
                    elif op in ("delete", "scope_change"):
                        # For scope_change, if moving to private, it must be removed from shared indices
                        backend.delete(mem_id, payload)
                    else:
                        logger.warning("Unrecognized outbox operation '%s' for item %s", op, seq_id)

                    now_str = utc_now()
                    conn.execute(
                        """
                        UPDATE projection_states
                        SET last_sequence_id = ?, last_projected_at = ?, error_count = 0, last_error = NULL, status = 'active'
                        WHERE backend = ?
                        """,
                        (seq_id, now_str, backend_name),
                    )
                    conn.commit()
                    projected_count += 1
                except Exception as exc:
                    err_msg = str(exc)
                    now_str = utc_now()
                    new_error_count = error_count + 1
                    # Exponential backoff capped at 300 seconds
                    backoff_delay = min(2 ** min(new_error_count, 8), 300)
                    self._next_retry[backend_name] = time.monotonic() + backoff_delay

                    conn.execute(
                        """
                        UPDATE projection_states
                        SET error_count = ?, last_error = ?, status = 'degraded'
                        WHERE backend = ?
                        """,
                        (new_error_count, err_msg, backend_name),
                    )
                    conn.commit()
                    # Error isolation: failure halts this backend's batch without crashing caller or other backends
                    break

            return projected_count

    def process_all(self, batch_size: int = 50) -> Dict[str, int]:
        """Run a projection cycle across all registered backends.

        Returns:
            Dict[str, int]: Items projected per backend.
        """
        results = {}
        for b_name in list(self.backends.keys()):
            results[b_name] = self.process_backend(b_name, batch_size=batch_size)
        return results

    def rebuild(self, backend_name: str) -> int:
        """Completely rebuild a projection from the authoritative SQLite memories table.

        Clears the backend sink, iterates over all active unexpired memories,
        streams them to the sink, and sets last_sequence_id to the current max sequence.

        Args:
            backend_name: Name of registered backend.

        Returns:
            int: Number of memories re-projected.
        """
        backend = self.backends.get(backend_name)
        if not backend:
            raise ValueError(f"Unknown projection backend: {backend_name}")

        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            max_seq = conn.execute("SELECT coalesce(max(sequence_id), 0) FROM memory_outbox").fetchone()[0]

            backend.clear()

            now_str = utc_now()
            rows = conn.execute(
                """
                SELECT * FROM memories
                WHERE (expires_at IS NULL OR expires_at = '' OR expires_at > ?)
                ORDER BY created_at ASC
                """,
                (now_str,),
            ).fetchall()

            rebuilt_count = 0
            for row in rows:
                item = dict(row)
                try:
                    item["metadata"] = json.loads(item["metadata"])
                except Exception:
                    item["metadata"] = {}
                backend.upsert(item)
                rebuilt_count += 1

            conn.execute(
                """
                UPDATE projection_states
                SET last_sequence_id = ?, last_projected_at = ?, error_count = 0, last_error = NULL, status = 'rebuilt'
                WHERE backend = ?
                """,
                (max_seq, now_str, backend_name),
            )
            conn.commit()

        return rebuilt_count


def create_default_projection_worker(
    db_path: Path,
    enable_chroma: bool = True,
    enable_neo4j: bool = True,
) -> ProjectionWorker:
    """Create a ProjectionWorker and optionally register projection backends (Chroma, Neo4j).

    Args:
        db_path: Path to authoritative SQLite memory database.
        enable_chroma: Whether to register Chroma projection adapter if enabled.
        enable_neo4j: Whether to register Neo4j projection adapter if enabled.

    Returns:
        Configured ProjectionWorker instance.
    """
    worker = ProjectionWorker(db_path)
    if enable_chroma and os.environ.get("CHROMA_ENABLED", "1").lower() not in ("0", "false", "no", "disabled"):
        try:
            from memory.chroma_adapter import create_chroma_adapter

            adapter = create_chroma_adapter()
            worker.register_backend(adapter)
        except Exception as exc:
            logger.warning("Failed to register Chroma projection backend: %s", exc)

    if enable_neo4j and os.environ.get("NEO4J_ENABLED", "1").lower() not in ("0", "false", "no", "disabled"):
        try:
            from memory.neo4j_adapter import create_neo4j_adapter

            adapter = create_neo4j_adapter()
            worker.register_backend(adapter)
        except Exception as exc:
            logger.warning("Failed to register Neo4j projection backend: %s", exc)

    return worker

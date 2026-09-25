"""Transactional coordination store and board projection module for AI Village.

Provides SQLite-backed atomic transactions, migrations, task claims with concurrency
guarantees, message delivery tracking, and idempotent legacy JSON import with corruption quarantine.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def utc_now() -> str:
    """Return current ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


# Schema version managed by migrations
LATEST_SCHEMA_VERSION = 3


def is_weak_criterion(criterion: str) -> bool:
    """Evaluate whether a success criterion is weak, generic, or a placeholder."""
    cleaned = criterion.strip().lower()
    if len(cleaned) < 15:
        return True
    placeholders = {
        "imported from legacy", "tbd", "done", "test", "n/a",
        "to be determined", "success", "working", "criterion 1", "criterion 2",
        "work in progress", "wip", "none"
    }
    return cleaned in placeholders


class CoordinationStore:
    """SQLite-backed coordinator managing tasks, transactions, and board projections."""

    def __init__(self, db_path: Path, board_dir: Optional[Path] = None):
        """Initialize store, apply schema migrations, and sync projections.

        Args:
            db_path: Path to SQLite coordination database.
            board_dir: Directory containing board files (e.g. work-items.json).
        """
        self.db_path = Path(db_path)
        self.board_dir = Path(board_dir) if board_dir else self.db_path.parent
        self.projection_path = self.board_dir / "work-items.json"
        self._init_db()

    @contextlib.contextmanager
    def _conn(self, timeout: float = 30.0):
        """Context manager providing an auto-closing SQLite connection with WAL and busy timeout."""
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create tables and apply migrations up to LATEST_SCHEMA_VERSION."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            cursor = conn.execute("SELECT MAX(version) FROM schema_migrations")
            row = cursor.fetchone()
            current_version = row[0] if (row and row[0] is not None) else 0

            if current_version < 1:
                # Migration 1: Base tasks and messages schema
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        success_criterion TEXT NOT NULL,
                        author TEXT NOT NULL,
                        owner TEXT,
                        status TEXT NOT NULL,
                        lease_until REAL NOT NULL DEFAULT 0.0,
                        evidence TEXT,
                        completion_verified INTEGER NOT NULL DEFAULT 0,
                        version INTEGER NOT NULL DEFAULT 1,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner ON tasks(owner)")

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS messages (
                        id TEXT PRIMARY KEY,
                        timestamp TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        recipient TEXT,
                        reply_to TEXT,
                        content TEXT NOT NULL,
                        delivered INTEGER NOT NULL DEFAULT 0,
                        acknowledged INTEGER NOT NULL DEFAULT 0,
                        delivered_at TEXT,
                        acknowledged_at TEXT
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_recipient ON messages(recipient)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp)")

                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
                    (utc_now(),),
                )

            if current_version < 2:
                # Migration 2: Persistent inbox messages and per-agent receipts tracking
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS inbox_messages (
                        id TEXT PRIMARY KEY,
                        source TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        recipient TEXT,
                        reply_to TEXT,
                        content TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_recipient ON inbox_messages(recipient)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_timestamp ON inbox_messages(timestamp)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_source ON inbox_messages(source)")

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS message_receipts (
                        message_id TEXT NOT NULL,
                        agent_id TEXT NOT NULL,
                        delivered INTEGER NOT NULL DEFAULT 0,
                        delivered_at TEXT,
                        acknowledged INTEGER NOT NULL DEFAULT 0,
                        acknowledged_at TEXT,
                        PRIMARY KEY (message_id, agent_id),
                        FOREIGN KEY (message_id) REFERENCES inbox_messages(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_agent ON message_receipts(agent_id, acknowledged)")

                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (2, ?)",
                    (utc_now(),),
                )

            if current_version < 3:
                # Migration 3: Task work progress tracking, revision history, and weak criterion marking
                existing_cols = {col[1] for col in conn.execute("PRAGMA table_info(tasks)").fetchall()}
                if "goal" not in existing_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN goal TEXT")
                if "last_finding" not in existing_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN last_finding TEXT")
                if "next_step" not in existing_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN next_step TEXT")
                if "blockers" not in existing_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN blockers TEXT")
                if "artifact_refs" not in existing_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN artifact_refs TEXT")
                if "weak_criterion" not in existing_cols:
                    conn.execute("ALTER TABLE tasks ADD COLUMN weak_criterion INTEGER NOT NULL DEFAULT 0")

                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS task_revisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        task_id TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        actor TEXT NOT NULL,
                        action TEXT NOT NULL,
                        owner TEXT,
                        status TEXT NOT NULL,
                        last_finding TEXT,
                        next_step TEXT,
                        blockers TEXT,
                        evidence TEXT,
                        timestamp TEXT NOT NULL,
                        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_revisions_task ON task_revisions(task_id, revision)")

                # Mark weak criteria on existing tasks
                cursor = conn.execute("SELECT id, success_criterion FROM tasks")
                for r in cursor.fetchall():
                    if is_weak_criterion(r[1]):
                        conn.execute("UPDATE tasks SET weak_criterion = 1 WHERE id = ?", (r[0],))

                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (3, ?)",
                    (utc_now(),),
                )
            conn.commit()

        # Set restrictive group-writable file mode for coordination db
        try:
            self.db_path.chmod(0o660)
        except OSError:
            pass

    def import_legacy_tasks(self, json_path: Path) -> Tuple[int, Optional[str]]:
        """Idempotently import tasks from legacy work-items.json into SQLite.

        If the file contains invalid/corrupted JSON, it is moved to quarantine
        to prevent data loss and diagnostics are recorded.

        Args:
            json_path: Path to work-items.json.

        Returns:
            Tuple[int, Optional[str]]: (imported_count, error_or_quarantine_message)
        """
        if not json_path.exists():
            return 0, None

        raw = json_path.read_text(encoding="utf-8").strip()
        if not raw:
            return 0, None

        try:
            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("Legacy work items root must be a JSON list")
        except Exception as exc:
            # Quarantine corrupted file rather than overwriting with empty default
            stamp = int(time.time())
            quarantine = json_path.parent / f"corrupted-{json_path.name}.{stamp}"
            shutil.move(str(json_path), str(quarantine))
            return 0, f"Quarantined corrupted legacy tasks file to {quarantine.name}: {exc}"

        imported = 0
        now_str = utc_now()
        with self._conn() as conn:
            for item in data:
                if not isinstance(item, dict) or "id" not in item or "title" not in item:
                    continue
                task_id = str(item["id"])
                title = str(item["title"])
                criterion = str(item.get("success_criterion", "Imported from legacy"))
                author = str(item.get("author", "legacy"))
                owner = item.get("owner")
                status = str(item.get("status", "open"))
                lease_until = float(item.get("lease_until", 0.0))
                evidence = item.get("evidence")
                completion_verified = 1 if item.get("completion_verified") else 0
                goal = str(item.get("goal") or title)
                last_finding = item.get("last_finding")
                next_step = item.get("next_step")
                blockers = item.get("blockers")
                artifact_refs = json.dumps(item.get("artifact_refs") or [])
                weak = 1 if (item.get("weak_criterion") or is_weak_criterion(criterion)) else 0
                created_at = str(item.get("created_at", now_str))
                updated_at = str(item.get("updated_at", now_str))

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO tasks (
                        id, title, success_criterion, author, owner, status,
                        lease_until, evidence, completion_verified, version,
                        goal, last_finding, next_step, blockers, artifact_refs, weak_criterion,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        title,
                        criterion,
                        author,
                        owner,
                        status,
                        lease_until,
                        evidence,
                        completion_verified,
                        goal,
                        last_finding,
                        next_step,
                        blockers,
                        artifact_refs,
                        weak,
                        created_at,
                        updated_at,
                    ),
                )
                if cursor.rowcount > 0:
                    imported += 1
            conn.commit()

        self.sync_projection()
        return imported, None

    def sync_projection(self) -> None:
        """Write current tasks from SQLite to projection file work-items.json atomically.

        Preserves read compatibility for existing file-based observers and tools.
        """
        all_tasks = self.list_tasks()
        payload = json.dumps(all_tasks, indent=2, ensure_ascii=False) + "\n"
        temp_path = self.projection_path.with_suffix(f".tmp-{os.getpid()}")
        try:
            temp_path.write_text(payload, encoding="utf-8")
            temp_path.chmod(0o660)
            temp_path.replace(self.projection_path)
        except OSError:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _row_to_task(self, r: sqlite3.Row) -> Dict[str, Any]:
        """Convert a SQLite tasks row to a comprehensive dictionary with P10 fields."""
        keys = r.keys()
        criterion = r["success_criterion"]
        return {
            "id": r["id"],
            "title": r["title"],
            "goal": r["goal"] if ("goal" in keys and r["goal"]) else r["title"],
            "success_criterion": criterion,
            "weak_criterion": bool(r["weak_criterion"]) if "weak_criterion" in keys else is_weak_criterion(criterion),
            "author": r["author"],
            "owner": r["owner"],
            "status": r["status"],
            "lease_until": r["lease_until"],
            "last_finding": r["last_finding"] if "last_finding" in keys else None,
            "next_step": r["next_step"] if "next_step" in keys else None,
            "blockers": r["blockers"] if "blockers" in keys else None,
            "artifact_refs": json.loads(r["artifact_refs"]) if ("artifact_refs" in keys and r["artifact_refs"]) else [],
            "evidence": r["evidence"],
            "completion_verified": bool(r["completion_verified"]),
            "revision": r["version"],
            "version": r["version"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }

    def list_tasks(self) -> List[Dict[str, Any]]:
        """List all tasks ordered by created_at.

        Returns:
            List[Dict[str, Any]]: List of task records.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM tasks ORDER BY created_at ASC")
            rows = cursor.fetchall()
            return [self._row_to_task(r) for r in rows]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Fetch single task by ID.

        Args:
            task_id: Task identifier.

        Returns:
            Optional[Dict[str, Any]]: Task dictionary or None.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_task(row)

    def operate(self, actor: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Perform transactional task operation: create, claim, complete, or yield.

        Ensures atomic transactions and race-condition free claims between processes.

        Args:
            actor: Agent identifier or username performing the action.
            args: Action payload dictionary.

        Returns:
            Dict[str, Any]: Resulting task dictionary.

        Raises:
            ValueError: On invalid parameters, unauthorized attempts, or capacity limits.
        """
        action = args.get("action")
        now_str = utc_now()

        with self._conn() as conn:
            conn.execute("BEGIN IMMEDIATE")

            if action == "create":
                title = str(args.get("title", "")).strip()[:160]
                criterion = str(args.get("success_criterion", "")).strip()[:800]
                if not title or not criterion:
                    raise ValueError("create requires title and success_criterion")

                # Check existing non-complete tasks with identical title (case-insensitive)
                cursor = conn.execute(
                    "SELECT id FROM tasks WHERE LOWER(title) = LOWER(?) AND status != 'complete'",
                    (title,),
                )
                existing = cursor.fetchone()
                if existing:
                    conn.rollback()
                    return self.get_task(existing[0])  # type: ignore

                # Check max open project limit (64)
                cursor = conn.execute("SELECT COUNT(*) FROM tasks WHERE status != 'complete'")
                count = cursor.fetchone()[0]
                if count >= 64:
                    raise ValueError("64 open projects already exist; join or finish one")

                task_id = uuid.uuid4().hex[:12]
                goal = str(args.get("goal") or title).strip()[:400]
                next_step = str(args.get("next_step") or "").strip()[:800] or None
                blockers = str(args.get("blockers") or "").strip()[:800] or None
                artifact_refs = json.dumps(args.get("artifact_refs") or [])
                weak = 1 if is_weak_criterion(criterion) else 0

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, success_criterion, author, owner, status,
                        lease_until, evidence, completion_verified, version,
                        goal, last_finding, next_step, blockers, artifact_refs, weak_criterion,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 'open', 0.0, NULL, 0, 1, ?, NULL, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, title, criterion, actor, goal, next_step, blockers, artifact_refs, weak, now_str, now_str),
                )
                conn.execute(
                    """
                    INSERT INTO task_revisions (
                        task_id, revision, actor, action, owner, status,
                        last_finding, next_step, blockers, evidence, timestamp
                    ) VALUES (?, 1, ?, 'create', NULL, 'open', NULL, ?, ?, NULL, ?)
                    """,
                    (task_id, actor, next_step, blockers, now_str),
                )
                conn.commit()
                self.sync_projection()
                return self.get_task(task_id)  # type: ignore

            # All other actions require existing task_id
            task_id = args.get("task_id")
            cursor = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
            row = cursor.fetchone()
            if not row:
                raise ValueError("unknown task_id")

            curr_owner = row["owner"]
            lease_until = row["lease_until"]
            curr_version = row["version"]
            curr_time = time.time()

            # Stale worker check: if caller provided expected_revision, verify against current version
            if "expected_revision" in args and args["expected_revision"] is not None:
                if int(args["expected_revision"]) != curr_version:
                    raise ValueError(f"stale task revision: expected {args['expected_revision']} but current is {curr_version}")

            if action == "claim":
                if row["status"] == "complete":
                    raise ValueError("task already complete")

                # Idempotent lease renewal if already owned by actor
                if curr_owner == actor and lease_until > curr_time:
                    new_lease = curr_time + 7200.0
                    conn.execute(
                        "UPDATE tasks SET lease_until = ?, updated_at = ? WHERE id = ?",
                        (new_lease, now_str, task_id),
                    )
                    conn.commit()
                    self.sync_projection()
                    return self.get_task(task_id)  # type: ignore

                # If owned by another worker and lease is active, reject
                if curr_owner not in (None, actor) and lease_until > curr_time:
                    raise ValueError("task already claimed; contact owner")

                new_lease = curr_time + 7200.0
                new_version = curr_version + 1
                cursor = conn.execute(
                    """
                    UPDATE tasks
                    SET owner = ?, status = 'active', lease_until = ?,
                        version = ?, updated_at = ?
                    WHERE id = ? AND (owner IS NULL OR owner = ? OR lease_until <= ?)
                    """,
                    (actor, new_lease, new_version, now_str, task_id, actor, curr_time),
                )
                if cursor.rowcount == 0:
                    raise ValueError("task already claimed; contact owner")

                conn.execute(
                    """
                    INSERT INTO task_revisions (
                        task_id, revision, actor, action, owner, status,
                        last_finding, next_step, blockers, evidence, timestamp
                    ) VALUES (?, ?, ?, 'claim', ?, 'active', ?, ?, ?, ?, ?)
                    """,
                    (task_id, new_version, actor, actor, row["last_finding"], row["next_step"], row["blockers"], row["evidence"], now_str),
                )
                conn.commit()
                self.sync_projection()
                return self.get_task(task_id)  # type: ignore

            elif action in ("progress", "update"):
                if curr_owner != actor or row["status"] != "active":
                    raise ValueError("only active task owner may record progress")

                last_finding = str(args.get("last_finding") or row["last_finding"] or "").strip()[:2000] or None
                next_step = str(args.get("next_step") or row["next_step"] or "").strip()[:800] or None
                blockers = str(args["blockers"]).strip()[:800] if "blockers" in args and args["blockers"] else (None if "blockers" in args else row["blockers"])
                artifact_refs = json.dumps(args.get("artifact_refs")) if "artifact_refs" in args else row["artifact_refs"]
                new_criterion = str(args.get("success_criterion", "")).strip()[:800] if "success_criterion" in args else row["success_criterion"]
                weak = 1 if is_weak_criterion(new_criterion) else 0

                new_version = curr_version + 1
                conn.execute(
                    """
                    UPDATE tasks
                    SET last_finding = ?, next_step = ?, blockers = ?, artifact_refs = ?,
                        success_criterion = ?, weak_criterion = ?, version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (last_finding, next_step, blockers, artifact_refs, new_criterion, weak, new_version, now_str, task_id),
                )
                conn.execute(
                    """
                    INSERT INTO task_revisions (
                        task_id, revision, actor, action, owner, status,
                        last_finding, next_step, blockers, evidence, timestamp
                    ) VALUES (?, ?, ?, 'progress', ?, 'active', ?, ?, ?, ?, ?)
                    """,
                    (task_id, new_version, actor, actor, last_finding, next_step, blockers, row["evidence"], now_str),
                )
                conn.commit()
                self.sync_projection()
                return self.get_task(task_id)  # type: ignore

            elif action in ("complete", "yield"):
                if curr_owner != actor:
                    raise ValueError("only owner may complete/yield")
                evidence = str(args.get("evidence", "")).strip()[:2000]
                if not evidence:
                    raise ValueError("provide evidence or reason")

                new_status = "complete" if action == "complete" else "open"
                new_owner = actor if action == "complete" else None
                new_lease = lease_until if action == "complete" else 0.0
                last_finding = str(args.get("last_finding") or row["last_finding"] or "").strip()[:2000] or None
                blockers = str(args.get("blockers") or "").strip()[:800] or None if action == "yield" else None
                artifact_refs = json.dumps(args.get("artifact_refs")) if "artifact_refs" in args else row["artifact_refs"]

                new_version = curr_version + 1
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = ?, owner = ?, lease_until = ?, evidence = ?,
                        last_finding = ?, blockers = ?, artifact_refs = ?,
                        completion_verified = 0, version = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (new_status, new_owner, new_lease, evidence, last_finding, blockers, artifact_refs, new_version, now_str, task_id),
                )
                conn.execute(
                    """
                    INSERT INTO task_revisions (
                        task_id, revision, actor, action, owner, status,
                        last_finding, next_step, blockers, evidence, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, new_version, actor, action, new_owner, new_status, last_finding, row["next_step"], blockers, evidence, now_str),
                )
                conn.commit()
                self.sync_projection()
                return self.get_task(task_id)  # type: ignore

            else:
                raise ValueError("action must be create, claim, progress, complete or yield")

    def get_task_history(self, task_id: str) -> List[Dict[str, Any]]:
        """Return full revision history for a task.

        Args:
            task_id: Task identifier.

        Returns:
            List[Dict[str, Any]]: Revision entries ordered by revision ascending.
        """
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM task_revisions WHERE task_id = ? ORDER BY revision ASC",
                (task_id,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "revision": r["revision"],
                    "actor": r["actor"],
                    "action": r["action"],
                    "owner": r["owner"],
                    "status": r["status"],
                    "last_finding": r["last_finding"],
                    "next_step": r["next_step"],
                    "blockers": r["blockers"],
                    "evidence": r["evidence"],
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]

    def post_inbox_message(
        self,
        source: str,
        sender: str,
        content: str,
        recipient: Optional[str] = None,
        reply_to: Optional[str] = None,
        timestamp: Optional[str] = None,
        msg_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Post a direct, broadcast, or organic message to the persistent inbox.

        Args:
            source: Message source ('board', 'direct', or 'organic').
            sender: Sending agent or entity identifier.
            content: Text payload of the message.
            recipient: Optional target agent identifier (None for broadcast).
            reply_to: Optional referenced message ID.
            timestamp: Optional ISO 8601 timestamp (defaults to utc_now()).
            msg_id: Optional explicit message ID.

        Returns:
            Dict[str, Any]: Saved message dictionary.
        """
        now_str = utc_now()
        ts = timestamp or now_str
        mid = msg_id or f"msg_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO inbox_messages (
                    id, source, sender, recipient, reply_to, content, timestamp, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (mid, source, sender, recipient, reply_to, content, ts, now_str),
            )
            conn.commit()

        return {
            "id": mid,
            "source": source,
            "sender": sender,
            "recipient": recipient,
            "reply_to": reply_to,
            "content": content,
            "timestamp": ts,
        }

    def fetch_unacknowledged_messages(
        self,
        agent_id: str,
        source: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Fetch unacknowledged messages addressed to agent or broadcast to all.

        Args:
            agent_id: Recipient agent identifier.
            source: Optional source filter ('board', 'direct', 'organic').
            limit: Maximum count to return.

        Returns:
            List[Dict[str, Any]]: List of unacknowledged message dictionaries.
        """
        with self._conn() as conn:
            query = """
                SELECT m.* FROM inbox_messages m
                LEFT JOIN message_receipts r
                    ON m.id = r.message_id AND r.agent_id = ?
                WHERE (m.recipient IS NULL OR m.recipient = ?)
                  AND (r.acknowledged IS NULL OR r.acknowledged = 0)
            """
            params: List[Any] = [agent_id, agent_id]
            if source:
                query += " AND m.source = ?"
                params.append(source)
            query += " ORDER BY m.timestamp ASC, m.id ASC LIMIT ?"
            params.append(limit)

            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            return [
                {
                    "id": r["id"],
                    "source": r["source"],
                    "sender": r["sender"],
                    "recipient": r["recipient"],
                    "reply_to": r["reply_to"],
                    "content": r["content"],
                    "timestamp": r["timestamp"],
                }
                for r in rows
            ]

    def mark_messages_delivered(self, agent_id: str, message_ids: List[str]) -> None:
        """Mark messages as delivered to the agent in the prompt context.

        Args:
            agent_id: Agent identifier.
            message_ids: List of message identifiers included in the prompt.
        """
        if not message_ids:
            return
        now_str = utc_now()
        with self._conn() as conn:
            for mid in message_ids:
                conn.execute(
                    """
                    INSERT INTO message_receipts (message_id, agent_id, delivered, delivered_at)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(message_id, agent_id) DO UPDATE SET
                        delivered = 1,
                        delivered_at = excluded.delivered_at
                    """,
                    (mid, agent_id, now_str),
                )
            conn.commit()

    def acknowledge_messages(self, agent_id: str, message_ids: List[str]) -> int:
        """Explicitly acknowledge messages processed by the agent.

        Args:
            agent_id: Agent identifier.
            message_ids: List of message identifiers to acknowledge.

        Returns:
            int: Number of acknowledged receipts.
        """
        if not message_ids:
            return 0
        now_str = utc_now()
        acked = 0
        with self._conn() as conn:
            for mid in message_ids:
                cursor = conn.execute(
                    """
                    INSERT INTO message_receipts (message_id, agent_id, delivered, delivered_at, acknowledged, acknowledged_at)
                    VALUES (?, ?, 1, ?, 1, ?)
                    ON CONFLICT(message_id, agent_id) DO UPDATE SET
                        acknowledged = 1,
                        acknowledged_at = excluded.acknowledged_at
                    """,
                    (mid, agent_id, now_str, now_str),
                )
                acked += cursor.rowcount
            conn.commit()
        return acked

    def sync_organic_inbox(
        self,
        organic_file: Path,
        agent_id: str,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """Idempotently import entries from organic-inbox.jsonl and fetch unacknowledged messages.

        Args:
            organic_file: Path to organic-inbox.jsonl.
            agent_id: Current agent identifier.
            limit: Maximum count to return.

        Returns:
            List[Dict[str, Any]]: Unacknowledged organic messages.
        """
        if organic_file.exists():
            try:
                for line in organic_file.read_text(encoding="utf-8").splitlines():
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(entry, dict):
                        ts = str(entry.get("timestamp", utc_now()))
                        content = str(entry.get("message") or entry.get("content", ""))
                        sender = str(entry.get("sender", "operator"))
                        content_digest = hashlib.sha256(f"{ts}:{content}".encode("utf-8")).hexdigest()[:12]
                        msg_id = str(entry.get("id") or f"org_{ts}_{content_digest}")
                        self.post_inbox_message(
                            source="organic",
                            sender=sender,
                            content=content,
                            recipient=entry.get("recipient"),
                            timestamp=ts,
                            msg_id=msg_id,
                        )
            except OSError:
                pass

        return self.fetch_unacknowledged_messages(agent_id=agent_id, source="organic", limit=limit)

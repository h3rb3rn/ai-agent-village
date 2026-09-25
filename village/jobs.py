"""Persistent background tool job manager for AI Village.

Provides traceable execution of long-running shell/build tasks with persistent IDs,
process group isolation (pgid), output bounding, timeouts, concurrency gating
(maximum one active mutating job per agent), and crash recovery reconciliation.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    """Return current ISO 8601 UTC timestamp."""
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    """SQLite-backed manager for persistent asynchronous tool jobs."""

    def __init__(self, db_path: Path):
        """Initialize job manager and ensure database schema is present.

        Args:
            db_path: Path to jobs SQLite database file.
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._processes: Dict[str, subprocess.Popen] = {}
        self._init_db()

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Create tool_jobs table and indexes if not present."""
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_jobs (
                    job_id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL,
                    command TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    pgid INTEGER,
                    exit_code INTEGER,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    log_path TEXT NOT NULL,
                    timeout_seconds INTEGER NOT NULL,
                    max_bytes INTEGER NOT NULL,
                    error_detail TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_agent ON tool_jobs(agent_id, status)")
            conn.commit()

        try:
            self.db_path.chmod(0o660)
        except OSError:
            pass

    def _is_pid_alive(self, pid: Optional[int]) -> bool:
        """Check if an OS process is alive and not a dead zombie."""
        if not pid or pid <= 0:
            return False
        try:
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
                if wpid == pid:
                    return False
            except (ChildProcessError, OSError):
                pass

            os.kill(pid, 0)
            proc_status = Path(f"/proc/{pid}/status")
            if proc_status.exists():
                try:
                    for line in proc_status.read_text().splitlines():
                        if line.startswith("State:"):
                            if "Z" in line or "X" in line:
                                return False
                            break
                except OSError:
                    return False
            return True
        except OSError:
            return False

    def reconcile_stale_jobs(self, agent_id: str) -> int:
        """Reconcile jobs left running from a prior crash or restart to 'unknown'.

        Args:
            agent_id: Agent identifier.

        Returns:
            int: Number of reconciled jobs.
        """
        reconciled = 0
        now_str = utc_now()
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM tool_jobs WHERE agent_id = ? AND status = 'running'",
                (agent_id,),
            )
            running = cursor.fetchall()
            for row in running:
                pid = row["pid"]
                if not self._is_pid_alive(pid):
                    conn.execute(
                        """
                        UPDATE tool_jobs
                        SET status = 'unknown', completed_at = ?,
                            error_detail = 'Interrupted by process restart or crash; state unclear, do not blindly re-execute'
                        WHERE job_id = ?
                        """,
                        (now_str, row["job_id"]),
                    )
                    reconciled += 1
            conn.commit()
        return reconciled

    def start_job(
        self,
        agent_id: str,
        command: str,
        cwd: Path,
        env: Optional[Dict[str, str]] = None,
        timeout_seconds: int = 3600,
        max_bytes: int = 131072,
    ) -> Dict[str, Any]:
        """Start a persistent background tool job with process group isolation.

        Enforces a maximum of one running mutating job per agent.

        Args:
            agent_id: Identifier of the requesting agent.
            command: Shell command line to execute.
            cwd: Working directory for process execution.
            env: Sanitized environment dictionary.
            timeout_seconds: Hard timeout in seconds.
            max_bytes: Output buffer limit in bytes.

        Returns:
            Dict[str, Any]: Job status dictionary.

        Raises:
            ValueError: If an active job is already running for this agent.
        """
        # Concurrency limit check: max 1 active mutating job per agent
        active = self.get_active_job(agent_id)
        if active is not None:
            raise ValueError(f"Active tool job {active['job_id']} is already running; wait or cancel it before starting another.")

        job_id = f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        log_dir = Path(cwd) / "jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{job_id}.log"

        now_str = utc_now()
        log_file = open(log_path, "wb")
        try:
            # Launch in dedicated process group (start_new_session=True)
            process = subprocess.Popen(
                ["bash", "-o", "pipefail", "-c", command],
                cwd=str(cwd),
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self._processes[job_id] = process
            pid = process.pid
            try:
                pgid = os.getpgid(pid)
            except OSError:
                pgid = pid

            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO tool_jobs (
                        job_id, agent_id, command, cwd, status, pid, pgid,
                        exit_code, started_at, completed_at, log_path,
                        timeout_seconds, max_bytes, error_detail
                    ) VALUES (?, ?, ?, ?, 'running', ?, ?, NULL, ?, NULL, ?, ?, ?, NULL)
                    """,
                    (job_id, agent_id, command, str(cwd), pid, pgid, now_str, str(log_path), timeout_seconds, max_bytes),
                )
                conn.commit()
        finally:
            log_file.close()

        return self.get_job(job_id)  # type: ignore

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Fetch single job dictionary by job_id, refreshing status from OS process if running.

        Args:
            job_id: Job identifier.

        Returns:
            Optional[Dict[str, Any]]: Job status dictionary.
        """
        self.poll_job(job_id)
        with self._conn() as conn:
            cursor = conn.execute("SELECT * FROM tool_jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_dict(row)

    def get_active_job(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Return the currently running job for the agent, if any.

        Args:
            agent_id: Agent identifier.

        Returns:
            Optional[Dict[str, Any]]: Active running job or None.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT job_id FROM tool_jobs WHERE agent_id = ? AND status = 'running'",
                (agent_id,),
            )
            rows = cursor.fetchall()
            for r in rows:
                job = self.get_job(r["job_id"])
                if job and job["status"] == "running":
                    return job
        return None

    def list_jobs(self, agent_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        """List recent jobs for an agent.

        Args:
            agent_id: Agent identifier.
            limit: Maximum count to return.

        Returns:
            List[Dict[str, Any]]: Job records ordered by started_at descending.
        """
        with self._conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM tool_jobs WHERE agent_id = ? ORDER BY started_at DESC LIMIT ?",
                (agent_id, limit),
            )
            rows = cursor.fetchall()
            return [self._row_to_dict(r) for r in rows]

    def poll_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Poll running job process status, check timeouts and output boundaries.

        Args:
            job_id: Job identifier.

        Returns:
            Optional[Dict[str, Any]]: Updated job dictionary.
        """
        with self._conn() as conn:
            cursor = conn.execute("SELECT * FROM tool_jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if not row or row["status"] != "running":
                return self._row_to_dict(row) if row else None

            pid = row["pid"]
            pgid = row["pgid"]
            started_at = datetime.fromisoformat(row["started_at"])
            elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
            now_str = utc_now()

            # Check hard timeout
            if elapsed >= row["timeout_seconds"]:
                self._terminate_process_group(pgid, pid)
                conn.execute(
                    """
                    UPDATE tool_jobs
                    SET status = 'timeout', exit_code = -9, completed_at = ?,
                        error_detail = 'Job exceeded timeout limit'
                    WHERE job_id = ?
                    """,
                    (now_str, job_id),
                )
                conn.commit()
                cursor = conn.execute("SELECT * FROM tool_jobs WHERE job_id = ?", (job_id,))
                return self._row_to_dict(cursor.fetchone())

            # Check if process has finished
            try:
                waited_pid, status = os.waitpid(pid, os.WNOHANG)
                if waited_pid == pid:
                    exit_code = os.waitstatus_to_exitcode(status)
                    new_status = "completed" if exit_code == 0 else "failed"
                    conn.execute(
                        """
                        UPDATE tool_jobs
                        SET status = ?, exit_code = ?, completed_at = ?
                        WHERE job_id = ?
                        """,
                        (new_status, exit_code, now_str, job_id),
                    )
                    conn.commit()
            except ChildProcessError:
                # Process already reaped or non-child; check liveness
                if not self._is_pid_alive(pid):
                    conn.execute(
                        """
                        UPDATE tool_jobs
                        SET status = 'completed', exit_code = 0, completed_at = ?
                        WHERE job_id = ?
                        """,
                        (now_str, job_id),
                    )
                    conn.commit()

            cursor = conn.execute("SELECT * FROM tool_jobs WHERE job_id = ?", (job_id,))
            return self._row_to_dict(cursor.fetchone())

    def cancel_job(self, job_id: str, reason: str = "Cancelled by user/agent") -> Optional[Dict[str, Any]]:
        """Cancel a running job by terminating its entire process group.

        Args:
            job_id: Job identifier.
            reason: Cancellation reason.

        Returns:
            Optional[Dict[str, Any]]: Updated job dictionary.
        """
        now_str = utc_now()
        with self._conn() as conn:
            cursor = conn.execute("SELECT * FROM tool_jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if not row:
                return None
            if row["status"] != "running":
                return self._row_to_dict(row)

            self._terminate_process_group(row["pgid"], row["pid"])
            proc = self._processes.get(job_id)
            if proc:
                try:
                    proc.wait(timeout=0.2)
                except Exception:
                    pass
            conn.execute(
                """
                UPDATE tool_jobs
                SET status = 'cancelled', exit_code = -15, completed_at = ?,
                    error_detail = ?
                WHERE job_id = ?
                """,
                (now_str, reason, job_id),
            )
            conn.commit()

        return self.get_job(job_id)

    def _terminate_process_group(self, pgid: Optional[int], pid: Optional[int]) -> None:
        """Send termination signals to process group, escalating from SIGTERM to SIGKILL."""
        target = pgid if (pgid and pgid > 0) else pid
        if not target or target <= 0:
            return
        try:
            os.killpg(target, signal.SIGTERM)
            time.sleep(0.05)
            if self._is_pid_alive(pid):
                os.killpg(target, signal.SIGKILL)
        except OSError:
            try:
                if pid:
                    os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if pid:
            try:
                os.waitpid(pid, os.WNOHANG)
            except (OSError, ChildProcessError):
                pass

    def get_job_output(self, job_id: str, max_chars: int = 4000) -> str:
        """Read recent output log excerpt from disk.

        Args:
            job_id: Job identifier.
            max_chars: Maximum characters to return from end of log.

        Returns:
            str: Log content excerpt.
        """
        with self._conn() as conn:
            cursor = conn.execute("SELECT log_path FROM tool_jobs WHERE job_id = ?", (job_id,))
            row = cursor.fetchone()
            if not row:
                return ""
            log_path = Path(row["log_path"])
            if not log_path.exists():
                return ""
            try:
                content = log_path.read_text(encoding="utf-8", errors="replace")
                return content[-max_chars:]
            except OSError:
                return ""

    def _row_to_dict(self, r: sqlite3.Row) -> Dict[str, Any]:
        return {
            "job_id": r["job_id"],
            "agent_id": r["agent_id"],
            "command": r["command"],
            "cwd": r["cwd"],
            "status": r["status"],
            "pid": r["pid"],
            "pgid": r["pgid"],
            "exit_code": r["exit_code"],
            "started_at": r["started_at"],
            "completed_at": r["completed_at"],
            "log_path": r["log_path"],
            "timeout_seconds": r["timeout_seconds"],
            "max_bytes": r["max_bytes"],
            "error_detail": r["error_detail"],
        }

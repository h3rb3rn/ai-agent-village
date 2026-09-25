"""Unit tests for P12: Persistent background tool jobs, process isolation, and event orientation."""

import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from village.jobs import JobManager
from web.runtime import Resident


class TestJobsAndEventOrientation(unittest.TestCase):
    """Test suite covering persistent background tool jobs, process groups, and crash reconciliation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "jobs.sqlite3"
        self.manager = JobManager(self.db_path)
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        for proc in list(self.manager._processes.values()):
            try:
                proc.kill()
                proc.wait(timeout=0.1)
            except Exception:
                pass
        self.temp_dir.cleanup()

    def test_job_lifecycle_and_pgid_isolation(self):
        """Verify normal job execution, output capture, and process group isolation."""
        job = self.manager.start_job(
            agent_id="agent_alpha",
            command="sleep 0.2; echo 'Job output line 1'; echo 'Job output line 2'",
            cwd=self.work_dir,
            timeout_seconds=10,
        )
        job_id = job["job_id"]
        self.assertEqual(job["status"], "running")
        self.assertIsNotNone(job["pgid"])

        # Wait for completion
        for _ in range(50):
            time.sleep(0.05)
            polled = self.manager.poll_job(job_id)
            if polled and polled["status"] in ("completed", "failed"):
                break

        final_job = self.manager.get_job(job_id)
        self.assertIsNotNone(final_job)
        self.assertEqual(final_job["status"], "completed")
        self.assertEqual(final_job["exit_code"], 0)

        output = self.manager.get_job_output(job_id)
        self.assertIn("Job output line 1", output)
        self.assertIn("Job output line 2", output)

    def test_job_concurrency_limit_one_mutating_job(self):
        """Enforce maximum of one active mutating background job per agent."""
        job1 = self.manager.start_job(
            agent_id="agent_alpha",
            command="sleep 10",
            cwd=self.work_dir,
            timeout_seconds=30,
        )
        self.assertEqual(job1["status"], "running")

        # Second job attempt for same agent must fail with ValueError
        with self.assertRaises(ValueError) as ctx:
            self.manager.start_job(
                agent_id="agent_alpha",
                command="echo 'second'",
                cwd=self.work_dir,
            )
        self.assertIn("already running", str(ctx.exception))

        # Cancelling the active job frees the lane
        self.manager.cancel_job(job1["job_id"])
        cancelled_job = self.manager.get_job(job1["job_id"])
        self.assertEqual(cancelled_job["status"], "cancelled")

        # Now agent_alpha can start a new job
        job2 = self.manager.start_job(
            agent_id="agent_alpha",
            command="sleep 10",
            cwd=self.work_dir,
        )
        self.assertEqual(job2["status"], "running")
        self.manager.cancel_job(job2["job_id"])

    def test_agent_a_builds_long_agent_b_operates(self):
        """Agent A running a long build does not block Agent B from starting work."""
        job_a = self.manager.start_job(
            agent_id="agent_a",
            command="sleep 10",
            cwd=self.work_dir,
            timeout_seconds=30,
        )
        self.assertEqual(job_a["status"], "running")

        # Agent B can start a job concurrently
        job_b = self.manager.start_job(
            agent_id="agent_b",
            command="sleep 5; echo 'Agent B fast task'",
            cwd=self.work_dir,
            timeout_seconds=10,
        )
        self.assertEqual(job_b["status"], "running")

        # Cleanup
        self.manager.cancel_job(job_a["job_id"])
        self.manager.cancel_job(job_b["job_id"])

    def test_crash_reconciliation_marks_unknown(self):
        """Verify incomplete jobs from crash/restart are marked unknown, not blindly rerun."""
        dead_pid = 9999999  # Guaranteed non-existent PID
        with self.manager._conn() as conn:
            conn.execute(
                """
                INSERT INTO tool_jobs (
                    job_id, agent_id, command, cwd, status, pid, pgid,
                    exit_code, started_at, completed_at, log_path,
                    timeout_seconds, max_bytes, error_detail
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, NULL, '2026-09-24T12:00:00+00:00', NULL, ?, 3600, 131072, NULL)
                """,
                ("job_stale_123", "agent_alpha", "make deploy", str(self.work_dir), dead_pid, dead_pid, str(self.work_dir / "stale.log")),
            )
            conn.commit()

        reconciled = self.manager.reconcile_stale_jobs("agent_alpha")
        self.assertEqual(reconciled, 1)

        job = self.manager.get_job("job_stale_123")
        self.assertEqual(job["status"], "unknown")
        self.assertIn("Interrupted by process restart or crash", job["error_detail"])

    def test_cancellation_terminates_process_group(self):
        """Verify cancelling a job terminates the process group and marks status cancelled."""
        job = self.manager.start_job(
            agent_id="agent_alpha",
            command="sleep 60",
            cwd=self.work_dir,
            timeout_seconds=120,
        )
        job_id = job["job_id"]
        pid = job["pid"]

        # Cancel the job
        self.manager.cancel_job(job_id, reason="Operator stop")
        polled = self.manager.get_job(job_id)
        self.assertEqual(polled["status"], "cancelled")
        self.assertEqual(polled["exit_code"], -15)

        # Confirm OS process is dead
        time.sleep(0.1)
        self.assertFalse(self.manager._is_pid_alive(pid))

    def test_timeout_enforcement(self):
        """Verify hard timeout stops runaway process and updates status to timeout."""
        job = self.manager.start_job(
            agent_id="agent_alpha",
            command="sleep 10",
            cwd=self.work_dir,
            timeout_seconds=1,
        )
        job_id = job["job_id"]
        time.sleep(1.2)

        polled = self.manager.poll_job(job_id)
        self.assertEqual(polled["status"], "timeout")
        self.assertEqual(polled["exit_code"], -9)

    def test_runtime_paused_blocks_execution(self):
        """Verify guard blocks command/job execution when paused ('pausiert startet nichts')."""
        env = {
            "AGENT_ID": "agent_alpha",
            "AGENT_NAME": "alpha",
            "AGENT_ROLE": "builder",
            "VILLAGE_ROOT": str(self.root),
            "VILLAGE_PAUSE_MARKER": str(self.root / "paused"),
            "AGENT_IDENTITY_PROMPT": str(self.root / "identity.txt"),
            "OLLAMA_MODEL": "qwen2.5:7b",
        }
        (self.root / "identity.txt").write_text("Test agent identity")
        (self.root / "board").mkdir(parents=True, exist_ok=True)
        (self.root / "paused").write_text('{"mode":"drain","reason":"maintenance"}')

        resident = Resident(env=env)
        # Attempt execute_bash while paused
        allowed_bash = resident.guard("execute_bash", {"command": "echo 'hack'"})
        self.assertFalse(allowed_bash)
        last_res = resident.state.get("last_result", {})
        self.assertIn("pausiert startet nichts", last_res.get("result", ""))

        # Attempt start_job while paused
        allowed_job = resident.guard("start_job", {"command": "make build"})
        self.assertFalse(allowed_job)
        last_res = resident.state.get("last_result", {})
        self.assertIn("pausiert startet nichts", last_res.get("result", ""))

    def test_event_wakeup_on_inbox_message(self):
        """Verify unacknowledged inbox message causes immediate early wakeup from sleep."""
        env = {
            "AGENT_ID": "agent_alpha",
            "AGENT_NAME": "alpha",
            "AGENT_ROLE": "builder",
            "VILLAGE_ROOT": str(self.root),
            "AGENT_IDENTITY_PROMPT": str(self.root / "identity.txt"),
            "OLLAMA_MODEL": "qwen2.5:7b",
        }
        (self.root / "identity.txt").write_text("Test agent identity")
        (self.root / "board").mkdir(parents=True, exist_ok=True)
        resident = Resident(env=env)

        # Mock cycle and post an inbox message shortly after run starts
        cycle_count = 0
        def fake_cycle():
            nonlocal cycle_count
            cycle_count += 1
            if cycle_count == 1:
                # Post direct message to agent_alpha
                resident.tasks.store.post_inbox_message(
                    source="direct",
                    sender="agent_beta",
                    recipient="agent_alpha",
                    content="Wake up and check build!",
                )
            elif cycle_count >= 2:
                resident.stopping = True

        resident.cycle = fake_cycle
        # Set compute_cycle_delay to high delay (e.g. 100 seconds)
        resident.compute_cycle_delay = lambda: 100

        start_time = time.monotonic()
        resident.run()
        elapsed = time.monotonic() - start_time

        # Should wake up in ~1 second, not wait 100 seconds
        self.assertLess(elapsed, 5.0)
        self.assertGreaterEqual(cycle_count, 2)


if __name__ == "__main__":
    unittest.main()

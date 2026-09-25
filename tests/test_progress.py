"""Unit and integration tests for P10: Work Progress without Claim Loops."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from village.coordinator import CoordinationStore, is_weak_criterion
from web.runtime import Resident


class TestWorkProgressAndClaimLoops(unittest.TestCase):
    """Test suite covering task progress, lease renewals, revision history, and stale worker rejection."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-progress-test-"))
        self.board = self.tmp / "board"
        self.board.mkdir(parents=True, exist_ok=True)
        self.store = CoordinationStore(self.board / "coordination.sqlite3", self.board)
        self.worker1 = "agent_worker_1"
        self.worker2 = "agent_worker_2"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idempotent_claim_renewals_preserve_revision(self):
        """Verify that repeating a claim by the same owner renews the lease idempotently without bumping revision."""
        task = self.store.operate("author", {
            "action": "create",
            "title": "Build Container Image",
            "success_criterion": "OCI container image built and tagged",
        })
        task_id = task["id"]
        self.assertEqual(task["revision"], 1)

        # First claim by worker1 -> bumps revision to 2
        claimed = self.store.operate(self.worker1, {
            "action": "claim",
            "task_id": task_id,
        })
        self.assertEqual(claimed["status"], "active")
        self.assertEqual(claimed["owner"], self.worker1)
        self.assertEqual(claimed["revision"], 2)
        initial_lease = claimed["lease_until"]

        # Small sleep to observe lease time renewal
        time.sleep(0.01)

        # Idempotent re-claim by same worker1 -> lease renewed, revision remains 2
        reclaimed = self.store.operate(self.worker1, {
            "action": "claim",
            "task_id": task_id,
        })
        self.assertEqual(reclaimed["owner"], self.worker1)
        self.assertEqual(reclaimed["revision"], 2, "Duplicate claim by same owner must not increment revision")
        self.assertGreaterEqual(reclaimed["lease_until"], initial_lease)

        # Verify revision history has only create (1) and initial claim (2)
        history = self.store.get_task_history(task_id)
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["action"], "create")
        self.assertEqual(history[1]["action"], "claim")

    def test_claim_after_lease_expiry(self):
        """Verify that an expired lease allows another worker to take over with an incremented revision."""
        task = self.store.operate("author", {
            "action": "create",
            "title": "Compile Binary",
            "success_criterion": "Compiles with zero compiler warnings",
        })
        task_id = task["id"]

        # Worker 1 claims task
        self.store.operate(self.worker1, {"action": "claim", "task_id": task_id})

        # Manually expire lease in SQLite
        with self.store._conn() as conn:
            conn.execute("UPDATE tasks SET lease_until = ? WHERE id = ?", (time.time() - 100.0, task_id))
            conn.commit()

        # Worker 2 claims expired task -> takeover succeeds
        takeover = self.store.operate(self.worker2, {"action": "claim", "task_id": task_id})
        self.assertEqual(takeover["owner"], self.worker2)
        self.assertEqual(takeover["status"], "active")
        self.assertEqual(takeover["revision"], 3)

        history = self.store.get_task_history(task_id)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[2]["actor"], self.worker2)

    def test_progress_recording_and_revision_history(self):
        """Verify recording progress updates finding, next step, blockers, and artifacts."""
        task = self.store.operate(self.worker1, {
            "action": "create",
            "title": "Analyze telemetry anomalies",
            "success_criterion": "Root cause identified and documented",
            "goal": "Explain CPU spikes during inference burst",
        })
        task_id = task["id"]
        self.store.operate(self.worker1, {"action": "claim", "task_id": task_id})

        # Record first progress milestone
        p1 = self.store.operate(self.worker1, {
            "action": "progress",
            "task_id": task_id,
            "last_finding": "Spike correlates with context window resizing",
            "next_step": "Profile memory allocation per generation",
            "blockers": "Need temporary write access to telemetry cache",
            "artifact_refs": ["/tmp/profile_01.csv"],
        })
        self.assertEqual(p1["revision"], 3)
        self.assertEqual(p1["last_finding"], "Spike correlates with context window resizing")
        self.assertEqual(p1["next_step"], "Profile memory allocation per generation")
        self.assertEqual(p1["blockers"], "Need temporary write access to telemetry cache")
        self.assertIn("/tmp/profile_01.csv", p1["artifact_refs"])

        # Non-owner cannot record progress
        with self.assertRaises(ValueError):
            self.store.operate(self.worker2, {
                "action": "progress",
                "task_id": task_id,
                "last_finding": "Unauthorized update",
            })

    def test_stale_worker_rejection(self):
        """Verify that a worker operating against an outdated revision is rejected."""
        task = self.store.operate(self.worker1, {
            "action": "create",
            "title": "Database Schema Optimization",
            "success_criterion": "Indexes added for slow queries",
        })
        task_id = task["id"]
        self.store.operate(self.worker1, {"action": "claim", "task_id": task_id})

        # Task is at revision 2. Progress update advances it to revision 3.
        self.store.operate(self.worker1, {
            "action": "progress",
            "task_id": task_id,
            "last_finding": "Index on status added",
        })

        # Worker attempts to complete with stale expected_revision=2
        with self.assertRaises(ValueError) as ctx:
            self.store.operate(self.worker1, {
                "action": "complete",
                "task_id": task_id,
                "expected_revision": 2,
                "evidence": "proof",
            })
        self.assertIn("stale task revision", str(ctx.exception))

        # With correct expected_revision=3, complete succeeds
        completed = self.store.operate(self.worker1, {
            "action": "complete",
            "task_id": task_id,
            "expected_revision": 3,
            "evidence": "proof",
        })
        self.assertEqual(completed["status"], "complete")
        self.assertEqual(completed["revision"], 4)

    def test_weak_criterion_evaluation_and_refinement(self):
        """Verify detection of weak/placeholder criteria and subsequent refinement."""
        self.assertTrue(is_weak_criterion("tbd"))
        self.assertTrue(is_weak_criterion("short"))
        self.assertTrue(is_weak_criterion("Imported from legacy"))
        self.assertFalse(is_weak_criterion("Verify all unit tests pass with zero regression"))

        # Create task with weak criterion
        task = self.store.operate(self.worker1, {
            "action": "create",
            "title": "Legacy ported task",
            "success_criterion": "tbd",
        })
        self.assertTrue(task["weak_criterion"])

        # Claim and refine criterion in progress update
        self.store.operate(self.worker1, {"action": "claim", "task_id": task["id"]})
        updated = self.store.operate(self.worker1, {
            "action": "progress",
            "task_id": task["id"],
            "success_criterion": "Deterministic output verified across 100 test runs",
        })
        self.assertFalse(updated["weak_criterion"])

    def test_yield_and_reclaim_lifecycle(self):
        """Verify voluntary yield frees the task for peer takeover."""
        task = self.store.operate(self.worker1, {
            "action": "create",
            "title": "Refactor parser",
            "success_criterion": "Passes all parser edge cases",
        })
        task_id = task["id"]
        self.store.operate(self.worker1, {"action": "claim", "task_id": task_id})

        # Worker 1 yields task due to blocker
        yielded = self.store.operate(self.worker1, {
            "action": "yield",
            "task_id": task_id,
            "evidence": "Hit parser complexity limit, handing off",
            "blockers": "Need AST specialist",
        })
        self.assertEqual(yielded["status"], "open")
        self.assertIsNone(yielded["owner"])
        self.assertEqual(yielded["lease_until"], 0.0)

        # Worker 2 can now claim without waiting
        claimed2 = self.store.operate(self.worker2, {"action": "claim", "task_id": task_id})
        self.assertEqual(claimed2["owner"], self.worker2)
        self.assertEqual(claimed2["status"], "active")

    def test_snapshot_prioritizes_own_work_and_blocker_guidance(self):
        """Verify snapshot places own active task first and provides blocker guidance."""
        root = self.tmp / "village"
        root.mkdir(parents=True, exist_ok=True)
        board = root / "board"
        board.mkdir(parents=True, exist_ok=True)

        store = CoordinationStore(board / "coordination.sqlite3", board)
        # Create peer task
        t_peer = store.operate("peer_agent", {
            "action": "create",
            "title": "Peer Task",
            "success_criterion": "Peer criterion that is quite detailed",
        })
        # Create own task with blockers
        t_own = store.operate("agent_focus", {
            "action": "create",
            "title": "My Focused Task",
            "success_criterion": "Detailed criterion for own task",
            "blockers": "Missing dependency package",
        })
        store.operate("agent_focus", {"action": "claim", "task_id": t_own["id"]})

        env = {
            "AGENT_ID": "agent_focus",
            "AGENT_NAME": "focus",
            "AGENT_ROLE": "engineer",
            "VILLAGE_ROOT": str(root),
            "OLLAMA_MODEL": "test-model",
            "VILLAGE_PAUSE_MARKER": str(self.tmp / "paused"),
        }
        agent = Resident(env=env)

        with patch.object(agent, "memory", return_value={"items": []}):
            data = json.loads(agent.snapshot())

        # Own active task is populated and first in projects list
        self.assertIsNotNone(data.get("own_active_task"))
        self.assertEqual(data["own_active_task"]["id"], t_own["id"])
        self.assertEqual(data["projects"][0]["id"], t_own["id"])

        # Blocker guidance is present advising independent steps rather than waiting for King
        self.assertIn("task_blocker_guidance", data)
        self.assertIn("Missing dependency package", data["task_blocker_guidance"])


if __name__ == "__main__":
    unittest.main()

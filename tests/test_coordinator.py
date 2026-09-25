"""Unit tests for P08: Transactional Coordination Store and Board Projection."""

import concurrent.futures
import json
import os
import tempfile
import unittest
from pathlib import Path

from village.coordinator import CoordinationStore


class TestCoordinationStore(unittest.TestCase):
    """Test suite for SQLite coordinator, concurrent task claims, and corruption quarantine."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.board = self.root / "board"
        self.board.mkdir(parents=True)
        self.db_path = self.board / "coordination.sqlite3"
        self.store = CoordinationStore(self.db_path, self.board)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_schema_migrations_applied(self) -> None:
        """Verify schema migrations table is created and version 1 is recorded."""
        with self.store._conn() as conn:
            cursor = conn.execute("SELECT version FROM schema_migrations")
            versions = [r["version"] for r in cursor.fetchall()]
            self.assertIn(1, versions)

    def test_concurrent_claims_race_condition_safe(self) -> None:
        """Verify that when two concurrent workers claim a task, exactly one succeeds."""
        # Create an open task
        item = self.store.operate("author_agent", {
            "action": "create",
            "title": "Parallel Build Pipeline",
            "success_criterion": "Must execute concurrently without collision",
        })
        task_id = item["id"]

        results = []
        errors = []

        def worker_claim(actor_name: str):
            try:
                # Use separate store instance connecting to same SQLite db
                local_store = CoordinationStore(self.db_path, self.board)
                res = local_store.operate(actor_name, {"action": "claim", "task_id": task_id})
                results.append(res)
            except ValueError as exc:
                errors.append(str(exc))

        # Launch 2 simultaneous workers racing to claim the same task
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(worker_claim, "worker_1")
            fut2 = executor.submit(worker_claim, "worker_2")
            fut1.result()
            fut2.result()

        # Exactly ONE claim must succeed
        self.assertEqual(len(results), 1, "Exactly one worker must win the claim")
        # Exactly ONE error must be raised
        self.assertEqual(len(errors), 1, "The losing worker must receive a claim error")
        self.assertIn("already claimed", errors[0])

        # Verify winning owner in store
        task = self.store.get_task(task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "active")
        self.assertIn(task["owner"], ["worker_1", "worker_2"])

    def test_idempotent_legacy_import(self) -> None:
        """Verify importing legacy tasks multiple times is idempotent."""
        legacy_file = self.board / "legacy-work-items.json"
        legacy_data = [
            {
                "id": "t100",
                "title": "Legacy Item 1",
                "success_criterion": "Criterion 1",
                "author": "founder",
                "status": "open",
            },
            {
                "id": "t200",
                "title": "Legacy Item 2",
                "success_criterion": "Criterion 2",
                "author": "founder",
                "status": "complete",
                "evidence": "proof",
            },
        ]
        legacy_file.write_text(json.dumps(legacy_data), encoding="utf-8")

        # First import
        count1, err1 = self.store.import_legacy_tasks(legacy_file)
        self.assertEqual(count1, 2)
        self.assertIsNone(err1)

        # Second import (must be idempotent)
        count2, err2 = self.store.import_legacy_tasks(legacy_file)
        self.assertEqual(count2, 0)
        self.assertIsNone(err2)

        # Verify total count in store is still 2
        tasks = self.store.list_tasks()
        self.assertEqual(len(tasks), 2)

    def test_corrupted_json_quarantine(self) -> None:
        """Verify corrupted JSON file is moved to quarantine instead of being clobbered."""
        legacy_file = self.board / "broken-tasks.json"
        legacy_file.write_text("{broken json invalid syntax", encoding="utf-8")

        count, err = self.store.import_legacy_tasks(legacy_file)
        self.assertEqual(count, 0)
        self.assertIsNotNone(err)
        self.assertIn("Quarantined", err)

        # Original file should be gone (moved)
        self.assertFalse(legacy_file.exists())

        # Quarantined file should exist
        quarantined_files = list(self.board.glob("corrupted-broken-tasks.json.*"))
        self.assertEqual(len(quarantined_files), 1)

    def test_board_projection_sync(self) -> None:
        """Verify that work-items.json is updated automatically and has 0660 mode."""
        self.store.operate("builder", {
            "action": "create",
            "title": "Projection Verification Task",
            "success_criterion": "Ensure file is updated",
        })

        proj_file = self.store.projection_path
        self.assertTrue(proj_file.exists())
        self.assertEqual(proj_file.stat().st_mode & 0o777, 0o660)

        data = json.loads(proj_file.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["title"], "Projection Verification Task")

    def test_task_limits_and_validation(self) -> None:
        """Verify open task limit (64) and parameter constraints."""
        # Create 64 tasks
        for i in range(64):
            self.store.operate("builder", {
                "action": "create",
                "title": f"Task {i:03d}",
                "success_criterion": f"Criterion {i}",
            })

        # 65th task must fail
        with self.assertRaises(ValueError) as ctx:
            self.store.operate("builder", {
                "action": "create",
                "title": "Task 065",
                "success_criterion": "Should exceed limit",
            })
        self.assertIn("64 open projects already exist", str(ctx.exception))

    def test_file_permissions_restrictive(self) -> None:
        """Verify that coordination.sqlite3 has restrictive permissions (0660)."""
        mode = self.db_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o660)


if __name__ == "__main__":
    unittest.main()

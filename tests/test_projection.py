"""Unit tests for P15: Durable projection queue (Transactional Outbox) and error isolation."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from memory import gateway
from memory.projection import InMemorySink, ProjectionWorker, record_outbox_event


class TestMemoryProjectionQueue(unittest.TestCase):
    """Test suite covering transactional outbox, worker processing, error isolation, and rebuild."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "memory.sqlite3"
        gateway.DB = self.db_path
        self.worker = ProjectionWorker(self.db_path)
        self.sink_a = InMemorySink("sink_a")
        self.sink_b = InMemorySink("sink_b")
        self.worker.register_backend(self.sink_a)
        self.worker.register_backend(self.sink_b)

    def tearDown(self):
        self.tmp.cleanup()

    def test_crash_between_commit_and_projection(self):
        """Outbox events committed in primary transaction survive simulated crash before worker runs."""
        # 1. Write memory to authoritative database via gateway db()
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item = {
            "id": "mem_crash_1",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "artisan",
            "scope": "shared",
            "kind": "observation",
            "content": "Surviving crash payload",
            "source_event": "evt-crash",
            "confidence": 0.95,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "crash_test_k1",
        }
        conn.execute(
            """
            INSERT INTO memories (
                id, created_at, updated_at, agent, scope, kind,
                content, source_event, confidence, expires_at,
                metadata, idempotency_key
            ) VALUES (
                :id, :created_at, :updated_at, :agent, :scope, :kind,
                :content, :source_event, :confidence, :expires_at,
                :metadata, :idempotency_key
            )
            """,
            item,
        )
        record_outbox_event(conn, "mem_crash_1", "upsert", item)
        conn.commit()
        conn.close()

        # Simulated crash: worker was not running at commit time. Sinks are currently empty.
        self.assertNotIn("mem_crash_1", self.sink_a.records)

        # 2. Worker starts/restarts and processes pending queue
        counts = self.worker.process_all()
        self.assertEqual(counts["sink_a"], 1)
        self.assertEqual(counts["sink_b"], 1)

        # Confirm data arrived in sink
        self.assertIn("mem_crash_1", self.sink_a.records)
        self.assertEqual(self.sink_a.records["mem_crash_1"]["content"], "Surviving crash payload")

    def test_duplicate_delivery_and_idempotent_replays(self):
        """Replaying outbox events multiple times does not corrupt or duplicate sink state."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item = {
            "id": "mem_idemp_1",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "builder",
            "scope": "shared",
            "kind": "plan",
            "content": "Idempotent delivery check",
            "source_event": "",
            "confidence": 0.8,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "idemp_k2",
        }
        conn.execute(
            """
            INSERT INTO memories (
                id, created_at, updated_at, agent, scope, kind,
                content, source_event, confidence, expires_at,
                metadata, idempotency_key
            ) VALUES (
                :id, :created_at, :updated_at, :agent, :scope, :kind,
                :content, :source_event, :confidence, :expires_at,
                :metadata, :idempotency_key
            )
            """,
            item,
        )
        record_outbox_event(conn, "mem_idemp_1", "upsert", item)
        conn.commit()
        conn.close()

        # First run
        self.worker.process_all()
        self.assertEqual(len(self.sink_a.records), 1)

        # Force replay by resetting last_sequence_id back to 0
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("UPDATE projection_states SET last_sequence_id = 0 WHERE backend = 'sink_a'")
            conn.commit()

        # Re-run projection
        replay_count = self.worker.process_backend("sink_a")
        self.assertEqual(replay_count, 1)
        # Sinks still contain exactly 1 entry with updated content
        self.assertEqual(len(self.sink_a.records), 1)
        self.assertEqual(self.sink_a.records["mem_idemp_1"]["id"], "mem_idemp_1")

    def test_backend_offline_error_isolation(self):
        """Offline backend fails gracefully into backoff without blocking primary DB or other sinks."""
        # Configure sink_b to fail on writes
        failing_sink = InMemorySink("failing_sink", fail_on_write=True)
        self.worker.register_backend(failing_sink)

        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item = {
            "id": "mem_iso_1",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Fault isolation invariant",
            "source_event": "",
            "confidence": 1.0,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "iso_k3",
        }
        conn.execute(
            """
            INSERT INTO memories (
                id, created_at, updated_at, agent, scope, kind,
                content, source_event, confidence, expires_at,
                metadata, idempotency_key
            ) VALUES (
                :id, :created_at, :updated_at, :agent, :scope, :kind,
                :content, :source_event, :confidence, :expires_at,
                :metadata, :idempotency_key
            )
            """,
            item,
        )
        record_outbox_event(conn, "mem_iso_1", "upsert", item)
        conn.commit()
        conn.close()

        # Run worker across all backends
        counts = self.worker.process_all()

        # Healthy sink_a succeeds
        self.assertEqual(counts["sink_a"], 1)
        self.assertIn("mem_iso_1", self.sink_a.records)

        # Failing sink reports 0 processed, enters degraded state with error count
        self.assertEqual(counts["failing_sink"], 0)
        status = self.worker.get_status()
        self.assertEqual(status["backends"]["failing_sink"]["status"], "degraded")
        self.assertGreaterEqual(status["backends"]["failing_sink"]["error_count"], 1)
        self.assertIn("offline", status["backends"]["failing_sink"]["last_error"])

        # Primary SQLite remains fully operational and writeable
        conn = gateway.db()
        cursor = conn.execute("SELECT count(*) FROM memories")
        self.assertEqual(cursor.fetchone()[0], 1)
        conn.close()

    def test_scope_change_private_shared_private(self):
        """Scope change from shared to private triggers tombstone in projection."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item = {
            "id": "mem_scope_1",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Initially shared finding",
            "source_event": "",
            "confidence": 0.9,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "scope_k1",
        }
        conn.execute(
            """
            INSERT INTO memories (
                id, created_at, updated_at, agent, scope, kind,
                content, source_event, confidence, expires_at,
                metadata, idempotency_key
            ) VALUES (
                :id, :created_at, :updated_at, :agent, :scope, :kind,
                :content, :source_event, :confidence, :expires_at,
                :metadata, :idempotency_key
            )
            """,
            item,
        )
        record_outbox_event(conn, "mem_scope_1", "upsert", item)
        conn.commit()

        # Project initial shared state
        self.worker.process_all()
        self.assertIn("mem_scope_1", self.sink_a.records)

        # Now change scope to private
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE memories SET scope = 'private', updated_at = '2026-09-25T01:00:00Z' WHERE id = 'mem_scope_1'")
        tombstone = {
            "id": "mem_scope_1",
            "agent": "scholar",
            "scope": "private",
            "reason": "scope_changed_to_private",
        }
        record_outbox_event(conn, "mem_scope_1", "scope_change", tombstone)
        conn.commit()
        conn.close()

        # Project scope change: sink must tombstone / remove from shared index
        self.worker.process_all()
        self.assertNotIn("mem_scope_1", self.sink_a.records)
        self.assertIn("mem_scope_1", self.sink_a.tombstones)

    def test_delete_with_tombstone(self):
        """Deleting memory in SQLite generates outbox tombstone and deletes from sink."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item = {
            "id": "mem_del_1",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "To be deleted",
            "source_event": "",
            "confidence": 0.5,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "del_k1",
        }
        conn.execute(
            """
            INSERT INTO memories (
                id, created_at, updated_at, agent, scope, kind,
                content, source_event, confidence, expires_at,
                metadata, idempotency_key
            ) VALUES (
                :id, :created_at, :updated_at, :agent, :scope, :kind,
                :content, :source_event, :confidence, :expires_at,
                :metadata, :idempotency_key
            )
            """,
            item,
        )
        record_outbox_event(conn, "mem_del_1", "upsert", item)
        conn.commit()

        self.worker.process_all()
        self.assertIn("mem_del_1", self.sink_a.records)

        # Delete from primary
        conn.execute("BEGIN IMMEDIATE")
        tombstone = {"id": "mem_del_1", "agent": "scholar", "deleted_at": "2026-09-25T02:00:00Z"}
        record_outbox_event(conn, "mem_del_1", "delete", tombstone)
        conn.execute("DELETE FROM memories WHERE id = 'mem_del_1'")
        conn.commit()
        conn.close()

        self.worker.process_all()
        self.assertNotIn("mem_del_1", self.sink_a.records)
        self.assertIn("mem_del_1", self.sink_a.tombstones)

    def test_complete_index_rebuild(self):
        """Full rebuild resets sink and re-populates active memories without erasing SQLite."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        # 2 active memories, 1 expired memory
        active1 = {
            "id": "rebuild_1", "created_at": "2026-09-25T00:00:00Z", "updated_at": "2026-09-25T00:00:00Z",
            "agent": "artisan", "scope": "shared", "kind": "observation", "content": "Active note 1",
            "source_event": "", "confidence": 0.9, "expires_at": None, "metadata": "{}", "idempotency_key": "rb1",
        }
        active2 = {
            "id": "rebuild_2", "created_at": "2026-09-25T00:00:00Z", "updated_at": "2026-09-25T00:00:00Z",
            "agent": "artisan", "scope": "shared", "kind": "observation", "content": "Active note 2",
            "source_event": "", "confidence": 0.9, "expires_at": None, "metadata": "{}", "idempotency_key": "rb2",
        }
        expired = {
            "id": "rebuild_exp", "created_at": "2020-01-01T00:00:00Z", "updated_at": "2020-01-01T00:00:00Z",
            "agent": "artisan", "scope": "shared", "kind": "observation", "content": "Expired note",
            "source_event": "", "confidence": 0.9, "expires_at": "2020-01-02T00:00:00Z", "metadata": "{}", "idempotency_key": "rb_exp",
        }
        for item in (active1, active2, expired):
            conn.execute(
                """
                INSERT INTO memories (
                    id, created_at, updated_at, agent, scope, kind,
                    content, source_event, confidence, expires_at,
                    metadata, idempotency_key
                ) VALUES (
                    :id, :created_at, :updated_at, :agent, :scope, :kind,
                    :content, :source_event, :confidence, :expires_at,
                    :metadata, :idempotency_key
                )
                """,
                item,
            )
            record_outbox_event(conn, item["id"], "upsert", item)
        conn.commit()
        conn.close()

        # Run complete rebuild on sink_a
        rebuilt = self.worker.rebuild("sink_a")
        # Only the 2 active non-expired memories should be re-projected
        self.assertEqual(rebuilt, 2)
        self.assertIn("rebuild_1", self.sink_a.records)
        self.assertIn("rebuild_2", self.sink_a.records)
        self.assertNotIn("rebuild_exp", self.sink_a.records)

        # Primary SQLite records remain intact
        conn = gateway.db()
        total_in_db = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        self.assertEqual(total_in_db, 3)
        conn.close()

    def test_rollback_worker_shutdown_preserves_primary_data(self):
        """Shutting down the worker leaves primary SQLite database completely functional."""
        # Unregister/kill worker
        del self.worker
        conn = gateway.db()
        cursor = conn.execute("SELECT count(*) FROM memories")
        self.assertIsNotNone(cursor)
        conn.close()


if __name__ == "__main__":
    unittest.main()

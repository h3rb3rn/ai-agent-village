"""Unit tests for P17: Neo4j graph projection adapter, schema, provenance, and fallback."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from memory import gateway
from memory.neo4j_adapter import (
    InMemoryGraphStore,
    Neo4jConnectionError,
    Neo4jError,
    Neo4jProjectionAdapter,
    create_neo4j_adapter,
)
from memory.projection import ProjectionWorker, record_outbox_event


class TestNeo4jProjectionAdapter(unittest.TestCase):
    """Test suite covering Neo4j graph projection, provenance tracking, and error isolation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "memory.sqlite3"
        gateway.DB = self.db_path

        # Use in-memory graph store for fast, deterministic offline testing
        self.graph_store = InMemoryGraphStore()
        self.adapter = Neo4jProjectionAdapter(in_memory_store=self.graph_store)
        gateway.set_neo4j_adapter(self.adapter)

    def tearDown(self):
        gateway.set_neo4j_adapter(None)
        self.tmp.cleanup()

    def test_adapter_initialization_and_healthy(self):
        """Verify adapter initialization and health status."""
        self.assertEqual(self.adapter.name, "neo4j")
        self.assertTrue(self.adapter.is_healthy())

    def test_upsert_creates_nodes_and_edges(self):
        """Upserting a memory creates Agent, Memory, and AUTHORED observed edge."""
        mem = {
            "id": "mem_g1",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Graph theory foundation",
            "confidence": 0.95,
            "created_at": "2026-09-25T00:00:00Z",
            "source_event": "evt-001",
        }
        self.adapter.upsert(mem)

        # Verify nodes exist in graph store
        self.assertIn("scholar", self.graph_store.nodes["Agent"])
        self.assertIn("mem_g1", self.graph_store.nodes["Memory"])
        mem_node = self.graph_store.nodes["Memory"]["mem_g1"]
        self.assertEqual(mem_node["content"], "Graph theory foundation")
        self.assertEqual(mem_node["scope"], "shared")

        # Verify edge exists with status 'observed'
        authored_edges = [
            e
            for e in self.graph_store.edges
            if e["source_id"] == "scholar"
            and e["target_id"] == "mem_g1"
            and e["rel_type"] == "AUTHORED"
        ]
        self.assertEqual(len(authored_edges), 1)
        self.assertEqual(authored_edges[0]["props"]["status"], "observed")

    def test_inferred_task_and_artifact_relationships(self):
        """References to tasks and artifacts create inferred relationship edges."""
        mem = {
            "id": "mem_g2",
            "agent": "artisan",
            "scope": "shared",
            "kind": "observation",
            "content": "Built component according to task specification",
            "confidence": 0.85,
            "created_at": "2026-09-25T00:01:00Z",
            "metadata": json.dumps({"task_id": "task_42", "artifact_id": "art_99"}),
        }
        self.adapter.upsert(mem)

        # Verify Task and Artifact nodes created
        self.assertIn("task_42", self.graph_store.nodes["Task"])
        self.assertIn("art_99", self.graph_store.nodes["Artifact"])

        # Verify inferred edges created
        task_edges = [
            e
            for e in self.graph_store.edges
            if e["source_id"] == "mem_g2" and e["target_id"] == "task_42"
        ]
        self.assertEqual(len(task_edges), 1)
        self.assertEqual(task_edges[0]["rel_type"], "REFERENCES_TASK")
        self.assertEqual(task_edges[0]["props"]["status"], "inferred")

        art_edges = [
            e
            for e in self.graph_store.edges
            if e["source_id"] == "mem_g2" and e["target_id"] == "art_99"
        ]
        self.assertEqual(len(art_edges), 1)
        self.assertEqual(art_edges[0]["rel_type"], "REFERENCES_ARTIFACT")
        self.assertEqual(art_edges[0]["props"]["status"], "inferred")

    def test_provenance_query_scope_isolation_fremde_uid(self):
        """Provenance queries respect scope: private memories rejected for foreign agents."""
        pub_mem = {
            "id": "mem_pub1",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Public consensus finding",
            "created_at": "2026-09-25T00:00:00Z",
        }
        priv_mem = {
            "id": "mem_priv1",
            "agent": "artisan",
            "scope": "private",
            "kind": "observation",
            "content": "Artisan private notes",
            "created_at": "2026-09-25T00:00:00Z",
        }
        self.adapter.upsert(pub_mem)
        self.adapter.upsert(priv_mem)

        # Public memory provenance accessible to anyone
        prov_scholar = self.adapter.get_provenance("mem_pub1", caller_agent="scholar")
        self.assertIsNotNone(prov_scholar)
        self.assertEqual(prov_scholar["agent"], "scholar")
        self.assertEqual(prov_scholar["author_relation_status"], "observed")

        prov_other = self.adapter.get_provenance("mem_pub1", caller_agent="other_agent")
        self.assertIsNotNone(prov_other)

        # Private memory provenance accessible to author and admin only
        prov_author = self.adapter.get_provenance("mem_priv1", caller_agent="artisan")
        self.assertIsNotNone(prov_author)
        self.assertEqual(prov_author["agent"], "artisan")

        prov_admin = self.adapter.get_provenance("mem_priv1", caller_agent="*")
        self.assertIsNotNone(prov_admin)

        # Foreign agent rejected (returns None)
        prov_foreign = self.adapter.get_provenance("mem_priv1", caller_agent="scholar")
        self.assertIsNone(prov_foreign)

    def test_delete_and_tombstone(self):
        """Deleting a memory removes the node and connected edges from graph."""
        mem = {
            "id": "mem_to_del",
            "agent": "scholar",
            "scope": "shared",
            "content": "To be removed from graph",
            "created_at": "2026-09-25T00:00:00Z",
            "metadata": json.dumps({"task_id": "task_del"}),
        }
        self.adapter.upsert(mem)
        self.assertIn("mem_to_del", self.graph_store.nodes["Memory"])
        self.assertEqual(len(self.graph_store.edges), 2)  # AUTHORED + REFERENCES_TASK

        self.adapter.delete("mem_to_del", {"agent": "scholar"})
        self.assertNotIn("mem_to_del", self.graph_store.nodes["Memory"])
        self.assertEqual(len(self.graph_store.edges), 0)

    def test_clear_and_rebuild_from_sqlite(self):
        """Rebuilding from authoritative SQLite outbox repopulates Neo4j graph."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item1 = {
            "id": "mem_rb_g1",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Graph rebuild item 1",
            "source_event": "evt-rb-1",
            "confidence": 0.9,
            "expires_at": None,
            "metadata": json.dumps({"task_id": "task_rb_1"}),
            "idempotency_key": "rb_g1",
        }
        item2 = {
            "id": "mem_rb_g2",
            "created_at": "2026-09-25T00:01:00Z",
            "updated_at": "2026-09-25T00:01:00Z",
            "agent": "artisan",
            "scope": "shared",
            "kind": "observation",
            "content": "Graph rebuild item 2",
            "source_event": "evt-rb-2",
            "confidence": 0.8,
            "expires_at": None,
            "metadata": json.dumps({"artifact_id": "art_rb_2"}),
            "idempotency_key": "rb_g2",
        }
        for it in (item1, item2):
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
                it,
            )
            record_outbox_event(conn, it["id"], "upsert", it)
        conn.commit()
        conn.close()

        worker = ProjectionWorker(self.db_path)
        worker.register_backend(self.adapter)

        # Clear graph
        self.adapter.clear()
        self.assertEqual(len(self.graph_store.nodes["Memory"]), 0)

        # Rebuild from SQLite
        rebuilt = worker.rebuild("neo4j")
        self.assertEqual(rebuilt, 2)
        self.assertIn("mem_rb_g1", self.graph_store.nodes["Memory"])
        self.assertIn("mem_rb_g2", self.graph_store.nodes["Memory"])
        self.assertIn("task_rb_1", self.graph_store.nodes["Task"])
        self.assertIn("art_rb_2", self.graph_store.nodes["Artifact"])

    def test_gateway_provenance_endpoint_with_neo4j_and_sqlite_fallback(self):
        """Gateway GET /v1/memories/<id>/provenance returns graph data, falls back on offline."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item = {
            "id": "mem_prov_test",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Knowledge with verifiable provenance",
            "source_event": "evt-origin",
            "confidence": 0.95,
            "expires_at": None,
            "metadata": json.dumps({"task_id": "task_prov_1"}),
            "idempotency_key": "prov_k1",
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
        conn.commit()
        conn.close()

        # 1. Project into Neo4j
        self.adapter.upsert(item)

        class DummyHandler:
            def __init__(self, path):
                self.path = path
                self.headers = {"Authorization": "Bearer test-token"}
                self.sent_code = None
                self.sent_payload = None

            def send_json(self, code, payload):
                self.sent_code = code
                self.sent_payload = payload
                return payload

        # Query provenance via gateway (Neo4j active)
        handler = DummyHandler("/v1/memories/mem_prov_test/provenance")
        with patch.object(gateway, "auth", return_value=True), patch.object(
            gateway, "caller", return_value="scholar"
        ):
            gateway.Handler.do_GET(handler)

        self.assertEqual(handler.sent_code, 200)
        self.assertEqual(handler.sent_payload["backend"], "neo4j-graph")
        self.assertEqual(handler.sent_payload["provenance"]["task_id"], "task_prov_1")
        self.assertEqual(
            handler.sent_payload["provenance"]["author_relation_status"], "observed"
        )

        # 2. Simulate Neo4j offline: fallback to SQLite provenance
        broken_adapter = MagicMock()
        broken_adapter.is_healthy.return_value = False
        gateway.set_neo4j_adapter(broken_adapter)

        handler_fallback = DummyHandler("/v1/memories/mem_prov_test/provenance")
        with patch.object(gateway, "auth", return_value=True), patch.object(
            gateway, "caller", return_value="scholar"
        ):
            gateway.Handler.do_GET(handler_fallback)

        self.assertEqual(handler_fallback.sent_code, 200)
        self.assertEqual(handler_fallback.sent_payload["backend"], "sqlite-provenance-fallback")
        self.assertEqual(handler_fallback.sent_payload["provenance"]["agent"], "scholar")
        self.assertEqual(handler_fallback.sent_payload["provenance"]["source_event"], "evt-origin")

    def test_error_isolation_offline_neo4j_does_not_block_sqlite_or_other_sinks(self):
        """Failing Neo4j adapter does not block primary DB or other projection backends."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item = {
            "id": "mem_neo_iso",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Fault isolation test",
            "source_event": "",
            "confidence": 1.0,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "neo_iso_k",
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
        record_outbox_event(conn, "mem_neo_iso", "upsert", item)
        conn.commit()
        conn.close()

        # Create worker with failing Neo4j adapter and healthy mock sink
        failing_neo4j = MagicMock()
        failing_neo4j.name = "neo4j"
        failing_neo4j.upsert.side_effect = Neo4jConnectionError("Connection refused: 7474")

        healthy_sink = MagicMock()
        healthy_sink.name = "other_sink"

        worker = ProjectionWorker(self.db_path)
        worker.register_backend(failing_neo4j)
        worker.register_backend(healthy_sink)

        counts = worker.process_all()

        # Healthy sink processed successfully
        self.assertEqual(counts["other_sink"], 1)
        healthy_sink.upsert.assert_called_once()

        # Failing Neo4j marked degraded with error isolated
        self.assertEqual(counts["neo4j"], 0)
        status = worker.get_status()
        self.assertEqual(status["backends"]["neo4j"]["status"], "degraded")
        self.assertIn("Connection refused", status["backends"]["neo4j"]["last_error"])

        # Primary SQLite remains fully operational
        conn = gateway.db()
        cnt = conn.execute("SELECT count(*) FROM memories WHERE id = 'mem_neo_iso'").fetchone()[0]
        self.assertEqual(cnt, 1)
        conn.close()

    def test_rollback_disable_neo4j(self):
        """Setting NEO4J_ENABLED=0 disables the adapter cleanly."""
        with patch.dict(os.environ, {"NEO4J_ENABLED": "0"}):
            self.assertIsNone(gateway.get_neo4j_adapter())


if __name__ == "__main__":
    unittest.main()

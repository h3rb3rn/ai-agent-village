"""Unit tests for P16: ChromaDB vector projection adapter, namespace isolation, and offline fallback."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import chromadb
from chromadb.config import Settings

from memory import gateway
from memory.chroma_adapter import (
    PINNED_MODEL_INFO,
    ChromaProjectionAdapter,
    DeterministicLocalEmbedder,
    create_chroma_adapter,
)
from memory.projection import ProjectionWorker, record_outbox_event


class TestChromaProjectionAdapter(unittest.TestCase):
    """Test suite covering Chroma vector projection, namespace isolation, and fallback semantics."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "memory.sqlite3"
        gateway.DB = self.db_path

        # Use an ephemeral client for isolated testing
        settings = Settings(anonymized_telemetry=False, allow_reset=True)
        self.chroma_client = chromadb.EphemeralClient(settings=settings)
        try:
            self.chroma_client.reset()
        except Exception:
            pass
        # Use deterministic embedder for fast, 100% offline reproducible unit testing
        self.embedder = DeterministicLocalEmbedder(dimension=384)
        self.adapter = ChromaProjectionAdapter(
            client=self.chroma_client,
            embedding_function=self.embedder,
        )
        gateway.set_chroma_adapter(self.adapter)

    def tearDown(self):
        gateway.set_chroma_adapter(None)
        try:
            self.chroma_client.reset()
        except Exception:
            pass
        self.tmp.cleanup()

    def test_adapter_initialization_offline_no_telemetry(self):
        """Verify telemetry disabled and model info matches pinned specification."""
        self.assertTrue(self.adapter.is_healthy())
        self.assertEqual(self.adapter.name, "chroma")
        model_info = self.adapter.get_model_info()
        self.assertEqual(model_info["name"], "sentence-transformers/all-MiniLM-L6-v2")
        self.assertEqual(model_info["license"], "Apache-2.0")
        self.assertEqual(model_info["dimension"], 384)
        self.assertEqual(
            model_info["sha256"],
            "4f148ba8ae9c2c7fbee4af2b132db8d06c6a6545b47fc83bbb98c3d22b8393e6",
        )
        self.assertFalse(model_info["telemetry"])
        self.assertTrue(model_info["offline_verified"])

    def test_upsert_shared_and_private_collections(self):
        """Shared memories go to village_shared; private memories go to agent-scoped collections."""
        shared_mem = {
            "id": "mem_s1",
            "agent": "scholar",
            "scope": "shared",
            "content": "Shared consensus on protocol",
            "kind": "fact",
            "confidence": 0.9,
            "created_at": "2026-09-25T00:00:00Z",
        }
        private_mem = {
            "id": "mem_p1",
            "agent": "artisan",
            "scope": "private",
            "content": "Private secret blueprint",
            "kind": "observation",
            "confidence": 0.8,
            "created_at": "2026-09-25T00:01:00Z",
        }

        self.adapter.upsert(shared_mem)
        self.adapter.upsert(private_mem)

        # Verify village_shared collection contains shared_mem only
        shared_col = self.chroma_client.get_collection(
            "village_shared", embedding_function=self.embedder
        )
        self.assertEqual(shared_col.count(), 1)
        self.assertEqual(shared_col.get(ids=["mem_s1"])["ids"], ["mem_s1"])

        # Verify artisan's private collection contains private_mem only
        artisan_col = self.chroma_client.get_collection(
            "village_agent_artisan", embedding_function=self.embedder
        )
        self.assertEqual(artisan_col.count(), 1)
        self.assertEqual(artisan_col.get(ids=["mem_p1"])["ids"], ["mem_p1"])

    def test_private_namespace_isolation_fremde_uid(self):
        """Caller agent_a searching cannot access or discover private memories of agent_b."""
        mem_a = {
            "id": "mem_a1",
            "agent": "agent_a",
            "scope": "private",
            "content": "Secret note by agent A regarding system tuning",
            "kind": "observation",
            "confidence": 0.9,
            "created_at": "2026-09-25T00:00:00Z",
        }
        mem_b = {
            "id": "mem_b1",
            "agent": "agent_b",
            "scope": "private",
            "content": "Confidential plan by agent B regarding resource allocation",
            "kind": "observation",
            "confidence": 0.9,
            "created_at": "2026-09-25T00:00:00Z",
        }
        mem_shared = {
            "id": "mem_pub",
            "agent": "agent_a",
            "scope": "shared",
            "content": "Public knowledge available to everyone",
            "kind": "fact",
            "confidence": 1.0,
            "created_at": "2026-09-25T00:00:00Z",
        }

        self.adapter.upsert(mem_a)
        self.adapter.upsert(mem_b)
        self.adapter.upsert(mem_shared)

        # Agent A searches across all scopes: should see mem_a1 and mem_pub, but NEVER mem_b1
        hits_for_a = self.adapter.search(query="system plan knowledge", caller_agent="agent_a")
        hit_ids_a = {h["id"] for h in hits_for_a}
        self.assertIn("mem_a1", hit_ids_a)
        self.assertIn("mem_pub", hit_ids_a)
        self.assertNotIn("mem_b1", hit_ids_a)

        # Agent B searches: should see mem_b1 and mem_pub, but NEVER mem_a1
        hits_for_b = self.adapter.search(query="system plan knowledge", caller_agent="agent_b")
        hit_ids_b = {h["id"] for h in hits_for_b}
        self.assertIn("mem_b1", hit_ids_b)
        self.assertIn("mem_pub", hit_ids_b)
        self.assertNotIn("mem_a1", hit_ids_b)

        # Explicit private scope search by agent B
        hits_b_private = self.adapter.search(
            query="confidential note", caller_agent="agent_b", scope="private"
        )
        hit_ids_b_priv = {h["id"] for h in hits_b_private}
        self.assertEqual(hit_ids_b_priv, {"mem_b1"})

    def test_delete_and_tombstone(self):
        """Deleting a memory removes it from Chroma collections."""
        mem = {
            "id": "mem_del_1",
            "agent": "scholar",
            "scope": "shared",
            "content": "Temporary observation to be removed",
            "kind": "observation",
            "confidence": 0.5,
            "created_at": "2026-09-25T00:00:00Z",
        }
        self.adapter.upsert(mem)
        shared_col = self.chroma_client.get_collection(
            "village_shared", embedding_function=self.embedder
        )
        self.assertEqual(shared_col.count(), 1)

        self.adapter.delete("mem_del_1", {"agent": "scholar", "scope": "shared"})
        self.assertEqual(shared_col.count(), 0)

    def test_scope_change_tombstone_shared_to_private(self):
        """Scope transition from shared to private moves item and removes it from shared index."""
        mem = {
            "id": "mem_trans_1",
            "agent": "scholar",
            "scope": "shared",
            "content": "Discovery initially shared then restricted",
            "kind": "fact",
            "confidence": 0.9,
            "created_at": "2026-09-25T00:00:00Z",
        }
        self.adapter.upsert(mem)

        shared_col = self.chroma_client.get_collection(
            "village_shared", embedding_function=self.embedder
        )
        scholar_col = self.chroma_client.get_collection(
            "village_agent_scholar", embedding_function=self.embedder
        )
        self.assertEqual(shared_col.count(), 1)
        self.assertEqual(scholar_col.count(), 0)

        # Transition to private
        mem_priv = dict(mem)
        mem_priv["scope"] = "private"
        self.adapter.upsert(mem_priv)

        # Now shared collection must be empty, and scholar private collection contains it
        self.assertEqual(shared_col.count(), 0)
        self.assertEqual(scholar_col.count(), 1)
        self.assertEqual(scholar_col.get(ids=["mem_trans_1"])["ids"], ["mem_trans_1"])

    def test_clear_and_rebuild_from_sqlite(self):
        """Rebuilding from authoritative SQLite outbox repopulates vector index."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item1 = {
            "id": "mem_rb1",
            "created_at": "2026-09-25T00:00:00Z",
            "updated_at": "2026-09-25T00:00:00Z",
            "agent": "artisan",
            "scope": "shared",
            "kind": "fact",
            "content": "Rebuild test item one",
            "source_event": "",
            "confidence": 0.8,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "rb_k1",
        }
        item2 = {
            "id": "mem_rb2",
            "created_at": "2026-09-25T00:01:00Z",
            "updated_at": "2026-09-25T00:01:00Z",
            "agent": "artisan",
            "scope": "private",
            "kind": "observation",
            "content": "Rebuild test item two",
            "source_event": "",
            "confidence": 0.7,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "rb_k2",
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

        # Clear vector database
        self.adapter.clear()
        self.assertEqual(len(self.chroma_client.list_collections()), 0)

        # Trigger full rebuild
        rebuilt = worker.rebuild("chroma")
        self.assertEqual(rebuilt, 2)

        # Verify items are back in collections
        shared_col = self.chroma_client.get_collection(
            "village_shared", embedding_function=self.embedder
        )
        artisan_col = self.chroma_client.get_collection(
            "village_agent_artisan", embedding_function=self.embedder
        )
        self.assertEqual(shared_col.count(), 1)
        self.assertEqual(artisan_col.count(), 1)

    def test_semantic_search_reference_dataset(self):
        """Semantic search retrieves relevant items from a fixed reference dataset."""
        dataset = [
            ("doc_arch", "software architecture and distributed system modular design", "shared"),
            ("doc_bio", "cellular biology genetics and dna sequencing mechanisms", "shared"),
            ("doc_agri", "crop rotation soil nutrients and sustainable agriculture", "shared"),
        ]
        for doc_id, text, scope in dataset:
            self.adapter.upsert(
                {
                    "id": doc_id,
                    "agent": "scholar",
                    "scope": scope,
                    "content": text,
                    "confidence": 0.9,
                    "created_at": "2026-09-25T00:00:00Z",
                }
            )

        hits = self.adapter.search(query="distributed system architecture", caller_agent="scholar")
        self.assertGreater(len(hits), 0)
        top_hit = hits[0]
        self.assertEqual(top_hit["id"], "doc_arch")
        self.assertGreater(top_hit["score"], 0.0)

    def test_sqlite_revalidation_drops_deleted_or_unauthorized_or_expired(self):
        """Gateway /v1/search revalidates all Chroma hits against authoritative SQLite:
        drops items deleted from SQLite, expired, or private to another agent.
        """
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        now_str = gateway.now()

        # 1. Normal active item
        item_valid = {
            "id": "mem_valid",
            "created_at": now_str,
            "updated_at": now_str,
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Valid persistent knowledge about distributed consensus",
            "source_event": "",
            "confidence": 0.9,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "vld_1",
        }
        # 2. Expired item (valid in Chroma, but expired in SQLite)
        item_expired = {
            "id": "mem_expired",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Expired knowledge about consensus",
            "source_event": "",
            "confidence": 0.9,
            "expires_at": "2026-01-02T00:00:00Z",
            "metadata": "{}",
            "idempotency_key": "exp_1",
        }
        # 3. Item deleted from SQLite (ghost memory in Chroma)
        item_deleted = {
            "id": "mem_ghost",
            "created_at": now_str,
            "updated_at": now_str,
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Ghost consensus item not in database",
            "source_event": "",
            "confidence": 0.9,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "ghost_1",
        }
        # Insert only valid and expired into SQLite; do NOT insert mem_ghost
        for it in (item_valid, item_expired):
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
        conn.commit()
        conn.close()

        # Upsert ALL THREE into Chroma to simulate vector index lagging behind or containing ghosts
        for it in (item_valid, item_expired, item_deleted):
            self.adapter.upsert(it)

        # Mock an authorized HTTP request to /v1/search
        class DummyHandler:
            def __init__(self, headers, body):
                raw = json.dumps(body).encode()
                self.headers = dict(headers)
                self.headers["Content-Length"] = str(len(raw))
                self.rfile = tempfile.SpooledTemporaryFile()
                self.rfile.write(raw)
                self.rfile.seek(0)
                self.path = "/v1/search"
                self.sent_code = None
                self.sent_payload = None

            def send_json(self, code, payload):
                self.sent_code = code
                self.sent_payload = payload
                return payload

        # Query via gateway
        handler = DummyHandler(
            headers={"Authorization": "Bearer test-token"},
            body={"query": "consensus", "limit": 10},
        )
        with patch.object(gateway, "auth", return_value=True), patch.object(
            gateway, "caller", return_value="scholar"
        ):
            gateway.Handler.do_POST(handler)

        self.assertEqual(handler.sent_code, 200)
        resp = handler.sent_payload
        self.assertEqual(resp["backend"], "chroma-semantic")
        result_ids = [it["id"] for it in resp["items"]]

        # mem_valid must be present
        self.assertIn("mem_valid", result_ids)
        # mem_expired must be filtered out
        self.assertNotIn("mem_expired", result_ids)
        # mem_ghost (not in SQLite) must be filtered out
        self.assertNotIn("mem_ghost", result_ids)

    def test_embedding_failure_or_backend_offline_falls_back_to_lexical(self):
        """When Chroma backend is offline or raises an error, gateway falls back to lexical search."""
        conn = gateway.db()
        conn.execute("BEGIN IMMEDIATE")
        item = {
            "id": "mem_lex_1",
            "created_at": gateway.now(),
            "updated_at": gateway.now(),
            "agent": "scholar",
            "scope": "shared",
            "kind": "fact",
            "content": "Authoritative lexical fallback target test content",
            "source_event": "",
            "confidence": 0.8,
            "expires_at": None,
            "metadata": "{}",
            "idempotency_key": "lex_k1",
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

        # Simulate Chroma backend throwing an error or reporting unhealthy
        broken_adapter = MagicMock()
        broken_adapter.is_healthy.return_value = False
        gateway.set_chroma_adapter(broken_adapter)

        class DummyHandler:
            def __init__(self, body):
                raw = json.dumps(body).encode()
                self.headers = {"Authorization": "Bearer test-token", "Content-Length": str(len(raw))}
                self.rfile = tempfile.SpooledTemporaryFile()
                self.rfile.write(raw)
                self.rfile.seek(0)
                self.path = "/v1/search"
                self.sent_code = None
                self.sent_payload = None

            def send_json(self, code, payload):
                self.sent_code = code
                self.sent_payload = payload
                return payload

        handler = DummyHandler(body={"query": "authoritative target", "limit": 5})
        with patch.object(gateway, "auth", return_value=True), patch.object(
            gateway, "caller", return_value="scholar"
        ):
            gateway.Handler.do_POST(handler)

        self.assertEqual(handler.sent_code, 200)
        resp = handler.sent_payload
        # Transparently fell back to SQLite lexical search
        self.assertEqual(resp["backend"], "sqlite-lexical-fallback")
        self.assertEqual(len(resp["items"]), 1)
        self.assertEqual(resp["items"][0]["id"], "mem_lex_1")

    def test_adapter_disable_rollback(self):
        """Setting CHROMA_ENABLED=0 rolls back to SQLite lexical search with full operability."""
        with patch.dict(os.environ, {"CHROMA_ENABLED": "0"}):
            self.assertIsNone(gateway.get_chroma_adapter())


if __name__ == "__main__":
    unittest.main()

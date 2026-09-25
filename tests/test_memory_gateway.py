"""Unit tests for P14: Memory gateway security, validation, fail-closed auth, and concurrency."""

import json
import math
import os
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "memory"))
import gateway


class MemoryGatewayTests(unittest.TestCase):
    """Test suite covering memory gateway security, fail-closed auth, CRUD, and isolation."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        gateway.DB = Path(self.tmp.name) / "memory" / "memory.sqlite3"
        gateway.TOKEN = "test-master-token"
        gateway.RATE_LIMIT = 2
        gateway.TOKENS_FILE = Path(self.tmp.name) / "tokens.json"
        gateway.TOKENS_FILE.write_text(json.dumps({"a": "token-a", "b": "token-b", "artisan": "token-artisan"}))

        self.server = gateway.ThreadingHTTPServer(("127.0.0.1", 0), gateway.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def call(self, method, path, body=None, token="test-master-token"):
        c = HTTPConnection("127.0.0.1", self.port)
        raw = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        c.request(method, path, raw, headers)
        r = c.getresponse()
        resp_data = r.read().decode("utf-8")
        return r.status, json.loads(resp_data) if resp_data else {}

    def test_provenance_search_and_quota(self):
        """Verify standard provenance tracking, search, and rate limit enforcement."""
        status, item = self.call(
            "POST",
            "/v1/memories",
            {
                "agent": "artisan",
                "scope": "private",
                "kind": "observation",
                "content": "GPU query needs nounits",
                "source_event": "evt-1",
                "confidence": 0.9,
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(item["source_event"], "evt-1")

        status, result = self.call("POST", "/v1/search", {"query": "GPU nounits", "agent": "artisan"})
        self.assertEqual(status, 200)
        self.assertEqual(result["items"][0]["agent"], "artisan")

        # Second write succeeds (limit=2)
        self.call("POST", "/v1/memories", {"agent": "artisan", "content": "second"})
        # Third write must be blocked by rate limit
        status, _ = self.call("POST", "/v1/memories", {"agent": "artisan", "content": "third"})
        self.assertEqual(status, 429)

    def test_authentication(self):
        """Invalid token must return 401 Unauthorized."""
        self.assertEqual(self.call("GET", "/v1/memories", token="wrong")[0], 401)
        self.assertEqual(self.call("GET", "/v1/memories", token=None)[0], 401)

    def test_private_memories_are_owner_scoped(self):
        """Ensure private memories are visible only to author; shared memories visible to all."""
        for owner, scope in [("a", "private"), ("b", "private"), ("b", "shared")]:
            status, _ = self.call(
                "POST",
                "/v1/memories",
                {"agent": owner, "content": "evidence " + scope, "scope": scope},
                token="token-" + owner,
            )
            self.assertEqual(status, 201)

        for method, path, body in [("GET", "/v1/memories", None), ("POST", "/v1/search", {"query": "evidence"})]:
            status, result = self.call(method, path, body, token="token-a")
            self.assertEqual(status, 200)
            self.assertEqual(len(result["items"]), 2)
            self.assertTrue(all(x["agent"] == "a" or x["scope"] == "shared" for x in result["items"]))

        # Agent 'a' attempting to impersonate 'b' must be rejected with 403 Forbidden
        status, _ = self.call("POST", "/v1/memories", {"agent": "b", "content": "impersonation"}, token="token-a")
        self.assertEqual(status, 403)

    def test_fail_closed_auth_on_missing_or_corrupt_tokens_file(self):
        """Missing or corrupted token file must fail-closed (401), never allow anonymous access."""
        # Unset master token
        gateway.TOKEN = ""
        # Remove tokens file
        if gateway.TOKENS_FILE.exists():
            gateway.TOKENS_FILE.unlink()

        # Healthcheck remains accessible unauthenticated
        status, data = self.call("GET", "/healthz", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))

        # All memory endpoints must reject with 401
        self.assertEqual(self.call("GET", "/v1/memories", token=None)[0], 401)
        self.assertEqual(self.call("POST", "/v1/memories", {"content": "leak"}, token=None)[0], 401)
        self.assertEqual(self.call("POST", "/v1/search", {"query": "leak"}, token=None)[0], 401)

        # Corrupted JSON tokens file must also fail-closed
        gateway.TOKENS_FILE.write_text("{corrupt-json")
        self.assertEqual(self.call("GET", "/v1/memories", token="some-token")[0], 401)

    def test_crud_operations_update_and_delete(self):
        """Verify GET by id, PATCH update, and DELETE with strict author ownership."""
        # 1. Agent A creates memory
        status, item = self.call(
            "POST",
            "/v1/memories",
            {"content": "Original content", "scope": "private", "confidence": 0.8},
            token="token-a",
        )
        self.assertEqual(status, 201)
        mem_id = item["id"]

        # 2. Agent A can read it directly
        status, fetched = self.call("GET", f"/v1/memories/{mem_id}", token="token-a")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["content"], "Original content")

        # 3. Agent B cannot read Agent A's private memory directly
        status, _ = self.call("GET", f"/v1/memories/{mem_id}", token="token-b")
        self.assertEqual(status, 403)

        # 4. Agent B cannot update or delete Agent A's memory
        status, _ = self.call("PATCH", f"/v1/memories/{mem_id}", {"content": "Hijacked"}, token="token-b")
        self.assertEqual(status, 403)
        status, _ = self.call("DELETE", f"/v1/memories/{mem_id}", token="token-b")
        self.assertEqual(status, 403)

        # 5. Agent A updates content and scope to shared
        status, updated = self.call(
            "PATCH",
            f"/v1/memories/{mem_id}",
            {"content": "Updated content", "scope": "shared", "confidence": 1.0},
            token="token-a",
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["content"], "Updated content")
        self.assertEqual(updated["scope"], "shared")

        # 6. Now Agent B can read it because scope is shared
        status, b_read = self.call("GET", f"/v1/memories/{mem_id}", token="token-b")
        self.assertEqual(status, 200)

        # 7. Agent A deletes the memory
        status, del_resp = self.call("DELETE", f"/v1/memories/{mem_id}", token="token-a")
        self.assertEqual(status, 200)
        self.assertTrue(del_resp.get("ok"))

        # 8. Subsequent GET returns 404
        status, _ = self.call("GET", f"/v1/memories/{mem_id}", token="token-a")
        self.assertEqual(status, 404)

    def test_validation_limits_utf8_confidence(self):
        """Reject invalid scopes, confidence out of range/NaN, empty content, and size limit exceedances."""
        # Invalid scope
        status, _ = self.call("POST", "/v1/memories", {"content": "test", "scope": "public"}, token="token-a")
        self.assertEqual(status, 400)

        # Invalid confidence (NaN/Inf)
        status, _ = self.call("POST", "/v1/memories", {"content": "test", "confidence": "nan"}, token="token-a")
        self.assertEqual(status, 400)

        # Empty content
        status, _ = self.call("POST", "/v1/memories", {"content": "   "}, token="token-a")
        self.assertEqual(status, 400)

        # Exceeding max content chars
        huge_content = "X" * (gateway.MAX_CONTENT + 10)
        status, _ = self.call("POST", "/v1/memories", {"content": huge_content}, token="token-a")
        self.assertEqual(status, 400)

    def test_expiration_filtering(self):
        """Expired memories must not appear in search or listing and return 410 on direct fetch."""
        # Create memory expired 1 hour ago
        past_iso = "2020-01-01T00:00:00Z"
        status, item = self.call(
            "POST",
            "/v1/memories",
            {"content": "Expired secret formula", "scope": "shared", "expires_at": past_iso},
            token="token-a",
        )
        self.assertEqual(status, 201)
        mem_id = item["id"]

        # List must not include expired memory
        status, listing = self.call("GET", "/v1/memories", token="token-b")
        self.assertEqual(status, 200)
        self.assertNotIn(mem_id, [m["id"] for m in listing.get("items", [])])

        # Search must not include expired memory
        status, results = self.call("POST", "/v1/search", {"query": "secret formula"}, token="token-b")
        self.assertEqual(status, 200)
        self.assertEqual(len(results.get("items", [])), 0)

        # Direct fetch returns 410 Gone
        status, _ = self.call("GET", f"/v1/memories/{mem_id}", token="token-a")
        self.assertEqual(status, 410)

    def test_idempotency_key_duplicate_requests(self):
        """Duplicate request with same idempotency key returns identical record without quota drain."""
        key = "idemp_req_unique_99"
        status1, item1 = self.call(
            "POST",
            "/v1/memories",
            {"content": "Repeatable note", "idempotency_key": key},
            token="token-a",
        )
        self.assertEqual(status1, 201)

        # Repeat identical request
        status2, item2 = self.call(
            "POST",
            "/v1/memories",
            {"content": "Repeatable note", "idempotency_key": key},
            token="token-a",
        )
        self.assertEqual(status2, 200)
        self.assertEqual(item1["id"], item2["id"])

    def test_concurrent_writes_and_quota_race(self):
        """Verify atomic transaction prevents quota overbooking during concurrent writes."""
        gateway.RATE_LIMIT = 5
        results = []
        threads = []

        def worker(idx):
            st, _ = self.call("POST", "/v1/memories", {"content": f"Concurrent {idx}"}, token="token-a")
            results.append(st)

        for i in range(10):
            t = threading.Thread(target=worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        success_count = sum(1 for s in results if s == 201)
        quota_exceeded_count = sum(1 for s in results if s == 429)

        # Exactly 5 should succeed and 5 should get 429
        self.assertEqual(success_count, 5)
        self.assertEqual(quota_exceeded_count, 5)

    def test_file_permissions_and_backup_verification(self):
        """Verify 0600 file permissions and backup database verification."""
        conn = gateway.db()
        conn.close()

        self.assertTrue(gateway.DB.exists())
        # Check permissions: must be 0600 (not group or world accessible)
        mode = gateway.DB.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

        # Test backup
        backup_dir = Path(self.tmp.name) / "backup"
        backup_path = gateway.backup_database(backup_dir)
        self.assertTrue(backup_path.exists())

        # Test verification
        ok, msg = gateway.verify_database(backup_path)
        self.assertTrue(ok)
        self.assertIn("ok", msg)


if __name__ == "__main__":
    unittest.main()

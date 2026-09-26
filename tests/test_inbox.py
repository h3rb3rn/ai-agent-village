"""Unit and integration tests for P09: Persistent Inbox & Explicit Ack."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from village.coordinator import CoordinationStore
from web.runtime import Resident


class TestInboxAndAck(unittest.TestCase):
    """Test suite covering persistent inbox storage, delivery tracking, and explicit ack."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-inbox-test-"))
        self.board = self.tmp / "board"
        self.board.mkdir(parents=True, exist_ok=True)
        self.store = CoordinationStore(self.board / "coordination.sqlite3", self.board)
        self.agent_id = "agent_omega"
        self.peer_id = "agent_beta"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_post_and_fetch_unacknowledged_messages(self):
        """Verify posting inbox messages and retrieving unacknowledged items scoped to agent."""
        msg1 = self.store.post_inbox_message(
            source="direct",
            sender=self.peer_id,
            content="Hello Omega, please review task T-1",
            recipient=self.agent_id,
            reply_to=None,
        )
        msg2 = self.store.post_inbox_message(
            source="direct",
            sender=self.peer_id,
            content="Message for another peer",
            recipient="agent_gamma",
        )
        msg3 = self.store.post_inbox_message(
            source="board",
            sender=self.peer_id,
            content="Broadcast to all",
            recipient=None,
        )
        events = (self.board / 'events.jsonl').read_text().splitlines()
        self.assertTrue(any('board_message' in line for line in events))

        unacked = self.store.fetch_unacknowledged_messages(self.agent_id)
        unacked_ids = [m["id"] for m in unacked]
        # Should see direct message to agent_omega and broadcast (recipient=None), but not message to agent_gamma
        self.assertIn(msg1["id"], unacked_ids)
        self.assertIn(msg3["id"], unacked_ids)
        self.assertNotIn(msg2["id"], unacked_ids)

    def test_explicit_acknowledgment_workflow(self):
        """Verify delivery marking followed by explicit acknowledgment."""
        msg = self.store.post_inbox_message(
            source="direct",
            sender=self.peer_id,
            content="Important sync point",
            recipient=self.agent_id,
        )
        msg_id = msg["id"]

        # Before delivery, unacknowledged
        unacked_before = self.store.fetch_unacknowledged_messages(self.agent_id)
        self.assertEqual(len(unacked_before), 1)

        # Mark delivered
        self.store.mark_messages_delivered(self.agent_id, [msg_id])

        # Still unacknowledged until explicitly acknowledged
        unacked_during = self.store.fetch_unacknowledged_messages(self.agent_id)
        self.assertEqual(len(unacked_during), 1)

        # Explicit ack
        acked_count = self.store.acknowledge_messages(self.agent_id, [msg_id])
        self.assertEqual(acked_count, 1)

        # After ack, no longer returned in unacknowledged
        unacked_after = self.store.fetch_unacknowledged_messages(self.agent_id)
        self.assertEqual(len(unacked_after), 0)

        # Idempotent re-acknowledgment
        acked_repeat = self.store.acknowledge_messages(self.agent_id, [msg_id])
        self.assertEqual(acked_repeat, 1)

    def test_sync_organic_inbox_idempotence(self):
        """Verify importing organic inbox messages is idempotent and handles restarts."""
        organic_file = self.board / "organic-inbox.jsonl"
        lines = [
            json.dumps({"timestamp": "2026-09-24T12:00:00Z", "sender": "operator", "message": "First directive"}),
            json.dumps({"timestamp": "2026-09-24T12:05:00Z", "sender": "operator", "message": "Second directive"}),
        ]
        organic_file.write_text("\n".join(lines) + "\n")

        first_sync = self.store.sync_organic_inbox(organic_file, self.agent_id)
        self.assertEqual(len(first_sync), 2)

        # Re-running sync with same content should not create duplicates
        second_sync = self.store.sync_organic_inbox(organic_file, self.agent_id)
        self.assertEqual(len(second_sync), 2)
        self.assertEqual([m["id"] for m in first_sync], [m["id"] for m in second_sync])

        # Acknowledge first message
        self.store.acknowledge_messages(self.agent_id, [first_sync[0]["id"]])
        third_sync = self.store.sync_organic_inbox(organic_file, self.agent_id)
        self.assertEqual(len(third_sync), 1)
        self.assertEqual(third_sync[0]["id"], first_sync[1]["id"])

    def test_context_budget_truncation_preserves_unacknowledged(self):
        """Verify that when context trimming drops messages, dropped messages remain unacknowledged."""
        organic_file = self.board / "organic-inbox.jsonl"
        # Create multiple organic messages
        lines = [
            json.dumps({"timestamp": f"2026-09-24T12:0{i}:00Z", "sender": "operator", "message": f"Message {i} " + "X" * 2000})
            for i in range(5)
        ]
        organic_file.write_text("\n".join(lines) + "\n")

        # Set up a Resident with a small context budget
        root = self.tmp / "village"
        root.mkdir(parents=True, exist_ok=True)
        (root / "board").mkdir(parents=True, exist_ok=True)
        shutil.copy(organic_file, root / "board" / "organic-inbox.jsonl")

        peers_path = Path("/etc/ai-village/runtime-peers.json")
        env = {
            "AGENT_ID": "agent_small_ctx",
            "AGENT_NAME": "small_ctx",
            "AGENT_ROLE": "tester",
            "VILLAGE_ROOT": str(root),
            "OLLAMA_MODEL": "test-model",
            "OLLAMA_NUM_CTX": "4096",  # Small budget: triggers trimming in snapshot
            "VILLAGE_PAUSE_MARKER": str(self.tmp / "paused"),
        }

        agent = Resident(env=env)
        with patch.object(agent, "memory", return_value={"items": []}):
            snapshot_str = agent.snapshot()

        snapshot_data = json.loads(snapshot_str)
        delivered_in_prompt = snapshot_data.get("recent_organic_messages_untrusted", [])
        # Only a subset survived budget trimming
        self.assertLess(len(delivered_in_prompt), 5)
        self.assertEqual(len(agent.delivered_inbox_ids), len(delivered_in_prompt))

        # Check in store: only delivered messages are marked delivered
        for item in delivered_in_prompt:
            self.assertIn(item["id"], agent.delivered_inbox_ids)

    def test_cycle_failure_does_not_acknowledge(self):
        """Verify that failed inference (network error or fallback) does not acknowledge delivered messages."""
        organic_file = self.board / "organic-inbox.jsonl"
        organic_file.write_text(
            json.dumps({"timestamp": "2026-09-24T12:00:00Z", "sender": "operator", "message": "Directive 1"}) + "\n"
        )

        root = self.tmp / "village_cycle"
        root.mkdir(parents=True, exist_ok=True)
        (root / "board").mkdir(parents=True, exist_ok=True)
        shutil.copy(organic_file, root / "board" / "organic-inbox.jsonl")

        env = {
            "AGENT_ID": "agent_fail_test",
            "AGENT_NAME": "fail_test",
            "AGENT_ROLE": "tester",
            "VILLAGE_ROOT": str(root),
            "OLLAMA_MODEL": "test-model",
            "OLLAMA_URL": "http://127.0.0.1:9999",
            "AGENT_IDENTITY_PROMPT": str(self.tmp / "identity.txt"),
            "VILLAGE_PAUSE_MARKER": str(self.tmp / "paused"),
        }
        Path(env["AGENT_IDENTITY_PROMPT"]).write_text("Test Identity")

        agent = Resident(env=env)

        # Snapshot runs, delivery is staged
        with patch.object(agent, "memory", return_value={"items": []}):
            agent.snapshot()
        self.assertTrue(len(agent.delivered_inbox_ids) > 0)
        staged_id = agent.delivered_inbox_ids[0]

        # Cycle fails due to network error
        with patch.object(agent, "snapshot", return_value=json.dumps({"test": 1})), \
             patch("web.runtime.Path.read_text", return_value="System prompt"):
            agent.cycle()

        # Messages must still be unacknowledged in the store
        unacked = agent.tasks.store.fetch_unacknowledged_messages(agent.id)
        self.assertIn(staged_id, [m["id"] for m in unacked])

    def test_cycle_success_acknowledges_messages(self):
        """Verify that successful cycle execution acknowledges delivered inbox messages."""
        organic_file = self.board / "organic-inbox.jsonl"
        organic_file.write_text(
            json.dumps({"timestamp": "2026-09-24T12:00:00Z", "sender": "operator", "message": "Directive A"}) + "\n"
        )

        root = self.tmp / "village_cycle_success"
        root.mkdir(parents=True, exist_ok=True)
        (root / "board").mkdir(parents=True, exist_ok=True)
        shutil.copy(organic_file, root / "board" / "organic-inbox.jsonl")

        env = {
            "AGENT_ID": "agent_success_test",
            "AGENT_NAME": "success_test",
            "AGENT_ROLE": "tester",
            "VILLAGE_ROOT": str(root),
            "OLLAMA_MODEL": "test-model",
            "OLLAMA_URL": "http://127.0.0.1:9999",
            "AGENT_IDENTITY_PROMPT": str(self.tmp / "identity.txt"),
            "VILLAGE_PAUSE_MARKER": str(self.tmp / "paused"),
        }
        Path(env["AGENT_IDENTITY_PROMPT"]).write_text("Test Identity")

        agent = Resident(env=env)
        with patch.object(agent, "memory", return_value={"items": []}):
            agent.snapshot()

        self.assertTrue(len(agent.delivered_inbox_ids) > 0)
        staged_id = agent.delivered_inbox_ids[0]

        # Simulate valid inference stopping at idle
        mock_response = {
            "done_reason": "stop",
            "message": {"content": '{"name":"idle","arguments":{}}'},
            "prompt_eval_count": 50,
            "eval_count": 10,
        }
        with patch.object(agent, "snapshot", return_value=json.dumps({"test": 1})), \
             patch("web.runtime.Path.read_text", return_value="System prompt"), \
             patch("web.runtime.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(mock_response).encode())):
            agent.cycle()

        # The message should now be acknowledged in the store!
        unacked = agent.tasks.store.fetch_unacknowledged_messages(agent.id)
        self.assertNotIn(staged_id, [m["id"] for m in unacked])
        self.assertEqual(len(agent.delivered_inbox_ids), 0)


if __name__ == "__main__":
    unittest.main()

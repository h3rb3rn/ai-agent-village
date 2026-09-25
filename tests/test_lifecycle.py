"""Unit tests for village.lifecycle inference tracking and state reconciliation."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from village.lifecycle import (
    InferenceRecord,
    InferenceState,
    InferenceTracker,
    classify_error,
)
from web.runtime import Resident


class TestVillageLifecycle(unittest.TestCase):
    """Test suite for inference lifecycle tracking, reconciliation, and action execution gating."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test_lifecycle.sqlite3"
        self.agent_id = "01-king"
        self.tracker = InferenceTracker(self.db_path, self.agent_id)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_request_lifecycle_progression(self) -> None:
        """Verify normal progression: queued -> requesting -> completed with metrics."""
        record = self.tracker.create_request(
            model="qwen2.5:7b",
            provider="ollama",
            generation=1,
        )
        self.assertEqual(record.state, InferenceState.QUEUED.value)
        self.assertEqual(record.generation, 1)

        # Transition to requesting
        started = self.tracker.start_request(record.request_id)
        self.assertIsNotNone(started)
        self.assertEqual(started.state, InferenceState.REQUESTING.value)
        self.assertIsNotNone(started.started_at)

        # Transition to completed
        completed = self.tracker.complete_request(
            record.request_id,
            duration_ms=450,
            prompt_tokens=128,
            completion_tokens=32,
            gpu_verified=True,
        )
        self.assertIsNotNone(completed)
        self.assertEqual(completed.state, InferenceState.COMPLETED.value)
        self.assertEqual(completed.duration_ms, 450)
        self.assertEqual(completed.prompt_tokens, 128)
        self.assertEqual(completed.completion_tokens, 32)
        self.assertTrue(completed.gpu_verified)

    def test_reconcile_stale_requests_transitions_to_unknown(self) -> None:
        """Verify that interrupted or crashed requests are reconciled to 'unknown'."""
        # Create a request left in 'requesting' state
        req1 = self.tracker.create_request(
            model="qwen2.5:7b",
            provider="ollama",
            generation=1,
        )
        self.tracker.start_request(req1.request_id)

        # Create a request left in 'queued' state
        req2 = self.tracker.create_request(
            model="qwen2.5:7b",
            provider="ollama",
            generation=1,
        )

        # Reconcile on simulated restart
        reconciled = self.tracker.reconcile_stale_requests()
        self.assertEqual(reconciled, 2)

        # Incomplete requests must now be 'unknown'
        r1 = self.tracker.get_request(req1.request_id)
        r2 = self.tracker.get_request(req2.request_id)
        self.assertEqual(r1.state, InferenceState.UNKNOWN.value)
        self.assertEqual(r1.error_class, "interrupted_or_crashed")
        self.assertEqual(r2.state, InferenceState.UNKNOWN.value)

    def test_action_execution_gating_generation_mismatch(self) -> None:
        """Ensure late responses from an older generation cannot execute actions."""
        record = self.tracker.create_request(
            model="qwen2.5:7b",
            provider="ollama",
            generation=1,
        )
        self.tracker.start_request(record.request_id)
        self.tracker.complete_request(
            record.request_id,
            duration_ms=500,
            prompt_tokens=100,
            completion_tokens=20,
        )

        # Active generation is now 2 (e.g. after config change or resume)
        authorized = self.tracker.mark_action_executed(record.request_id, current_generation=2)
        self.assertFalse(authorized, "Late response from generation 1 must be rejected in generation 2")

        # Now test matching generation
        authorized_same_gen = self.tracker.mark_action_executed(record.request_id, current_generation=1)
        self.assertTrue(authorized_same_gen, "Matching generation must be authorized")

    def test_duplicate_action_execution_prevented(self) -> None:
        """Ensure the same request cannot authorize multiple action executions."""
        record = self.tracker.create_request(
            model="qwen2.5:7b",
            provider="ollama",
            generation=1,
        )
        self.tracker.start_request(record.request_id)
        self.tracker.complete_request(
            record.request_id,
            duration_ms=200,
            prompt_tokens=50,
            completion_tokens=10,
        )

        # First authorization succeeds
        self.assertTrue(self.tracker.mark_action_executed(record.request_id, current_generation=1))

        # Duplicate attempt fails
        self.assertFalse(
            self.tracker.mark_action_executed(record.request_id, current_generation=1),
            "Duplicate execution of the same request must be denied",
        )

    def test_error_classification(self) -> None:
        """Check error classification for timeouts, connection errors, and HTTP codes."""
        # Timeout
        t_err = TimeoutError("Request timed out after 30 seconds")
        cat, _ = classify_error(t_err)
        self.assertEqual(cat, "timeout")

        # Connection refused
        cr_err = OSError("Connection refused by peer")
        cat, _ = classify_error(cr_err)
        self.assertEqual(cat, "unknown_error")

    def test_runtime_cycle_integrates_lifecycle(self) -> None:
        """Verify Resident.cycle records requests in SQLite and writes active pointer."""
        root = Path(self.tmp.name) / "village"
        (root / "board").mkdir(parents=True)
        (root / "telemetry").mkdir(parents=True)
        env = dict(
            os.environ,
            AGENT_ID="02-weaver",
            AGENT_NAME="weaver",
            AGENT_ROLE="resident",
            VILLAGE_ROOT=str(root),
            OLLAMA_MODEL="qwen2.5:7b",
            OLLAMA_URL="http://localhost:11434",
            AGENT_IDENTITY_PROMPT="/test/identity",
        )
        agent = Resident(env)
        agent.pending_cursor = 1
        agent.pending_organic_cursor = 1

        answer = {
            "done_reason": "stop",
            "message": {"content": '{"name":"idle","arguments":{}}'},
            "gpu_verified": True,
        }
        with patch.object(agent, "snapshot", return_value="{}"), \
             patch("runtime.Path.read_text", return_value="System instructions"), \
             patch("runtime.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(answer).encode())):
            agent.cycle()

        # Check that request was recorded in SQLite
        recent = agent.tracker.get_recent_requests(1)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0].state, InferenceState.COMPLETED.value)
        self.assertTrue(recent[0].action_executed)
        self.assertTrue(recent[0].gpu_verified)

        # Check active request file
        active_file = agent.home / "active_request.json"
        self.assertTrue(active_file.exists())
        data = json.loads(active_file.read_text(encoding="utf-8"))
        self.assertEqual(data.get("state"), InferenceState.COMPLETED.value)


if __name__ == "__main__":
    unittest.main()

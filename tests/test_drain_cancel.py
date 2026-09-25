"""Unit tests for P07: Controlled Drain, Abort, and Upstream Cancellation handling."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from village.control import (
    abort_village,
    drain_village,
    get_village_status,
    is_paused,
    pause_village,
    read_pause_metadata,
    resume_village,
    wait_for_drain,
)
from village.inference import cancel_upstream_inference
from village.lifecycle import InferenceState, InferenceTracker
from web.runtime import Resident


class TestDrainAndCancel(unittest.TestCase):
    """Test suite for drain vs abort, in-flight wait, and upstream cancel handling."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.marker_path = self.root / "paused"
        (self.root / "board").mkdir(parents=True)
        (self.root / "telemetry").mkdir(parents=True)
        (self.root / "users").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_pause_modes_drain_and_abort(self) -> None:
        """Verify distinct metadata for drain mode vs abort mode."""
        meta_drain = pause_village(
            marker_path=self.marker_path,
            reason="routine_maintenance",
            operator="admin",
            mode="drain",
        )
        self.assertEqual(meta_drain["mode"], "drain")
        read_drain = read_pause_metadata(self.marker_path)
        self.assertEqual(read_drain.get("mode"), "drain")

        # Now set abort mode
        meta_abort = abort_village(
            marker_path=self.marker_path,
            reason="emergency_halt",
            operator="security_team",
        )
        self.assertEqual(meta_abort["mode"], "abort")
        read_abort = read_pause_metadata(self.marker_path)
        self.assertEqual(read_abort.get("mode"), "abort")
        self.assertEqual(read_abort.get("reason"), "emergency_halt")

    def test_wait_for_drain_detects_active_and_idle(self) -> None:
        """Verify wait_for_drain polls active_request.json until requests complete."""
        king_dir = self.root / "users" / "01-king"
        weaver_dir = self.root / "users" / "02-weaver"
        king_dir.mkdir(parents=True)
        weaver_dir.mkdir(parents=True)

        # 01-king is requesting, 02-weaver is idle (completed)
        king_req = king_dir / "active_request.json"
        king_req.write_text(json.dumps({"state": "requesting"}), encoding="utf-8")
        weaver_req = weaver_dir / "active_request.json"
        weaver_req.write_text(json.dumps({"state": "completed"}), encoding="utf-8")

        # Short timeout: drain should detect active king and timeout
        res_timeout = wait_for_drain(village_root=self.root, timeout_seconds=0.1, poll_interval=0.05)
        self.assertFalse(res_timeout["drained"])
        self.assertEqual(res_timeout["active_count"], 1)
        self.assertIn("01-king", res_timeout["active_agents"])

        # King completes request
        king_req.write_text(json.dumps({"state": "completed"}), encoding="utf-8")

        # Now drain should immediately succeed
        res_success = wait_for_drain(village_root=self.root, timeout_seconds=1.0, poll_interval=0.05)
        self.assertTrue(res_success["drained"])
        self.assertEqual(res_success["active_count"], 0)
        self.assertEqual(res_success["active_agents"], [])

    def test_runtime_cycle_skips_when_paused(self) -> None:
        """Verify Resident.cycle does not start inference when paused."""
        pause_village(marker_path=self.marker_path, reason="test_pause", mode="drain")
        env = dict(
            os.environ,
            AGENT_ID="01-king",
            AGENT_NAME="king",
            AGENT_ROLE="king",
            VILLAGE_ROOT=str(self.root),
            VILLAGE_PAUSE_MARKER=str(self.marker_path),
            OLLAMA_MODEL="qwen2.5:7b",
            OLLAMA_URL="http://localhost:11434",
            AGENT_IDENTITY_PROMPT="/test/identity",
        )
        agent = Resident(env)
        with patch("runtime.urllib.request.urlopen") as mock_url:
            agent.cycle()
            mock_url.assert_not_called()

        # Check telemetry event recorded cycle_skipped_paused
        events_file = self.root / "telemetry" / "agent-events.jsonl"
        self.assertTrue(events_file.exists())
        self.assertIn("cycle_skipped_paused", events_file.read_text())

    def test_runtime_cycle_abort_cancels_request_in_lifecycle(self) -> None:
        """Verify that when abort is active, network exceptions record cancellation in lifecycle."""
        id_file = self.root / "identity.txt"
        id_file.write_text("agent identity prompt", encoding="utf-8")
        sys_prompt_file = self.root / "system-prompt.txt"
        sys_prompt_file.write_text("system prompt", encoding="utf-8")

        env = dict(
            os.environ,
            AGENT_ID="01-king",
            AGENT_NAME="king",
            AGENT_ROLE="king",
            VILLAGE_ROOT=str(self.root),
            VILLAGE_PAUSE_MARKER=str(self.marker_path),
            OLLAMA_MODEL="qwen2.5:7b",
            OLLAMA_URL="http://localhost:11434",
            AGENT_IDENTITY_PROMPT=str(id_file),
        )
        agent = Resident(env)

        def raise_abort_error(*args, **kwargs):
            # Simulate operator abort occurring during inference
            abort_village(marker_path=self.marker_path, reason="operator_emergency")
            raise OSError("Connection closed by peer during abort")

        with patch.object(agent, "snapshot", return_value="{}"), \
             patch("web.runtime.Path", side_effect=lambda p: sys_prompt_file if "system-prompt" in str(p) else Path(p)), \
             patch("runtime.urllib.request.urlopen", side_effect=raise_abort_error):
            agent.cycle()

        # Verify tracker recorded cancellation
        recent = agent.tracker.get_recent_requests(1)
        self.assertEqual(len(recent), 1)
        # Because remote backend verification is unconfirmed, state is UNKNOWN / remote_backend_unverified
        self.assertEqual(recent[0].state, InferenceState.UNKNOWN.value)
        self.assertEqual(recent[0].error_class, "remote_backend_unverified")
        self.assertIn("operator_emergency", recent[0].error_detail)

    def test_cancel_upstream_inference_contract(self) -> None:
        """Verify that cancel_upstream_inference does not fabricate universal cancel endpoints."""
        ok_ollama, reason_ollama = cancel_upstream_inference("ollama", "req_123", "http://localhost:11434")
        self.assertFalse(ok_ollama)
        self.assertIn("does not issue cancellation receipt", reason_ollama)

        ok_openai, reason_openai = cancel_upstream_inference("openai", "req_456", "https://api.openai.com/v1")
        self.assertFalse(ok_openai)
        self.assertIn("do not support out-of-band request cancellation", reason_openai)

    def test_cancellation_confirmation_in_lifecycle(self) -> None:
        """Verify request_cancellation and confirm_cancellation state transitions."""
        db_path = self.root / "lifecycle.sqlite3"
        tracker = InferenceTracker(db_path, "01-king")
        record = tracker.create_request("model", "ollama", 1)
        tracker.start_request(record.request_id)

        # Request cancellation
        c_req = tracker.request_cancellation(record.request_id, "stopping agent")
        self.assertIsNotNone(c_req)
        self.assertEqual(c_req.state, InferenceState.CANCEL_REQUESTED.value)

        # Backend confirmed cancellation -> CANCELLED
        c_conf = tracker.confirm_cancellation(record.request_id, backend_confirmed=True, detail="server confirmed")
        self.assertIsNotNone(c_conf)
        self.assertEqual(c_conf.state, InferenceState.CANCELLED.value)
        self.assertEqual(c_conf.error_class, "cancelled")

        # Another request: Backend unconfirmed -> UNKNOWN
        record2 = tracker.create_request("model", "ollama", 1)
        tracker.start_request(record2.request_id)
        c_unconf = tracker.confirm_cancellation(record2.request_id, backend_confirmed=False, detail="socket closed")
        self.assertEqual(c_unconf.state, InferenceState.UNKNOWN.value)
        self.assertEqual(c_unconf.error_class, "remote_backend_unverified")


if __name__ == "__main__":
    unittest.main()

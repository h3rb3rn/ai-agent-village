"""Unit and integration tests for P11: Error Classification, Loop Detection, and Context Budget."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from web.decision import decision
from web.runtime import Resident, normalize_command


class TestRecoveryLoopDetectionAndBudget(unittest.TestCase):
    """Test suite covering loop detection, paraphrased repetition, polling, and context recovery."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-recovery-test-"))
        self.root = self.tmp / "village"
        self.board = self.root / "board"
        self.telemetry = self.root / "telemetry"
        self.board.mkdir(parents=True, exist_ok=True)
        self.telemetry.mkdir(parents=True, exist_ok=True)

        self.env = {
            "AGENT_ID": "agent_recovery",
            "AGENT_NAME": "recovery_agent",
            "AGENT_ROLE": "engineer",
            "VILLAGE_ROOT": str(self.root),
            "OLLAMA_MODEL": "test-model",
            "OLLAMA_NUM_CTX": "8192",
            "VILLAGE_CYCLE_SECONDS": "120",
            "VILLAGE_COMMAND_TIMEOUT_SECONDS": "5",
            "VILLAGE_PAUSE_MARKER": str(self.tmp / "paused"),
        }
        self.agent = Resident(self.env)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _execute(self, name: str, **kwargs):
        self.agent.execute({"tool_call": {"name": name, "arguments": kwargs}})

    def test_pwd_loop_blocked(self):
        """Verify identical repeated command (pwd) is blocked after threshold."""
        # 1st execution: allowed
        self._execute("execute_bash", command="pwd")
        self.assertTrue(self.agent.state["last_result"]["ok"])

        # 2nd execution: allowed
        self._execute("execute_bash", command="pwd")
        self.assertTrue(self.agent.state["last_result"]["ok"])

        # 3rd execution: blocked by loop detector
        self._execute("execute_bash", command="pwd")
        self.assertFalse(self.agent.state["last_result"]["ok"])
        self.assertIn("Repeated action blocked", self.agent.state["last_result"]["result"])

    def test_paraphrased_repetition_blocked(self):
        """Verify paraphrased shell commands (echo $PWD vs pwd) normalize and trigger guard."""
        self.assertEqual(normalize_command("echo $PWD"), "pwd")
        self.assertEqual(normalize_command('echo "$PWD"'), "pwd")
        self.assertEqual(normalize_command("  /bin/pwd ; "), "pwd")

        # Execute normalized variants
        self._execute("execute_bash", command="pwd")
        self._execute("execute_bash", command="echo $PWD")
        self._execute("execute_bash", command='echo "$PWD"')

        # The third variant must be caught as a repeated loop
        self.assertFalse(self.agent.state["last_result"]["ok"])
        self.assertIn("Repeated action blocked", self.agent.state["last_result"]["result"])

    def test_legitimate_polling_allowed(self):
        """Verify that polling a command whose output changes is not blocked as a loop."""
        counter_file = self.agent.home / "counter.txt"
        counter_file.write_text("1")

        # Polling turn 1: reads 1
        self._execute("execute_bash", command="cat counter.txt")
        self.assertTrue(self.agent.state["last_result"]["ok"])

        # Counter increments
        counter_file.write_text("2")

        # Polling turn 2: reads 2
        self._execute("execute_bash", command="cat counter.txt")
        self.assertTrue(self.agent.state["last_result"]["ok"])

        # Counter increments again
        counter_file.write_text("3")

        # Polling turn 3: output changed again, so it must NOT be blocked!
        self._execute("execute_bash", command="cat counter.txt")
        self.assertTrue(self.agent.state["last_result"]["ok"])
        self.assertNotIn("Repeated action blocked", self.agent.state["last_result"]["result"])

    def test_auth_failure_backoff_and_restart(self):
        """Verify auth failures trigger 15-minute backoff and survive process restart."""
        # Initial delay is base delay (120s)
        self.assertEqual(self.agent.compute_cycle_delay(), 120)

        # Flag auth failure in state
        self.agent.state["auth_failure"] = True
        self.agent.state["last_error_category"] = "auth_error"
        self.agent.feedback("auth_error", "Invalid API token", False)

        # Delay increases to 900s (15 min)
        self.assertEqual(self.agent.compute_cycle_delay(), 900)

        # Re-instantiate agent from disk (restart)
        restarted_agent = Resident(self.env)
        self.assertTrue(restarted_agent.state.get("auth_failure"))
        self.assertEqual(restarted_agent.compute_cycle_delay(), 900)

    def test_invalid_streak_backoff_survives_restart(self):
        """Verify 3 consecutive invalid decisions trigger 900s backoff across restarts."""
        for _ in range(3):
            self.agent.execute(decision({"message": {"content": '{"name":'}}))

        self.assertEqual(self.agent.state["invalid_streak"], 3)
        self.assertEqual(self.agent.compute_cycle_delay(), 900)

        # Reload after restart
        restarted = Resident(self.env)
        self.assertEqual(restarted.state.get("invalid_streak"), 3)
        self.assertEqual(restarted.compute_cycle_delay(), 900)

    def test_token_budget_and_recovery_guidance_in_snapshot(self):
        """Verify snapshot includes token budget metadata and recovery guidance on previous error."""
        # Simulate previous failed action
        self.agent.feedback("execute_bash", "No such file or directory: /opt/missing.py", False)

        with patch.object(self.agent, "memory", return_value={"items": []}):
            snapshot_data = json.loads(self.agent.snapshot())

        # Token budget is present and explicitly labeled
        self.assertIn("token_budget", snapshot_data)
        tb = snapshot_data["token_budget"]
        self.assertEqual(tb["context_window_tokens"], 8192)
        self.assertEqual(tb["token_count_mode"], "estimated")
        self.assertEqual(tb["kv_cache_status"], "remote_managed")

        # Recovery guidance is present to guide away from blind retries
        self.assertIn("recovery_guidance", snapshot_data)
        rg = snapshot_data["recovery_guidance"]
        self.assertEqual(rg["failed_action"], "execute_bash")
        self.assertIn("No such file or directory", rg["error"])
        self.assertIn("Formulate a revised hypothesis", rg["directive"])


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from village.firewatch import GuardThresholds, append_alerts, observe


class FirewatchTests(unittest.TestCase):
    def test_observe_reports_pressure_and_down_services(self):
        with patch("village.firewatch.shutil.disk_usage", return_value=SimpleNamespace(free=5 * 1024 ** 3)), \
             patch("village.firewatch._memory_available_mib", return_value=512), \
             patch("village.firewatch._load_per_cpu", return_value=3.0):
            result = observe(thresholds=GuardThresholds(min_disk_free_gib=10), service_checker=lambda _: False)
        codes = {item["code"] for item in result["alerts"]}
        self.assertTrue({"memory_pressure", "disk_pressure", "load_pressure"} <= codes)
        self.assertIn("service_memory_gateway_down", codes)
        self.assertEqual(result["mode"], "read_only_escalation")

    def test_append_alerts_is_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "firewatch.jsonl"
            self.assertEqual(append_alerts({"timestamp": "now", "alerts": [{"code": "x"}]}, path), 1)
            self.assertIn('"source": "firewatch"', path.read_text())

    def test_no_alert_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "firewatch.jsonl"
            self.assertEqual(append_alerts({"alerts": []}, path), 0)
            self.assertFalse(path.exists())

class AgentHealthTests(unittest.TestCase):
    def rows(self, agent, events, start=1000.0):
        from datetime import datetime, timezone
        return [{"agent": agent, "event": e, "timestamp": datetime.fromtimestamp(start + i * 10, timezone.utc).isoformat()}
                for i, e in enumerate(events)]

    def test_repeated_runtime_exceptions_are_reported_as_wedged(self):
        from village.firewatch import agent_health
        rows = self.rows("09-chronicler", ["agent_start", "inference_finished"] + ["runtime_exception"] * 3)
        alerts = agent_health(rows, now=1100.0)
        self.assertEqual([a["code"] for a in alerts], ["agent_wedged"])
        self.assertEqual(alerts[0]["agent"], "09-chronicler")

    def test_recovery_clears_wedged_state(self):
        from village.firewatch import agent_health
        rows = self.rows("a", ["runtime_exception"] * 3 + ["inference_finished"])
        self.assertEqual(agent_health(rows, now=1100.0), [])

    def test_stalled_agent_without_finished_inference(self):
        from village.firewatch import agent_health
        silent = self.rows("a", ["inference_finished"])
        self.assertEqual(agent_health(silent, now=1000.0 + 5000), [])  # no recent activity at all: silent, not stalled
        fresh = self.rows("a", ["inference_finished"], start=1000.0) + self.rows("a", ["inference_started"], start=1000.0 + 1900)
        codes = [a["code"] for a in agent_health(fresh, now=1000.0 + 2000)]
        self.assertEqual(codes, ["agent_stalled"])

    def test_healthy_agents_produce_no_alert(self):
        from village.firewatch import agent_health
        self.assertEqual(agent_health(self.rows("a", ["inference_started", "inference_finished"]), now=1100.0), [])

    def test_observe_skips_agent_checks_while_paused(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            events = Path(tmp) / "agent-events.jsonl"
            rows = self.rows("a", ["runtime_exception"] * 3)
            events.write_text("".join(json.dumps(r) + "\n" for r in rows))
            paused = Path(tmp) / "paused"; paused.write_text("x")
            with patch("village.firewatch.shutil.disk_usage", return_value=SimpleNamespace(free=50 * 1024 ** 3)), \
                 patch("village.firewatch._memory_available_mib", return_value=8000), \
                 patch("village.firewatch._load_per_cpu", return_value=0.1):
                quiet = observe(service_checker=lambda _: True, agent_events=events, pause_marker=paused, now=1100.0)
                loud = observe(service_checker=lambda _: True, agent_events=events, pause_marker=Path(tmp) / "none", now=1100.0)
        self.assertEqual(quiet["alerts"], [])
        self.assertEqual([a["code"] for a in loud["alerts"]], ["agent_wedged"])


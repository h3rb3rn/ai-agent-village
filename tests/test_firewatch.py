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

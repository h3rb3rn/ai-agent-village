"""web/telemetry-collector.py: the auditor has no HTTP service of its own, so
the collector reads its SQLite log directly. This covers that one new code
path in isolation - not the collector's live host loop, which needs systemd,
Ollama and nvidia-smi and is exercised on N06-M10 itself."""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]


def _load_telemetry_collector(village_root: Path):
    os.environ["VILLAGE_ROOT"] = str(village_root)
    sys.path.insert(0, str(ROOT_DIR))
    sys.path.insert(0, str(ROOT_DIR / "web"))
    spec = importlib.util.spec_from_file_location(
        "telemetry_collector_under_test", ROOT_DIR / "web" / "telemetry-collector.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class AuditorStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-telemetry-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(os.environ.pop, "VILLAGE_ROOT", None)
        self.module = _load_telemetry_collector(self.tmp)

    def test_no_cycle_run_yet_reports_a_clean_zero_state_not_an_error(self):
        status = self.module.auditor_status()
        self.assertNotIn("error", status)
        self.assertEqual(status["delivered_total"], 0)
        self.assertEqual(status["cycles_run"], 0)
        self.assertIsNone(status["last_cycle_at"])

    def test_reflects_real_audit_log_content(self):
        from village.auditor import AuditStore, detect_foreign_home_access

        def event(agent, ev, detail, event_id):
            return {"agent": agent, "event": ev, "detail": detail, "event_id": event_id,
                    "timestamp": "2026-09-28T00:00:00+00:00"}

        db_path = self.tmp / "telemetry" / "audit.sqlite3"
        store = AuditStore(db_path)
        finding = next(iter(detect_foreign_home_access([
            event("01-king", "foreign_home_blocked", "target_agent=08-logician; command_prefix=x", "e1")
        ])))
        store.route(finding)
        store.record_cycle({"deterministic_findings": 1, "deterministic_delivered": 1,
                            "llm_candidates": 0, "llm_findings": 0, "llm_delivered": 0, "llm_errors": 0})

        status = self.module.auditor_status()
        self.assertEqual(status["delivered_total"], 1)
        self.assertEqual(status["cycles_run"], 1)
        self.assertIsNotNone(status["last_cycle_at"])


if __name__ == "__main__":
    unittest.main()

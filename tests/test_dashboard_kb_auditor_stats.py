"""P35/P36: the operator asked two concrete dashboard questions - how much
personal versus community knowledge exists in the substrate, and how much the
auditor had to intervene, split by the free deterministic layer versus the
independent LLM review (or neither, when the LLM produced no usable verdict).
This exercises the real renderMemoryRatio()/renderAuditor() functions from
observatory.js end to end, DOM-free, against representative gateway/auditor
payload shapes."""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HARNESS = Path(__file__).resolve().parent / "fixtures" / "dashboard_stats_harness.js"


@unittest.skipUnless(shutil.which("node"), "Node.js not available on PATH")
class DashboardStatsTests(unittest.TestCase):
    def render(self, payload):
        result = subprocess.run(
            ["node", str(HARNESS), str(ROOT / "web/observatory.js"), json.dumps(payload)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.strip())

    def test_knowledgebase_ratio_shows_private_shared_split_and_percentages(self):
        out = self.render({"stats": {"total": 9023, "by_scope": {"private": 354, "shared": 8669},
                                     "agents": [{"agent": "dataset-import", "memories": 8669}]}})
        html = out["memoryRatio"]
        self.assertIn("9.023 Einträge gesamt", html)
        self.assertIn("354", html)
        self.assertIn("8.669", html)
        self.assertIn("96 %", html)  # shared share, rounded
        self.assertIn("aus Wissensimport", html)
        self.assertIn("von Agenten selbst geschrieben", html)

    def test_knowledgebase_ratio_handles_the_empty_substrate_without_crashing(self):
        out = self.render({"stats": {"total": 0, "by_scope": {"private": 0, "shared": 0}, "agents": []}})
        self.assertIn("Noch keine Einträge", out["memoryRatio"])

    def test_auditor_not_yet_run_is_reported_honestly_not_as_an_error(self):
        out = self.render({"auditor": {"cycles_run": 0, "delivered_total": 0, "last_cycle_at": None}})
        self.assertIn("Noch kein Zyklus gelaufen", out["auditorSummary"])
        self.assertEqual(out["auditorCategories"], "")

    def test_auditor_summary_splits_deterministic_versus_llm_and_shows_unresolved(self):
        out = self.render({"auditor": {
            "cycles_run": 12, "last_cycle_at": "2026-09-28T00:00:00+00:00",
            "delivered_total": 5, "delivered_by_source": {"deterministic": 3, "llm": 2},
            "delivered_by_scope": {"private": 4, "shared": 1},
            "delivered_by_category": {"foreign_home_access": 3, "format_violation": 2},
            "llm_candidates": 6, "llm_findings": 2, "llm_unresolved": 4,
        }})
        summary = out["auditorSummary"]
        self.assertIn("5 Eingriffe gesamt", summary)
        self.assertIn("3", summary)  # deterministic count
        self.assertIn("per Python-Skript", summary)
        self.assertIn("per LLM", summary)
        self.assertIn("6 LLM-Kandidaten", summary)
        self.assertIn("4", summary)  # unresolved count present
        self.assertIn("ohne verwertbares Urteil", summary)
        categories = out["auditorCategories"]
        self.assertIn("Persönlich", categories)
        self.assertIn("Gemeinschaftswissen", categories)
        self.assertIn("Fremdes Verzeichnis", categories)
        self.assertIn("Formatfehler", categories)


if __name__ == "__main__":
    unittest.main()

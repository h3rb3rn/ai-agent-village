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

    # P89: the dashboard's default rendered language changed from German to
    # English (the WebUI chrome used to be German-only while the content was
    # already English; an i18n layer now makes English the default with
    # German selectable) - these assertions were updated to the new default
    # English strings accordingly. The harness's `fetch` always resolves
    # `{ok:false}` (no language file reachable in this DOM-free sandbox), so
    # this also exercises observatory.js's built-in English literal fallback.

    def test_knowledgebase_ratio_shows_private_shared_split_and_percentages(self):
        out = self.render({"stats": {"total": 9023, "by_scope": {"private": 354, "shared": 8669},
                                     "agents": [{"agent": "dataset-import", "memories": 8669}]}})
        html = out["memoryRatio"]
        self.assertIn("9,023 entries total", html)
        self.assertIn("354", html)
        self.assertIn("8,669", html)
        self.assertIn("96 %", html)  # shared share, rounded
        self.assertIn("from knowledge import", html)
        self.assertIn("written by agents themselves", html)

    def test_knowledgebase_ratio_handles_the_empty_substrate_without_crashing(self):
        out = self.render({"stats": {"total": 0, "by_scope": {"private": 0, "shared": 0}, "agents": []}})
        self.assertIn("No entries in the memory substrate yet", out["memoryRatio"])

    def test_auditor_not_yet_run_is_reported_honestly_not_as_an_error(self):
        out = self.render({"auditor": {"cycles_run": 0, "delivered_total": 0, "last_cycle_at": None}})
        self.assertIn("No cycle has run yet", out["auditorSummary"])
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
        self.assertIn("5 interventions total", summary)
        self.assertIn("3", summary)  # deterministic count
        self.assertIn("via Python script", summary)
        self.assertIn("via LLM", summary)
        self.assertIn("6 LLM candidates", summary)
        self.assertIn("4", summary)  # unresolved count present
        self.assertIn("without an actionable verdict", summary)
        categories = out["auditorCategories"]
        self.assertIn("Personal", categories)
        self.assertIn("Shared knowledge", categories)
        self.assertIn("Foreign directory", categories)
        self.assertIn("Format error", categories)


if __name__ == "__main__":
    unittest.main()

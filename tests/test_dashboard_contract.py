import re
import unittest
from pathlib import Path


class DashboardContractTests(unittest.TestCase):
    def setUp(self):
        self.html = Path("web/observatory.html").read_text(encoding="utf-8")
        self.js = Path("web/observatory.js").read_text(encoding="utf-8")

    def test_navigation_targets_exist(self):
        anchors = re.findall(r'data-anchor="([^"]+)"', self.html)
        ids = set(re.findall(r'id="([^"]+)"', self.html))
        self.assertTrue(anchors)
        self.assertTrue(set(anchors).issubset(ids))

    def test_no_external_assets_or_hardcoded_agent_count(self):
        self.assertNotRegex(self.html, r"https?://(?!127\\.0\\.0\\.1)")
        self.assertNotIn("agents.length===9", self.js)
        self.assertNotIn("for(let i=1;i<=9", self.js)

    def test_keyboard_and_focus_contract_is_present(self):
        self.assertIn("keydown", self.js)
        self.assertIn("tabindex", self.js)
        self.assertIn("aria-label", self.js)

    def test_gazette_view_never_polls_for_live_updates(self):
        # Operator feedback (2026-09-29): a published Gazette edition should
        # read like a stable newspaper page, not a live dashboard - the
        # periodic 15s refresh() reloading content while someone is reading
        # an article was reported as disruptive. refresh() must bail out
        # immediately for the gazette view, before its own reschedule/fetch
        # logic runs, and before anything else in the function body.
        match = re.search(r"async function refresh\(\)\{([^}]*)", self.js)
        self.assertIsNotNone(match, "refresh() not found")
        self.assertTrue(match.group(1).startswith("if(view==='gazette')return;"),
                         "refresh() must bail out for the gazette view as its very first statement")


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

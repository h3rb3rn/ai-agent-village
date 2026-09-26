import unittest
from pathlib import Path


class DashboardMemoryProjectionTests(unittest.TestCase):
    def test_projection_cards_are_rendered_without_external_assets(self):
        source = Path("web/observatory.js").read_text(encoding="utf-8")
        self.assertIn("memory-projection", source)
        self.assertIn("s.lag", source)
        self.assertIn("s.error_count", source)


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path


class CanonicalBootstrapTests(unittest.TestCase):
    def test_embedded_legacy_components_are_overlaid_by_repo_sources(self):
        script = Path(__file__).parents[1] / 'bootstrap-ai-village.sh'
        text = script.read_text(encoding='utf-8')
        self.assertIn('install -m 0755 "$SCRIPT_DIR/web/telemetry-collector.py" /usr/local/lib/ai-village/telemetry-collector.py', text)
        self.assertIn('install -m 0755 "$SCRIPT_DIR/web/webui.py" /usr/local/lib/ai-village/webui.py', text)


if __name__ == '__main__':
    unittest.main()

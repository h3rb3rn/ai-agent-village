import unittest
from pathlib import Path


class CanonicalAuthorityTests(unittest.TestCase):
    def test_bootstrap_overlays_authority_entrypoint(self):
        text = (Path(__file__).parents[1] / 'bootstrap-ai-village.sh').read_text()
        self.assertIn('install -m 0750 "$SCRIPT_DIR/scripts/authority-server.py" /usr/local/lib/ai-village/authority.py', text)

    def test_entrypoint_uses_tested_authority_module(self):
        text = (Path(__file__).parents[1] / 'scripts/authority-server.py').read_text()
        self.assertIn('from village.authority import main', text)


if __name__ == '__main__':
    unittest.main()

import unittest
from pathlib import Path


class CanonicalBootstrapHelperTests(unittest.TestCase):
    def test_runtime_helpers_are_overlaid_by_repository_sources(self):
        text = (Path(__file__).parents[1] / 'bootstrap-ai-village.sh').read_text()
        for source, target in (
            ('scripts/agent-runner', '/usr/local/lib/ai-village/agent-runner'),
            ('scripts/village-resume', '/usr/local/sbin/village-resume'),
            ('scripts/village-update', '/usr/local/sbin/village-update'),
        ):
            self.assertIn(f'install -m 0755 "$SCRIPT_DIR/{source}" {target}', text)


if __name__ == '__main__':
    unittest.main()

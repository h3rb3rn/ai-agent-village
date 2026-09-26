import unittest

from village.release import RELEASE_FILES


class ReleaseManifestScopeTests(unittest.TestCase):
    def test_live_governance_components_are_manifested(self):
        sources = {src for src, _, _ in RELEASE_FILES}
        for required in (
            'village/meetings.py', 'village/research.py',
            'village/firewatch.py', 'village/teams.py',
            'scripts/authority-server.py',
        ):
            self.assertIn(required, sources)


if __name__ == '__main__':
    unittest.main()

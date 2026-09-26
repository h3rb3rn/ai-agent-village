import unittest
from pathlib import Path


class RebootResumeContractTests(unittest.TestCase):
    def test_resume_uses_lock_and_persistent_pause_marker(self):
        source = Path("scripts/village-resume").read_text(encoding="utf-8")
        self.assertIn("flock -n", source)
        self.assertIn("VILLAGE_PAUSE_MARKER", source)
        self.assertIn("resume_skipped", source)

    def test_bootstrap_units_keep_pause_condition(self):
        source = Path("bootstrap-ai-village.sh").read_text(encoding="utf-8")
        self.assertIn("ConditionPathExists=!/etc/ai-village/paused", source)
        self.assertIn("ai-village-resume", source)

    def test_resume_does_not_remove_pause_marker(self):
        source = Path("scripts/village-resume").read_text(encoding="utf-8")
        self.assertNotIn("rm -f $PAUSE_MARKER", source)
        self.assertNotIn("rm -f \"$PAUSE_MARKER\"", source)


if __name__ == "__main__":
    unittest.main()

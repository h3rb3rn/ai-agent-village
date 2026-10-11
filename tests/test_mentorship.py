"""P119: mentor pairs and the completion measure."""
import unittest

from village.mentorship import MENTOR_PAIRS, completions, mentor_for, mentor_note


class MentorshipTests(unittest.TestCase):
    def test_the_two_pairs_are_the_chosen_ones(self):
        self.assertEqual(MENTOR_PAIRS, {"04-artisan": "06-operator", "08-logician": "07-methodologist"})
        self.assertEqual(mentor_for("04-artisan"), "06-operator")
        self.assertIsNone(mentor_for("02-explorer"))

    def test_mentor_note_names_the_learner_and_the_task(self):
        note = mentor_note("04-artisan", "abc123", "Disk monitor")
        self.assertIn("04-artisan", note)
        self.assertIn("abc123", note)

    def test_completions_count_only_the_learner_and_the_window(self):
        revs = [{"actor": "04-artisan", "action": "complete", "timestamp": "2026-10-10T09:00"},
                {"actor": "04-artisan", "action": "claim", "timestamp": "2026-10-10T09:00"},
                {"actor": "04-artisan", "action": "complete", "timestamp": "2026-10-09T09:00"},
                {"actor": "06-operator", "action": "complete", "timestamp": "2026-10-10T09:00"}]
        self.assertEqual(completions(revs, "04-artisan"), 2)
        self.assertEqual(completions(revs, "04-artisan", since="2026-10-10"), 1)


if __name__ == "__main__":
    unittest.main()

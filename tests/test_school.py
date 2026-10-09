"""P105: the curriculum is always planned one week ahead."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from village.school import HORIZON_DAYS, Curriculum

TODAY = date(2026, 10, 10)


class CurriculumTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plan = Curriculum(Path(self.tmp.name) / "school.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_full_week_ahead_has_no_gaps_when_every_day_is_planned(self):
        for offset in range(1, HORIZON_DAYS + 1):
            self.plan.add(date.fromordinal(TODAY.toordinal() + offset), f"topic {offset}", "01-king", TODAY)
        self.assertEqual(self.plan.gaps(TODAY), [])

    def test_empty_plan_reports_the_whole_week_as_gaps(self):
        self.assertEqual(len(self.plan.gaps(TODAY)), HORIZON_DAYS)

    def test_a_lesson_beyond_the_horizon_is_refused(self):
        with self.assertRaises(ValueError):
            self.plan.add(date.fromordinal(TODAY.toordinal() + HORIZON_DAYS + 1), "too far", "01-king", TODAY)

    def test_a_lesson_for_today_or_the_past_is_refused(self):
        with self.assertRaises(ValueError):
            self.plan.add(TODAY, "today", "01-king", TODAY)

    def test_the_gap_moves_forward_as_days_pass(self):
        for offset in range(1, HORIZON_DAYS + 1):
            self.plan.add(date.fromordinal(TODAY.toordinal() + offset), f"topic {offset}", "01-king", TODAY)
        tomorrow = date.fromordinal(TODAY.toordinal() + 1)
        self.assertEqual(self.plan.gaps(tomorrow), [date.fromordinal(TODAY.toordinal() + HORIZON_DAYS + 1)])

    def test_empty_topic_is_refused(self):
        with self.assertRaises(ValueError):
            self.plan.add(date.fromordinal(TODAY.toordinal() + 1), "   ", "01-king", TODAY)


if __name__ == "__main__":
    unittest.main()

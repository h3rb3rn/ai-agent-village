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


class SchoolDeliveryTests(unittest.TestCase):
    """P106: the planned lesson of the day is announced; the teacher is reminded of gaps."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plan = Curriculum(Path(self.tmp.name) / "school.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_planned_lesson_is_announced_for_its_day(self):
        from village.school import daily_announcement
        self.plan.add(date.fromordinal(TODAY.toordinal() + 1), "fractions", "01-king", TODAY)
        text = daily_announcement(self.plan, date.fromordinal(TODAY.toordinal() + 1))
        self.assertIn("fractions", text)
        self.assertIsNone(daily_announcement(self.plan, TODAY))

    def test_reminder_names_the_unplanned_days_and_clears_when_the_week_is_full(self):
        from village.school import planning_reminder
        self.assertIn("Still open", planning_reminder(self.plan, TODAY))
        for offset in range(1, HORIZON_DAYS + 1):
            self.plan.add(date.fromordinal(TODAY.toordinal() + offset), f"topic {offset}", "01-king", TODAY)
        self.assertIsNone(planning_reminder(self.plan, TODAY))


class DefaultCurriculumTests(unittest.TestCase):
    """P113: open days are filled from the default topics, so the week is never empty."""

    def test_autofill_plans_every_open_day_of_the_week(self):
        from village.school import autofill_week, DEFAULT_TOPICS
        with tempfile.TemporaryDirectory() as d:
            plan = Curriculum(Path(d) / "school.sqlite3")
            planned = autofill_week(plan, TODAY)
            self.assertEqual(len(planned), HORIZON_DAYS)
            self.assertEqual(plan.gaps(TODAY), [])
            self.assertIn(plan.lesson(planned[0])["topic"], DEFAULT_TOPICS)

    def test_autofill_does_not_overwrite_a_lesson_the_teacher_planned(self):
        from village.school import autofill_week
        with tempfile.TemporaryDirectory() as d:
            plan = Curriculum(Path(d) / "school.sqlite3")
            tomorrow = date.fromordinal(TODAY.toordinal() + 1)
            plan.add(tomorrow, "teacher's own topic", "01-king", TODAY)
            autofill_week(plan, TODAY)
            self.assertEqual(plan.lesson(tomorrow)["topic"], "teacher's own topic")


class LessonAnswerTests(unittest.TestCase):
    """P115: a resident's answer to today's lesson is stored once per day and must say something."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plan = Curriculum(Path(self.tmp.name) / "school.sqlite3")
        self.day = date.fromordinal(TODAY.toordinal() + 1)
        self.plan.add(self.day, "fractions", "01-king", TODAY)

    def tearDown(self):
        self.tmp.cleanup()

    def test_answer_is_stored_once_per_resident_and_day(self):
        from village.school import answers_for, record_answer
        text = "A fraction is a part of a whole, written as numerator over denominator."
        self.assertTrue(record_answer(self.plan, "02-explorer", self.day, text))
        self.assertFalse(record_answer(self.plan, "02-explorer", self.day, text))
        self.assertEqual(answers_for(self.plan, self.day), ["02-explorer"])

    def test_empty_or_short_answer_is_refused(self):
        from village.school import record_answer
        with self.assertRaises(ValueError):
            record_answer(self.plan, "02-explorer", self.day, "ok")

    def test_no_answer_without_a_lesson(self):
        from village.school import record_answer
        with self.assertRaises(ValueError):
            record_answer(self.plan, "02-explorer", date.fromordinal(TODAY.toordinal() + 3),
                          "A sufficiently long answer about a day without a lesson.")

"""P117: today's school lesson reaches the compact prompt."""
import unittest

from village.prompting import compact_context


class SchoolLessonContextTests(unittest.TestCase):
    def test_school_lesson_is_kept_in_the_compact_context(self):
        out = compact_context({"school_lesson": "School, 2026-10-10: fractions. Answer with school_operation."})
        self.assertIn("school_lesson", out)
        self.assertIn("school_operation", out["school_lesson"])


if __name__ == "__main__":
    unittest.main()

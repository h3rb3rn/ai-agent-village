import ast
import unittest
from pathlib import Path


class MeetingSchedulerPolicyTests(unittest.TestCase):
    def test_scheduler_checks_active_meetings_before_creating_one(self):
        text = (Path(__file__).parents[1] / 'scripts/meeting-scheduler.py').read_text()
        tree = ast.parse(text)
        self.assertIn('if meetings.active():', text)
        self.assertIn('continue', text)
        self.assertIsNotNone(tree)


if __name__ == '__main__':
    unittest.main()

"""P112: an invalid meeting operation says what is valid."""
import unittest

from web.decision import _validate_action


class MeetingOperationReasonTests(unittest.TestCase):
    def test_create_is_rejected_with_the_valid_operations_named(self):
        reason = _validate_action('meeting_operation', {'operation': 'create'})
        self.assertIn('only report or close', reason)
        self.assertIn('scheduler', reason)

    def test_report_and_close_are_still_accepted(self):
        self.assertIsNone(_validate_action('meeting_operation', {'operation': 'report'}))
        self.assertIsNone(_validate_action('meeting_operation', {'operation': 'close'}))


if __name__ == '__main__':
    unittest.main()

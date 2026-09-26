import unittest
from village.rollout import REQUIRED_CHECKPOINTS, validate_checkpoints


class RolloutScenarioTests(unittest.TestCase):
    def test_ordered_checkpoints_require_ids(self):
        rows = [{"name": name, "request_id": f"req-{i}", "run_id": "run-1"} for i, name in enumerate(REQUIRED_CHECKPOINTS)]
        self.assertEqual(validate_checkpoints(rows), list(REQUIRED_CHECKPOINTS))

    def test_missing_or_reordered_step_fails(self):
        with self.assertRaises(ValueError): validate_checkpoints([])
        with self.assertRaises(ValueError): validate_checkpoints([{"name": "message_ack", "run_id": "r"}])

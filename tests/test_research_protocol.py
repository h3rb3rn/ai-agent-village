import unittest

from village.research_protocol import ExperimentCondition, wilson_interval


class ResearchProtocolTests(unittest.TestCase):
    def test_condition_records_revisions_and_budget(self):
        condition = ExperimentCondition("heterogeneous", "model-r1", "prompt-r2", "runtime-r3", 1200, "none")
        record = condition.to_record()
        self.assertEqual(record["token_budget"], 1200)
        self.assertEqual(record["intervention"], "none")

    def test_invalid_condition_and_denominator_fail_closed(self):
        with self.assertRaises(ValueError):
            ExperimentCondition("", "m", "p", "r", 1).to_record()
        with self.assertRaises(ValueError):
            wilson_interval(0, 0)

    def test_wilson_interval_contains_observed_rate(self):
        low, high = wilson_interval(7, 10)
        self.assertLessEqual(low, 0.7)
        self.assertGreaterEqual(high, 0.7)


if __name__ == "__main__":
    unittest.main()

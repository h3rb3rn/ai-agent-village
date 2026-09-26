import unittest
from village.rollout import REQUIRED_CHECKPOINTS, validate_evidence_bundle


class RolloutEvidenceTests(unittest.TestCase):
    def test_mock_bundle_is_execution_free(self):
        rows = [{"name": name, "request_id": f"req-{i}", "run_id": "run-mock", "evidence_ref": f"synthetic/{i}"} for i, name in enumerate(REQUIRED_CHECKPOINTS)]
        result = validate_evidence_bundle(rows)
        self.assertEqual(result["execution"], 0)
        self.assertEqual(result["checkpoints"], len(REQUIRED_CHECKPOINTS))

    def test_missing_evidence_fails(self):
        rows = [{"name": name, "request_id": f"req-{i}", "run_id": "run-mock"} for i, name in enumerate(REQUIRED_CHECKPOINTS)]
        with self.assertRaises(ValueError): validate_evidence_bundle(rows)

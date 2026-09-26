import json
import tempfile
import unittest
from pathlib import Path

from village.interventions import record_intervention


class InterventionTests(unittest.TestCase):
    def test_record_is_append_only_and_versioned(self):
        with tempfile.TemporaryDirectory() as td:
            event = record_intervention(Path(td) / "interventions.jsonl", actor="operator", scope="prompt", reason="calibration", before="r1", after="r2", run_id="run-1")
            self.assertEqual(event["schema_version"], "1.0")
            row = json.loads((Path(td) / "interventions.jsonl").read_text())
            self.assertEqual(row["intervention_id"], event["intervention_id"])

    def test_missing_context_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                record_intervention(Path(td) / "x", actor="", scope="prompt", reason="x", before="a", after="b", run_id="r")


if __name__ == "__main__":
    unittest.main()

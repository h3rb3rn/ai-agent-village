import unittest
from village.lineage import LineageArtifact, child_gate


def artifact():
    return LineageArtifact("child-1", "agent-1", "skill", "r1", "dataset:synth-1", "eval:run-1", "license:synthetic")


class LineageTests(unittest.TestCase):
    def test_lineage_record_contains_provenance(self): self.assertEqual(artifact().record()["parent_id"], "agent-1")
    def test_gate_is_deferred_until_prerequisites(self):
        with self.assertRaises(PermissionError): child_gate(lineage=artifact(), gpu_budget_minutes=10, max_children=1, completed_prerequisites={"P13"})
    def test_approved_gate_is_planning_only(self):
        result = child_gate(lineage=artifact(), gpu_budget_minutes=10, max_children=1, completed_prerequisites={"P13", "P19", "P24", "P25", "P26"})
        self.assertEqual(result["action"], "planned_only")

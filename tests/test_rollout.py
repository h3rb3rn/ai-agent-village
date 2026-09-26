import unittest
from village.rollout import CanaryPlan, prepare_canary


class RolloutTests(unittest.TestCase):
    def test_canary_requires_explicit_approval(self):
        with self.assertRaises(PermissionError): prepare_canary(CanaryPlan("agent-1", "rev-1"))

    def test_approved_plan_is_planning_only(self):
        result = prepare_canary(CanaryPlan("agent-1", "rev-1", operator_approved=True))
        self.assertEqual(result["action"], "planned_only")

    def test_unpaused_plan_is_rejected(self):
        with self.assertRaises(ValueError): prepare_canary(CanaryPlan("agent-1", "rev-1", operator_approved=True, village_paused=False))

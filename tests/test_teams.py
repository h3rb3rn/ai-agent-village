import sqlite3
import tempfile
import unittest
from pathlib import Path

from village.teams import TeamStore


class TeamStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TeamStore(Path(self.tmp.name) / "coordination.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_multiple_agents_share_role_and_keep_subtasks(self):
        team = self.store.create("01-alpha", {
            "project": "archive",
            "goal": "build a reproducible semantic archive",
            "role": "researcher",
            "coordination_mode": "parallel",
        })
        self.store.join("02-beta", team["id"], "retrieval")
        self.store.join("03-gamma", team["id"], "verification")
        sub = self.store.create_subtask("02-beta", team["id"], {
            "title": "test retrieval",
            "criterion": "reproduce one known query with evidence",
        })
        self.store.claim_subtask("02-beta", sub["id"])
        result = self.store.complete_subtask("02-beta", sub["id"], "fixture query returned 3 verified records")
        self.assertEqual(result["status"], "complete")
        current = self.store.get(team["id"])
        self.assertEqual([m["agent_id"] for m in current["members"]], ["01-alpha", "02-beta", "03-gamma"])
        self.assertEqual(current["role"], "researcher")

    def test_society_can_change_team_role_by_majority(self):
        team = self.store.create("01-alpha", {"project": "p", "goal": "goal with evidence", "role": "builder"})
        self.store.join("02-beta", team["id"])
        self.store.join("03-gamma", team["id"])
        proposal = self.store.propose_role("02-beta", team["id"], "critic", "independent review is currently the bottleneck")
        self.store.vote_role("01-alpha", proposal["id"], "accept")
        decided = self.store.vote_role("03-gamma", proposal["id"], "accept")
        self.assertEqual(decided["status"], "accepted")
        self.assertEqual(self.store.get(team["id"])["role"], "critic")

    def test_subtask_claim_is_not_global_task_claim(self):
        team = self.store.create("01-alpha", {"project": "p", "goal": "goal with evidence", "role": "researcher"})
        sub = self.store.create_subtask("01-alpha", team["id"], {"title": "one", "criterion": "observable result exists"})
        self.store.join("02-beta", team["id"])
        self.store.claim_subtask("02-beta", sub["id"])
        with self.assertRaises(ValueError):
            self.store.claim_subtask("01-alpha", sub["id"])


if __name__ == "__main__":
    unittest.main()

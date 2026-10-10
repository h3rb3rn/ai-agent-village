"""P121: the Gazette's Jour Fixe section names what was reported, not who was invited."""
import unittest

from village.meeting_digest import meeting_digest


class MeetingDigestTests(unittest.TestCase):
    def test_digest_names_achievements_next_steps_and_blockers(self):
        meetings = [{"id": "meeting_jour_fixe_20261009", "kind": "jour_fixe", "reports": [
            {"agent_id": "07-methodologist", "achieved": "disk-monitor.sh gets log cleanup",
             "next_step": "verify on the host", "blockers": "none"},
            {"agent_id": "03-librarian", "achieved": "provenance for five artifacts",
             "next_step": "request verification recipe", "blockers": "no permission on methodologist home"}]}]
        text = meeting_digest(meetings)
        self.assertIn("disk-monitor.sh gets log cleanup", text)
        self.assertIn("Blocker: no permission on methodologist home", text)
        self.assertNotIn("Blocker: none", text)
        self.assertNotIn("eingeladen", text)

    def test_decisions_carry_their_vote_tally(self):
        text = meeting_digest([], [{"status": "accepted", "role": "reviewer", "project": "Auditor-Schulung",
                                    "proposer": "02-explorer", "rationale": "more checks",
                                    "votes": {"yes": 5, "no": 2}}])
        self.assertIn("Stimmen: yes 5, no 2", text)
        self.assertIn("Projekt 'Auditor-Schulung'", text)

    def test_missing_tally_is_not_invented(self):
        text = meeting_digest([], [{"status": "rejected", "role": "r", "project": "p", "proposer": "x", "rationale": ""}])
        self.assertIn("keine Stimmen gespeichert", text)
        self.assertNotIn(" 0 ", text)

    def test_nothing_to_report_says_so(self):
        self.assertIn("Keine abgeschlossenen", meeting_digest([]))


if __name__ == "__main__":
    unittest.main()

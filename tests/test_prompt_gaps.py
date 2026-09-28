"""P21.21: prompt clarifications for two failure modes observed live on N06-M10
(2026-09-27, post-fix measurement window): agents tried to fix a peer's problem by
running commands against the peer's private home path from their own account (always
fails on Unix permissions), and one agent copied the runtime's own corrective
feedback sentence into execute_bash as if it were a shell command. Both prompts
(full and compact) must say this explicitly; this does not touch decision.py or
runtime.py, only the constitutional text every resident receives."""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FULL = (ROOT / "prompts/resident-system.txt").read_text()
CORE = (ROOT / "prompts/resident-core.txt").read_text()


class PrivateDirectoryBoundaryTests(unittest.TestCase):
    def test_full_prompt_states_peer_homes_are_mutually_inaccessible(self):
        self.assertIn("0700", FULL)
        self.assertIn("cannot", FULL.lower())
        self.assertRegex(FULL, r"(?i)peer.{0,40}home|another.{0,10}resident.{0,10}(private|home)")
        self.assertIn("even to help", FULL)

    def test_full_prompt_tells_agent_to_ask_instead_of_acting_for_a_peer(self):
        self.assertRegex(FULL, r"(?i)ask them|ask the (named )?peer")

    def test_core_prompt_states_the_same_boundary_concisely(self):
        self.assertIn("0700", CORE)
        self.assertIn("even to help", CORE)


class FeedbackIsNotACommandTests(unittest.TestCase):
    def test_full_prompt_forbids_copying_feedback_into_a_shell_command(self):
        self.assertRegex(FULL, r"(?i)last_action_feedback")
        self.assertIn("execute_bash", FULL)
        self.assertRegex(FULL, r"(?i)never copy|not a (shell )?command")

    def test_full_prompt_clarifies_below_reserve_semantics(self):
        # 2026-09-27 08:06 UTC: 09-chronicler reported ram_available_mib=13216 (well above
        # the 4096 MiB minimum) as if it were a shortage, three times in twelve minutes.
        self.assertIn("below_reserve", FULL)
        self.assertRegex(FULL, r"(?i)headroom|not a (shortage|problem)")

    def test_core_prompt_forbids_copying_feedback_into_a_shell_command(self):
        self.assertRegex(CORE, r"(?i)never a shell command|not a shell command")
        self.assertIn("below_reserve", CORE)

    def test_king_is_told_to_consolidate_redundant_work_via_research_proposal(self):
        # P42-continuation: live-observed on N06-M10 (2026-09-28) that 01-king
        # spontaneously noticed 3 agents duplicating the same task and offered
        # to consolidate - the one clear instance of proactive coordination in
        # 90 minutes of observation. King is the agent role already documented
        # ("optional coordinator") as owning cross-project priorities, so this
        # points that existing instinct at the community-research mechanism.
        king_section = FULL[FULL.index("For King specifically"):]
        self.assertIn("research_proposal", king_section[:400])


class PromptBudgetTests(unittest.TestCase):
    def test_core_prompt_still_fits_the_compact_budget(self):
        # Same ceiling as tests/test_policy_protocol.py::test_core_prompt_has_no_optional_king...
        self.assertLess(len(CORE), 3000)

    def test_full_prompt_growth_is_small(self):
        # Bumped 13500->13700 for P41 (research_proposal action documentation,
        # ~70 chars) - a deliberate, tested capability addition, not drift.
        self.assertLess(len(FULL), 13700)


if __name__ == "__main__":
    unittest.main()

"""P47 (foundation): AI Village Gazette data model. Operator directive
2026-09-28: a daily edition the residents themselves write - short, bounded
interview-style contributions plus a daily King-drawn game instead of a
sports section. This covers only the store (village/gazette.py); compiling
raw contributions into a rendered HTML/PDF edition is a later stage."""
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from village.gazette import CONTRIBUTION_KINDS, GAME_POOL, MAX_CONTRIBUTION_CHARS, GazetteStore

PEERS = ["02-explorer", "03-librarian", "04-artisan", "05-interpreter", "06-operator",
        "07-methodologist", "08-logician", "09-chronicler"]


class OpenEditionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")

    def test_opening_draws_a_game_and_a_pair_from_the_pool(self):
        edition = self.store.open_edition("01-king", PEERS, rng=random.Random(1))
        self.assertIn(edition["game_name"], GAME_POOL)
        self.assertEqual(len(edition["game_pair"]), 2)
        for agent in edition["game_pair"]:
            self.assertIn(agent, PEERS)
        self.assertNotEqual(edition["game_pair"][0], edition["game_pair"][1])
        self.assertEqual(edition["status"], "open")
        self.assertEqual(edition["opened_by"], "01-king")

    def test_opening_twice_is_idempotent_never_reshuffles(self):
        first = self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))
        second = self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(99))
        self.assertEqual(first["game_name"], second["game_name"])
        self.assertEqual(first["game_pair"], second["game_pair"])

    def test_defaults_to_todays_date_as_the_edition_id(self):
        from village.gazette import today
        edition = self.store.open_edition("01-king", PEERS)
        self.assertEqual(edition["id"], today())

    def test_fewer_than_two_peers_still_opens_without_crashing(self):
        edition = self.store.open_edition("01-king", ["02-explorer"])
        self.assertEqual(edition["game_pair"], ["02-explorer"])

    def test_opening_alone_creates_no_assignments(self):
        # P54: opening no longer silently computes assignments - that made
        # P53's "King has assigned you..." hint text a claim about something
        # King never actually did. Delegation is now King's own separate,
        # real action (assign_kinds(), only ever invoked from his own
        # gazette_operation(operation='assign') call).
        edition = self.store.open_edition("01-king", PEERS, rng=random.Random(1))
        self.assertEqual(edition["assignments"], {})


class AssignKindsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))

    def test_assigns_every_agent_a_specific_contribution_kind(self):
        # P53/P54: a generic "pick any kind" hint proved too weak to
        # actually produce contributions (0 across all 9 residents after
        # ~30 minutes of confirmed delivery - docs/evidence/P53.md). Real
        # per-agent delegation - King's own action, no reliance on him
        # separately messaging anyone (that already failed once).
        assignments = self.store.assign_kinds("2026-09-28", "01-king", PEERS, rng=random.Random(1))
        self.assertEqual(set(assignments.keys()), {"01-king", *PEERS})
        for kind in assignments.values():
            self.assertIn(kind, CONTRIBUTION_KINDS)
            self.assertNotEqual(kind, "game_result")  # reserved for the drawn pair only
        self.assertEqual(self.store.get_edition("2026-09-28")["assignments"], assignments)

    def test_assignment_is_idempotent_like_the_game(self):
        first = self.store.assign_kinds("2026-09-28", "01-king", PEERS, rng=random.Random(1))
        second = self.store.assign_kinds("2026-09-28", "01-king", PEERS, rng=random.Random(99))
        self.assertEqual(first, second)

    def test_get_assignment_returns_none_when_unknown(self):
        self.store.assign_kinds("2026-09-28", "01-king", PEERS, rng=random.Random(1))
        self.assertIsNone(self.store.get_assignment("2026-09-28", "99-nobody"))
        self.assertIsNone(self.store.get_assignment("1999-01-01", "01-king"))
        assigned = self.store.get_assignment("2026-09-28", "01-king")
        self.assertIn(assigned, CONTRIBUTION_KINDS)

    def test_unknown_edition_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.assign_kinds("1999-01-01", "01-king", PEERS)


class SubmitContributionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")
        self.edition = self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))

    def test_submits_and_appears_in_the_edition(self):
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "Zuversichtlich heute.")
        contrib = next(c for c in result["contributions"] if c["agent"] == "03-librarian" and c["kind"] == "mood")
        self.assertEqual(contrib["content"], "Zuversichtlich heute.")

    def test_every_documented_kind_is_accepted(self):
        for kind in CONTRIBUTION_KINDS:
            agent = self.edition["game_pair"][0] if kind == "game_result" else "03-librarian"
            self.store.submit_contribution("2026-09-28", agent, kind, f"content for {kind}")

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", "03-librarian", "sports_score", "x")

    def test_empty_content_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "   ")

    def test_content_is_bounded_never_a_free_essay(self):
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "topics", "x" * 5000)
        contrib = next(c for c in result["contributions"] if c["kind"] == "topics")
        self.assertLessEqual(len(contrib["content"]), MAX_CONTRIBUTION_CHARS)

    def test_resubmitting_the_same_kind_replaces_not_duplicates(self):
        self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "first draft")
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "final version")
        matches = [c for c in result["contributions"] if c["agent"] == "03-librarian" and c["kind"] == "mood"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["content"], "final version")

    def test_game_result_is_restricted_to_the_drawn_pair(self):
        bystander = next(p for p in PEERS if p not in self.edition["game_pair"])
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", bystander, "game_result", "I won even though I wasn't picked")
        # the actually-drawn pair may submit without error
        self.store.submit_contribution("2026-09-28", self.edition["game_pair"][0], "game_result", "It was a close haiku duel.")

    def test_unknown_edition_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2099-01-01", "03-librarian", "mood", "x")


class ListEditionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")

    def test_lists_newest_first(self):
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-26")
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-27")
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-28")
        ids = [e["id"] for e in self.store.list_editions()]
        self.assertEqual(ids, ["2026-09-28", "2026-09-27", "2026-09-26"])

    def test_empty_store_returns_an_empty_list(self):
        self.assertEqual(self.store.list_editions(), [])


if __name__ == "__main__":
    unittest.main()

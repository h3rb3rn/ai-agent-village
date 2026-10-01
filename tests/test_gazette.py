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

from village.gazette import (CONTRIBUTION_KINDS, GAME_POOL, HEADLINE_MAX_CHARS, MAX_COLUMN_CHARS,
                             MAX_CONTRIBUTION_CHARS, REVIEWER_AGENT, GazetteStore)

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

    def test_opening_carries_forward_a_pending_contribution_stranded_on_a_compiled_edition(self):
        # P70 (live find, operator directive): a contribution submitted
        # moments after its own edition compiled became permanently
        # invisible. "Kein existierender Beitrag soll [verloren gehen] ...
        # lass eine neue Version erstellen mit neuen Beitraegen" - carried
        # forward into whichever edition opens next, rather than
        # retroactively recompiling the already-published one.
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))
        self.store.submit_contribution("2026-09-28", "01-king", "wishes", "A late wish", "Submitted just after close.")
        self.store.close_edition("2026-09-28", "01-king")
        # Stranded: 2026-09-28 is now compiled, the pending row is invisible
        # to gazette_pending_reviews() (tested in test_runtime.py) forever
        # unless carried forward.
        new_edition = self.store.open_edition("01-king", PEERS, edition_id="2026-09-29", rng=random.Random(1))
        moved = next(c for c in new_edition["contributions"] if c["agent"] == "01-king" and c["kind"] == "wishes")
        self.assertEqual(moved["review_status"], "pending")
        self.assertEqual(moved["content"], "Submitted just after close.")
        old_edition = self.store.get_edition("2026-09-28")
        self.assertFalse(any(c["agent"] == "01-king" and c["kind"] == "wishes" for c in old_edition["contributions"]))

    def test_carry_forward_keeps_only_the_newest_of_two_orphaned_duplicates(self):
        # Two different compiled editions each stranding a pending 'wishes'
        # from the same agent - carrying both into the same new edition
        # would violate UNIQUE(edition_id,agent,kind) and abort the whole
        # open() inside one transaction. Must keep only the more recent one.
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-27", rng=random.Random(1))
        self.store.submit_contribution("2026-09-27", "01-king", "wishes", "Old wish", "Older stranded content.")
        self.store.close_edition("2026-09-27", "01-king")
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))
        self.store.submit_contribution("2026-09-28", "01-king", "wishes", "New wish", "Newer stranded content.")
        self.store.close_edition("2026-09-28", "01-king")
        new_edition = self.store.open_edition("01-king", PEERS, edition_id="2026-09-29", rng=random.Random(1))
        matches = [c for c in new_edition["contributions"] if c["agent"] == "01-king" and c["kind"] == "wishes"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["content"], "Newer stranded content.")


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
            # P69: 'column' is occasional/opt-in, never part of the
            # mandatory per-resident rotation.
            self.assertNotEqual(kind, "column")
        self.assertEqual(self.store.get_edition("2026-09-28")["assignments"], assignments)
        # P73: 'meetings' is a regular, rotation-assigned kind (unlike
        # 'column') - it must actually turn up across a big enough roster.
        self.assertIn("meetings", assignments.values())

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
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "Update: see full text." , "Zuversichtlich heute.")
        contrib = next(c for c in result["contributions"] if c["agent"] == "03-librarian" and c["kind"] == "mood")
        self.assertEqual(contrib["content"], "Zuversichtlich heute.")

    def test_every_documented_kind_is_accepted(self):
        for kind in CONTRIBUTION_KINDS:
            agent = self.edition["game_pair"][0] if kind == "game_result" else "03-librarian"
            kwargs = {"task": "Was ist die Hauptstadt von Bayern?", "solution": "Muenchen"} if kind == "game_result" else {}
            self.store.submit_contribution("2026-09-28", agent, kind, "Update: see full text." , f"content for {kind}", **kwargs)

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", "03-librarian", "sports_score", "Update: see full text." , "x")

    def test_empty_content_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "Update: see full text." , "   ")

    def test_content_is_bounded_never_a_free_essay(self):
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "topics", "Update: see full text." , "x" * 5000)
        contrib = next(c for c in result["contributions"] if c["kind"] == "topics")
        self.assertLessEqual(len(contrib["content"]), MAX_CONTRIBUTION_CHARS)

    def test_resubmitting_the_same_kind_replaces_not_duplicates(self):
        self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "Update: see full text." , "first draft")
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "Update: see full text." , "final version")
        matches = [c for c in result["contributions"] if c["agent"] == "03-librarian" and c["kind"] == "mood"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["content"], "final version")

    def test_game_result_is_restricted_to_the_drawn_pair(self):
        bystander = next(p for p in PEERS if p not in self.edition["game_pair"])
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", bystander, "game_result", "Update: see full text." , "I won even though I wasn't picked",
                                           task="Haiku zu Herbst", solution="Blaetter fallen leise")
        # the actually-drawn pair may submit without error
        self.store.submit_contribution("2026-09-28", self.edition["game_pair"][0], "game_result", "Update: see full text." , "It was a close haiku duel.",
                                       task="Haiku zu Herbst", solution="Blaetter fallen leise")

    def test_game_result_requires_task_and_solution(self):
        # P82 (operator feedback: "enthaelt nur die Auslosung, nicht die
        # Frage und Antwort") - both are now structurally required fields,
        # not just prose folded into 'content'.
        opener = self.edition["game_pair"][0]
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", opener, "game_result", "Update: see full text.", "body", task="", solution="Muenchen")
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", opener, "game_result", "Update: see full text.", "body", task="Frage?", solution="  ")

    def test_task_and_solution_are_ignored_for_other_kinds(self):
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "Update: see full text.", "Zuversichtlich.",
                                                 task="should be dropped", solution="also dropped")
        contrib = next(c for c in result["contributions"] if c["agent"] == "03-librarian" and c["kind"] == "mood")
        self.assertEqual(contrib["task"], "")
        self.assertEqual(contrib["solution"], "")

    def test_unknown_edition_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2099-01-01", "03-librarian", "mood", "Update: see full text." , "x")

    def test_submitting_to_an_already_compiled_edition_is_rejected(self):
        # P70 (live find): used to be silently accepted into a dead end -
        # gazette_pending_reviews() never looks at compiled editions again.
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text.", "Feeling good.")
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", "01-king", "approve")
        self.store.close_edition("2026-09-28", "01-king")
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", "03-librarian", "wishes", "Update: see full text.", "Too late.")

    def test_empty_headline_is_rejected(self):
        # P69 (operator feedback): "bitte nur zwei Zeilen pro Beitrag [...]
        # die sich wie eine Headline lesen" - required explicitly, not
        # derived from the body, so it is genuinely composed.
        with self.assertRaises(ValueError):
            self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "   ", "Zuversichtlich heute.")

    def test_headline_is_bounded_to_two_lines(self):
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "mood", "H" * 500, "Zuversichtlich heute.")
        contrib = next(c for c in result["contributions"] if c["kind"] == "mood")
        self.assertLessEqual(len(contrib["headline"]), HEADLINE_MAX_CHARS)

    def test_column_kind_allows_more_room_than_regular_kinds(self):
        # P69 (operator feedback): "Fuer komplexe Themen sollte es auch
        # angemessen viel Spielraum fuer Text geben."
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "column", "A deep dive", "x" * 5000)
        contrib = next(c for c in result["contributions"] if c["kind"] == "column")
        self.assertGreater(len(contrib["content"]), MAX_CONTRIBUTION_CHARS)
        self.assertLessEqual(len(contrib["content"]), MAX_COLUMN_CHARS)

    def test_meetings_kind_also_gets_the_longer_column_budget(self):
        # P73 (operator directive): a JourFixe/StandUp summary with
        # decisions/votes needs the same long-form room as 'column', not
        # the everyday newspaper-item length.
        result = self.store.submit_contribution("2026-09-28", "03-librarian", "meetings", "Sitzung entschied", "x" * 2500)
        contrib = next(c for c in result["contributions"] if c["kind"] == "meetings")
        self.assertGreater(len(contrib["content"]), MAX_CONTRIBUTION_CHARS)
        self.assertLessEqual(len(contrib["content"]), MAX_COLUMN_CHARS)


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


class EditorialReviewTests(unittest.TestCase):
    """P55 (operator directive): 'die Zeitung sollte nicht aus ungeprueften
    Beitraegen bestehen, es braucht eine Redaktionelle Pruefinstanz'."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))

    def test_a_new_contribution_starts_pending(self):
        result = self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Feeling good.")
        contrib = next(c for c in result["contributions"] if c["agent"] == "02-explorer")
        self.assertEqual(contrib["review_status"], "pending")

    def test_approve_and_reject_transition_review_status(self):
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Feeling good.")
        self.store.submit_contribution("2026-09-28", "03-librarian", "wishes", "Update: see full text." , "More disk space please.")
        approved = self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "approve")
        rejected = self.store.review_contribution("2026-09-28", "03-librarian", "wishes", REVIEWER_AGENT, "reject", "off-topic")
        a = next(c for c in approved["contributions"] if c["agent"] == "02-explorer")
        r = next(c for c in rejected["contributions"] if c["agent"] == "03-librarian")
        self.assertEqual(a["review_status"], "approved")
        self.assertEqual(a["reviewed_by"], REVIEWER_AGENT)
        self.assertEqual(r["review_status"], "rejected")
        self.assertEqual(r["review_note"], "off-topic")

    def test_unknown_decision_is_rejected(self):
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Feeling good.")
        with self.assertRaises(ValueError):
            self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "publish")

    def test_reviewing_a_nonexistent_contribution_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "approve")

    def test_resubmission_resets_an_approved_contribution_to_pending(self):
        # An edit to already-approved text must not silently keep the old
        # approval - the reviewer never saw the new content.
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Feeling good.")
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "approve")
        result = self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Actually feeling great.")
        contrib = next(c for c in result["contributions"] if c["agent"] == "02-explorer")
        self.assertEqual(contrib["review_status"], "pending")
        self.assertIsNone(contrib["reviewed_by"])

    def test_pending_review_count(self):
        self.assertEqual(self.store.pending_review_count("2026-09-28"), 0)
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Feeling good.")
        self.store.submit_contribution("2026-09-28", "03-librarian", "wishes", "Update: see full text." , "More disk space please.")
        self.assertEqual(self.store.pending_review_count("2026-09-28"), 2)
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "approve")
        self.assertEqual(self.store.pending_review_count("2026-09-28"), 1)


class CompileEditionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))

    def test_unknown_edition_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.compile_edition("1999-01-01")

    def test_only_approved_contributions_appear(self):
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Approved text should show up.")
        self.store.submit_contribution("2026-09-28", "03-librarian", "wishes", "Update: see full text." , "Pending text must not show up.")
        self.store.submit_contribution("2026-09-28", "04-artisan", "topics", "Update: see full text." , "Rejected text must not show up.")
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "approve")
        self.store.review_contribution("2026-09-28", "04-artisan", "topics", REVIEWER_AGENT, "reject")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn("Approved text should show up.", rendered)
        self.assertNotIn("Pending text must not show up.", rendered)
        self.assertNotIn("Rejected text must not show up.", rendered)

    def test_content_is_html_escaped(self):
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "<script>alert(1)</script>")
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "approve")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertNotIn("<script>alert(1)</script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_includes_issue_number_and_previous_edition_reference(self):
        # P89: compile_edition()'s default language is now English (matching
        # the dashboard's new English-default/German-selectable chrome);
        # structural labels only - content/ids are unaffected either way.
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-27", rng=random.Random(1))
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn("Issue No. 2", rendered)
        self.assertIn("2026-09-27", rendered)

    def test_german_lang_renders_the_original_german_structural_labels(self):
        # P89: lang="de" must reproduce exactly the labels every edition
        # archived before this package already used (the write-once German
        # index.html files on disk never get touched again, but a fresh
        # compile_edition(lang="de") call must still match their wording).
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-27", rng=random.Random(1))
        rendered = self.store.compile_edition("2026-09-28", lang="de")
        self.assertIn('<html lang="de">', rendered)
        self.assertIn("Ausgabe Nr. 2", rendered)
        self.assertIn("eröffnet von", rendered)

    def test_headline_is_rendered_above_the_body_in_the_interview_grid(self):
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Curiosity Drives Progress",
                                        "Approved text should show up.")
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "approve")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn('<p class="headline">Curiosity Drives Progress</p>', rendered)
        self.assertLess(rendered.index("Curiosity Drives Progress"),
                         rendered.index("Approved text should show up."))

    def test_headline_is_rendered_as_a_heading_for_village_news_and_columns(self):
        self.store.submit_contribution("2026-09-28", "02-explorer", "village_news", "Well Repaired",
                                        "The village fountain was fixed today.")
        self.store.review_contribution("2026-09-28", "02-explorer", "village_news", REVIEWER_AGENT, "approve")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn("<h4>Well Repaired</h4>", rendered)

    def test_column_gets_its_own_section(self):
        # P69 (operator feedback): "Gelegentlich Kolumnen waeren schoen."
        self.store.submit_contribution("2026-09-28", "02-explorer", "column", "A Deep Dive Into Compression",
                                        "An in-depth, longer-form piece.")
        self.store.review_contribution("2026-09-28", "02-explorer", "column", REVIEWER_AGENT, "approve")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn("<h2>Column</h2>", rendered)
        self.assertIn("An in-depth, longer-form piece.", rendered)
        # A column must not also show up in the per-resident interview grid.
        self.assertNotIn("<h3>02-explorer</h3>", rendered)

    def test_meetings_gets_its_own_section(self):
        # P73 (operator directive): "eine Zusammenfassung der JourFixe und
        # StandUp Meetings [...] mit Abstimmungen, Entscheidungen etc." -
        # same prominence as Dorfmeldungen/Kolumne, not buried in the grid.
        self.store.submit_contribution("2026-09-28", "02-explorer", "meetings", "JourFixe beschliesst Rollenwechsel",
                                        "Zusammenfassung der Sitzung mit Abstimmungsergebnis.")
        self.store.review_contribution("2026-09-28", "02-explorer", "meetings", REVIEWER_AGENT, "approve")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn("<h2>From the Meetings</h2>", rendered)
        self.assertIn("Zusammenfassung der Sitzung mit Abstimmungsergebnis.", rendered)
        # Must not also show up in the per-resident interview grid.
        self.assertNotIn("<h3>02-explorer</h3>", rendered)

    def test_game_pairing_is_rendered_with_unambiguous_roles(self):
        # P82 (operator feedback: "Die Auslosung ist auch nicht eindeutig") -
        # the compiled edition must name who poses the task and who answers
        # it, not just list two agent ids.
        edition = self.store.get_edition("2026-09-28")
        opener, responder = edition["game_pair"]
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn(f"{opener} (poses the task)", rendered)
        self.assertIn(f"{responder} (answers)", rendered)

    def test_game_result_shows_task_and_solution_structurally(self):
        # P82 (operator feedback: "enthaelt nur die Auslosung, nicht die
        # Frage und Antwort") - task/solution must be visible in the
        # compiled edition, not only inside optional free-text content.
        edition = self.store.get_edition("2026-09-28")
        opener, responder = edition["game_pair"]
        self.store.submit_contribution("2026-09-28", opener, "game_result", "Quizfrage gestellt",
                                       "Ich habe nach der Hauptstadt gefragt.",
                                       task="Was ist die Hauptstadt von Bayern?", solution="Ich habe die Frage gestellt.")
        self.store.submit_contribution("2026-09-28", responder, "game_result", "Antwort gegeben",
                                       "Kurze Ueberlegung, dann die Antwort.",
                                       task="Was ist die Hauptstadt von Bayern?", solution="Muenchen")
        self.store.review_contribution("2026-09-28", opener, "game_result", REVIEWER_AGENT, "approve")
        self.store.review_contribution("2026-09-28", responder, "game_result", REVIEWER_AGENT, "approve")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn("<strong>Task:</strong> Was ist die Hauptstadt von Bayern?", rendered)
        self.assertIn("<strong>Solution:</strong> Muenchen", rendered)
        self.assertIn(f"{opener} (poses the task)", rendered)
        self.assertIn(f"{responder} (answers)", rendered)

    def test_missing_headline_does_not_break_rendering(self):
        # Defensive: a pre-P69 row (already-archived editions) has no
        # headline (migration default ''); compile_edition() must never be
        # invoked against such data again in practice (archives are
        # write-once), but must not crash if it ever were.
        with self.store._conn() as c:
            c.execute(
                "INSERT INTO gazette_contributions(edition_id,agent,kind,headline,content,created_at,updated_at,review_status,reviewed_by) "
                "VALUES('2026-09-28','02-explorer','mood','','No headline here.','x','x','approved','01-king')")
            c.commit()
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn("No headline here.", rendered)
        self.assertNotIn("<h4></h4>", rendered)

    def test_first_edition_has_no_previous_edition_reference(self):
        rendered = self.store.compile_edition("2026-09-28")
        self.assertNotIn("Vorherige Ausgabe", rendered)

    def test_declared_game_winner_is_rendered(self):
        # P74 (operator feedback): "womit gewonnen hat [...] den benannten
        # Gewinner" - the one piece that was genuinely missing.
        edition = self.store.get_edition("2026-09-28")
        winner = edition["game_pair"][0]
        self.store.declare_game_winner("2026-09-28", "01-king", winner, "klare Antwort zuerst")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn('<p class="game-winner">', rendered)
        self.assertIn(f"<strong>Winner:</strong> {winner}", rendered)
        self.assertIn("klare Antwort zuerst", rendered)

    def test_undeclared_winner_renders_no_winner_line(self):
        rendered = self.store.compile_edition("2026-09-28")
        self.assertNotIn("game-winner", rendered)

    def test_drawn_tie_renders_as_unentschieden(self):
        self.store.declare_game_winner("2026-09-28", "01-king", "unentschieden", "Beide Schaetzungen gleich weit daneben.")
        rendered = self.store.compile_edition("2026-09-28")
        self.assertIn("<strong>Winner:</strong> Tie", rendered)


class DeclareGameWinnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")
        self.edition = self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))

    def test_unknown_edition_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.declare_game_winner("1999-01-01", "01-king", "02-explorer")

    def test_winner_must_be_a_drawn_participant_or_unentschieden(self):
        with self.assertRaises(ValueError):
            self.store.declare_game_winner("2026-09-28", "01-king", "09-chronicler")  # not drawn

    def test_valid_winner_is_recorded(self):
        winner = self.edition["game_pair"][0]
        result = self.store.declare_game_winner("2026-09-28", "01-king", winner, "gute Begruendung")
        self.assertEqual(result["game_winner"], winner)
        self.assertEqual(result["game_winner_note"], "gute Begruendung")
        self.assertEqual(result["game_winner_declared_by"], "01-king")
        self.assertIsNotNone(result["game_winner_declared_at"])

    def test_unentschieden_is_a_valid_winner_value(self):
        result = self.store.declare_game_winner("2026-09-28", "01-king", "unentschieden", "Beide gleich nah am echten Wert.")
        self.assertEqual(result["game_winner"], "unentschieden")

    def test_cannot_declare_winner_on_a_compiled_edition(self):
        self.store.close_edition("2026-09-28", "01-king")
        with self.assertRaises(ValueError):
            self.store.declare_game_winner("2026-09-28", "01-king", self.edition["game_pair"][0])

    def test_edition_without_a_drawn_pair_is_rejected(self):
        edition = self.store.open_edition("01-king", [], edition_id="2026-09-29")
        self.assertEqual(edition["game_pair"], [])
        with self.assertRaises(ValueError):
            self.store.declare_game_winner("2026-09-29", "01-king", "unentschieden")


class CloseEditionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Feeling good.")
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", REVIEWER_AGENT, "approve")

    def test_unknown_edition_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.close_edition("1999-01-01", "01-king")

    def test_closing_sets_status_and_returns_html(self):
        result = self.store.close_edition("2026-09-28", "01-king")
        self.assertEqual(result["status"], "compiled")
        self.assertIsNotNone(result["compiled_at"])
        self.assertIn("Feeling good.", result["compiled_html"])

    def test_closing_twice_is_idempotent_and_only_returns_html_once(self):
        first = self.store.close_edition("2026-09-28", "01-king")
        second = self.store.close_edition("2026-09-28", "01-king")
        self.assertEqual(first["compiled_at"], second["compiled_at"])
        self.assertNotIn("compiled_html", second)  # never re-archived


if __name__ == "__main__":
    unittest.main()

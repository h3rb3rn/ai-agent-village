"""P66: pure-Python Gazette PDF writer (Stufe 4, Teil 2 of
docs/analysis/GAZETTE-PLAN-2026-09-28.md). No external PDF library - these
tests check the hand-written object/xref structure directly. The generator
was also independently verified against real tooling during development
(ghostscript accepted it with zero errors; poppler's pdftotext/pdfinfo
extracted the exact expected text, umlauts and em dash included, across the
correct page count) - not repeated here since that tooling is not a
dependency of the shipped code or CI."""
import random
import shutil
import tempfile
import unittest
from pathlib import Path

from village.gazette import GazetteStore
from village.gazette_pdf import build_pdf, edition_sections, render_edition_pdf

PEERS = ["02-explorer", "03-librarian", "04-artisan"]


class BuildPdfStructureTests(unittest.TestCase):
    def test_header_and_trailer_are_present(self):
        pdf = build_pdf([("title", "AI Village Gazette"), ("body", "hello")])
        self.assertTrue(pdf.startswith(b"%PDF-1.4\n"))
        self.assertTrue(pdf.rstrip(b"\n").endswith(b"%%EOF"))
        self.assertIn(b"trailer", pdf)
        self.assertIn(b"xref", pdf)

    def test_text_appears_literally_in_the_content_stream(self):
        pdf = build_pdf([("body", "A short village update.")])
        self.assertIn(b"(A short village update.) Tj", pdf)

    def test_parentheses_and_backslashes_are_escaped(self):
        pdf = build_pdf([("body", "A (parenthetical) remark, and a \\backslash\\.")])
        self.assertIn(rb"\(parenthetical\)", pdf)
        self.assertIn(rb"\\backslash\\", pdf)
        # The raw, unescaped form must never appear as a bare PDF string.
        self.assertNotIn(b"(A (parenthetical)", pdf)

    def test_umlauts_and_em_dash_round_trip_via_win_ansi_encoding(self):
        text = "Wünsche mir mehr Bücher über Äpfel — grüße, ß"
        pdf = build_pdf([("body", text)])
        self.assertIn("Wünsche".encode("cp1252"), pdf)
        self.assertIn("Äpfel".encode("cp1252"), pdf)
        self.assertIn("—".encode("cp1252"), pdf)  # em dash
        self.assertIn("ß".encode("cp1252"), pdf)
        self.assertIn(b"/Encoding /WinAnsiEncoding", pdf)

    def test_unencodable_characters_are_replaced_not_raised(self):
        pdf = build_pdf([("body", "An emoji shows up here: \U0001F600")])
        self.assertIn(b"%PDF-1.4", pdf)  # did not raise; still a valid document

    def test_long_content_produces_more_than_one_page(self):
        long_line = "Ein sehr langer Testtext " * 40
        sections = [("body", long_line)] * 12
        pdf = build_pdf(sections)
        # Page objects (not the single /Type /Pages tree object) carry
        # "/Type /Page /Parent" - counting that distinguishes them.
        self.assertGreaterEqual(pdf.count(b"/Type /Page /Parent"), 2)

    def test_short_content_produces_exactly_one_page(self):
        pdf = build_pdf([("title", "AI Village Gazette"), ("body", "Short.")])
        self.assertEqual(pdf.count(b"/Type /Page /Parent"), 1)

    def test_empty_sections_still_produce_a_valid_single_page_document(self):
        pdf = build_pdf([])
        self.assertTrue(pdf.startswith(b"%PDF-1.4\n"))
        self.assertEqual(pdf.count(b"/Type /Page /Parent"), 1)


class EditionSectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-gazette-pdf-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = GazetteStore(self.tmp / "coordination.sqlite3")
        self.store.open_edition("01-king", PEERS, edition_id="2026-09-28", rng=random.Random(1))
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Update: see full text." , "Approved mood text.")
        self.store.submit_contribution("2026-09-28", "03-librarian", "wishes", "Update: see full text." , "Rejected wish text.")
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", "01-king", "approve")
        self.store.review_contribution("2026-09-28", "03-librarian", "wishes", "01-king", "reject", "off-topic")

    def test_only_approved_content_reaches_the_pdf(self):
        result = self.store.close_edition("2026-09-28", "01-king")
        pdf = render_edition_pdf(result, result["issue_number"], result["previous_id"])
        self.assertIn(b"Approved mood text.", pdf)
        self.assertNotIn(b"Rejected wish text.", pdf)

    def test_issue_number_and_previous_edition_appear_in_the_header(self):
        yesterday = self.store.open_edition("01-king", PEERS, edition_id="2026-09-27")
        self.store.submit_contribution(yesterday["id"], "02-explorer", "mood", "Update: see full text." , "Yesterday.")
        self.store.review_contribution(yesterday["id"], "02-explorer", "mood", "01-king", "approve")
        self.store.close_edition("2026-09-27", "01-king")
        result = self.store.close_edition("2026-09-28", "01-king")
        sections = edition_sections(result, result["issue_number"], result["previous_id"])
        # P89: edition_sections()'s default language is now English, same
        # as village/gazette.py's compile_edition() default.
        joined = " ".join(text for _, text in sections)
        self.assertIn(f"Issue No. {result['issue_number']}", joined)
        self.assertIn("Previous edition: 2026-09-27", joined)

    def test_headline_becomes_a_bold_subhead_line(self):
        self.store.submit_contribution("2026-09-28", "04-artisan", "village_news", "Well Repaired",
                                        "The fountain was fixed.")
        self.store.review_contribution("2026-09-28", "04-artisan", "village_news", "01-king", "approve")
        result = self.store.close_edition("2026-09-28", "01-king")
        sections = edition_sections(result, result["issue_number"], result["previous_id"])
        subheads = [text for level, text in sections if level == "subhead"]
        self.assertIn("Well Repaired", subheads)

    def test_column_gets_its_own_heading_section(self):
        self.store.submit_contribution("2026-09-28", "04-artisan", "column", "A Deep Dive",
                                        "An in-depth, longer-form piece.")
        self.store.review_contribution("2026-09-28", "04-artisan", "column", "01-king", "approve")
        result = self.store.close_edition("2026-09-28", "01-king")
        sections = edition_sections(result, result["issue_number"], result["previous_id"])
        headings = [text for level, text in sections if level == "heading"]
        self.assertIn("Column", headings)
        joined = " ".join(text for _, text in sections)
        self.assertIn("An in-depth, longer-form piece.", joined)

    def test_game_section_shows_task_and_solutions_from_voluntary_participants(self):
        # P90 (operator feedback: "es kann immer nur der eine Teilnehmer
        # gewinnen der schaetzt [...] es muessen [...] mehr Agents
        # Teilnehmen") - mirrors compile_edition()'s own P90 fix: no more
        # drawn pair/roles, King poses the task once, any number of
        # residents may voluntarily submit a guess.
        self.store.set_game_task("2026-09-28", "01-king", "Hauptstadt von Bayern?")
        self.store.submit_contribution("2026-09-28", "02-explorer", "game_result", "Antwort gegeben",
                                       "Kurz ueberlegt.", solution="Muenchen")
        self.store.submit_contribution("2026-09-28", "03-librarian", "game_result", "Andere Antwort",
                                       "Auch ueberlegt.", solution="Nuernberg")
        self.store.review_contribution("2026-09-28", "02-explorer", "game_result", "01-king", "approve")
        self.store.review_contribution("2026-09-28", "03-librarian", "game_result", "01-king", "approve")
        self.store.declare_game_winner("2026-09-28", "01-king", "02-explorer", "closest")
        result = self.store.close_edition("2026-09-28", "01-king")
        sections = edition_sections(result, result["issue_number"], result["previous_id"])
        joined = " ".join(text for _, text in sections)
        self.assertIn("Task: Hauptstadt von Bayern?", joined)
        self.assertIn("Solution: Muenchen", joined)
        self.assertIn("Solution: Nuernberg", joined)
        self.assertIn("Winner: 02-explorer", joined)

    def test_german_lang_reproduces_the_original_german_structural_labels(self):
        # P89: lang="de" must match the wording every PDF archived before
        # this package already used.
        result = self.store.close_edition("2026-09-28", "01-king")
        sections = edition_sections(result, result["issue_number"], result["previous_id"], lang="de")
        headings = [text for level, text in sections if level == "heading"]
        self.assertIn("Spiel des Tages", headings)
        pdf = render_edition_pdf(result, result["issue_number"], result["previous_id"], lang="de")
        self.assertIn(b"(Spiel des Tages) Tj", pdf)

    def test_pdf_renders_the_headline_in_bold_courier(self):
        self.store.submit_contribution("2026-09-28", "04-artisan", "village_news", "Well Repaired",
                                        "The fountain was fixed.")
        self.store.review_contribution("2026-09-28", "04-artisan", "village_news", "01-king", "approve")
        result = self.store.close_edition("2026-09-28", "01-king")
        pdf = render_edition_pdf(result, result["issue_number"], result["previous_id"])
        self.assertIn(b"(Well Repaired) Tj", pdf)
        self.assertIn(b"/F2 10 Tf", pdf)  # bold Courier at body size for the subhead


if __name__ == "__main__":
    unittest.main()

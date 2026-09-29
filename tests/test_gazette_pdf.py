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
        self.store.submit_contribution("2026-09-28", "02-explorer", "mood", "Approved mood text.")
        self.store.submit_contribution("2026-09-28", "03-librarian", "wishes", "Rejected wish text.")
        self.store.review_contribution("2026-09-28", "02-explorer", "mood", "01-king", "approve")
        self.store.review_contribution("2026-09-28", "03-librarian", "wishes", "01-king", "reject", "off-topic")

    def test_only_approved_content_reaches_the_pdf(self):
        result = self.store.close_edition("2026-09-28", "01-king")
        pdf = render_edition_pdf(result, result["issue_number"], result["previous_id"])
        self.assertIn(b"Approved mood text.", pdf)
        self.assertNotIn(b"Rejected wish text.", pdf)

    def test_issue_number_and_previous_edition_appear_in_the_header(self):
        yesterday = self.store.open_edition("01-king", PEERS, edition_id="2026-09-27")
        self.store.submit_contribution(yesterday["id"], "02-explorer", "mood", "Yesterday.")
        self.store.review_contribution(yesterday["id"], "02-explorer", "mood", "01-king", "approve")
        self.store.close_edition("2026-09-27", "01-king")
        result = self.store.close_edition("2026-09-28", "01-king")
        sections = edition_sections(result, result["issue_number"], result["previous_id"])
        joined = " ".join(text for _, text in sections)
        self.assertIn(f"Ausgabe Nr. {result['issue_number']}", joined)
        self.assertIn("Vorherige Ausgabe: 2026-09-27", joined)


if __name__ == "__main__":
    unittest.main()

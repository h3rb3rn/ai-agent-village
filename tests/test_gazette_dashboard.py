"""P64: Gazette dashboard page (Stufe 4 of docs/analysis/GAZETTE-PLAN-2026-09-28.md)
- lists and serves only compiled editions, never a still-open, pending, or
rejected contribution. That guarantee is the entire point of the editorial
review gate (P55); a public-facing page that bypassed it by reading live
DB rows instead of the archived, already-filtered HTML would undo it."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("VILLAGE_ROOT", "/tmp/ai-village-gazette-dashboard")
sys.path.insert(0, os.path.abspath("web"))

from http.server import ThreadingHTTPServer
from web import webui
from village.gazette import GazetteStore
from village.gazette_pdf import render_edition_pdf


class GazetteDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "board").mkdir()
        (root / "telemetry").mkdir()
        (root / "gazette" / "archive").mkdir(parents=True)
        cls.saved = (webui.ROOT, webui.GAZETTE_ARCHIVE, webui.EVENTS, webui.INBOX, webui.AGENT_TELEMETRY, webui.ASSETS)
        webui.ROOT = root
        webui.GAZETTE_ARCHIVE = root / "gazette" / "archive"
        webui.EVENTS = root / "board/events.jsonl"
        webui.INBOX = root / "board/organic-inbox.jsonl"
        webui.AGENT_TELEMETRY = root / "telemetry/agent-events.jsonl"
        webui.ASSETS = Path(__file__).resolve().parents[1] / "web"  # the real observatory.html, checked into the repo
        webui.EVENTS.write_text("")

        store = GazetteStore(root / "board" / "coordination.sqlite3")
        # A compiled edition with one approved and one rejected contribution -
        # only the approved one may ever surface, on the list or in the archive.
        edition = store.open_edition("01-king", ["02-explorer", "03-librarian"], edition_id="2026-09-20")
        store.submit_contribution(edition["id"], "02-explorer", "mood", "Update: see full text." , "Feeling good.")
        store.submit_contribution(edition["id"], "03-librarian", "wishes", "Update: see full text." , "More books please.")
        store.review_contribution(edition["id"], "02-explorer", "mood", "01-king", "approve")
        store.review_contribution(edition["id"], "03-librarian", "wishes", "01-king", "reject", "off-topic")
        compiled = store.close_edition(edition["id"], "01-king")
        archive_dir = webui.GAZETTE_ARCHIVE / edition["id"]
        archive_dir.mkdir(parents=True, exist_ok=True)
        (archive_dir / "index.html").write_text(compiled["compiled_html"], encoding="utf-8")
        (archive_dir / "gazette.pdf").write_bytes(
            render_edition_pdf(compiled, compiled["issue_number"], compiled["previous_id"]))
        # P89: the German archive sibling web/runtime.py's close dispatch
        # now also writes for every edition going forward.
        compiled_de = store.compile_edition(edition["id"], lang="de")
        (archive_dir / "index.de.html").write_text(compiled_de, encoding="utf-8")
        (archive_dir / "gazette.de.pdf").write_bytes(
            render_edition_pdf(compiled, compiled["issue_number"], compiled["previous_id"], lang="de"))

        # A second edition that is still open with an unreviewed contribution -
        # must never appear on the list, regardless of how it is filtered.
        store.open_edition("01-king", ["02-explorer", "03-librarian"], edition_id="2026-09-21")
        store.submit_contribution("2026-09-21", "02-explorer", "mood", "Update: see full text." , "Still working on it.")

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        webui.ROOT, webui.GAZETTE_ARCHIVE, webui.EVENTS, webui.INBOX, webui.AGENT_TELEMETRY, webui.ASSETS = cls.saved
        cls.tmp.cleanup()

    def request(self, path, raw=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        raw_body = response.read()
        data = raw_body if raw else raw_body.decode("utf-8", "replace")
        result = (response.status, data, response) if raw else (response.status, data)
        conn.close()
        return result

    def test_gazette_route_serves_the_spa_shell(self):
        status, body = self.request("/gazette")
        self.assertEqual(status, 200)
        self.assertIn("AI VILLAGE", body)

    def test_api_gazette_lists_only_the_compiled_edition(self):
        status, body = self.request("/api/gazette")
        self.assertEqual(status, 200)
        ids = [r["id"] for r in json.loads(body)]
        self.assertIn("2026-09-20", ids)
        self.assertNotIn("2026-09-21", ids)  # still open, unreviewed

    def test_api_gazette_contributor_count_excludes_rejected(self):
        _, body = self.request("/api/gazette")
        rows = {r["id"]: r for r in json.loads(body)}
        self.assertEqual(rows["2026-09-20"]["contributor_count"], 1)  # only the approved one

    def test_archive_html_is_served_for_a_compiled_edition(self):
        status, body = self.request("/gazette/2026-09-20.html")
        self.assertEqual(status, 200)
        self.assertIn("Feeling good.", body)
        self.assertNotIn("More books please.", body)  # rejected content never appears

    def test_archive_pdf_is_served_for_a_compiled_edition(self):
        status, body, response = self.request("/gazette/2026-09-20.pdf", raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"%PDF-1.4"))
        self.assertIn(b"Feeling good.", body)
        self.assertNotIn(b"More books please.", body)  # rejected content never appears
        self.assertEqual(response.getheader("Content-Type"), "application/pdf")
        self.assertIn("attachment", response.getheader("Content-Disposition", ""))

    def test_german_archive_html_is_served_for_a_compiled_edition(self):
        status, body = self.request("/gazette/2026-09-20.de.html")
        self.assertEqual(status, 200)
        self.assertIn('<html lang="de">', body)
        self.assertIn("Feeling good.", body)  # content identical, labels only translated
        self.assertNotIn("More books please.", body)

    def test_german_archive_pdf_is_served_for_a_compiled_edition(self):
        status, body, response = self.request("/gazette/2026-09-20.de.pdf", raw=True)
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith(b"%PDF-1.4"))
        self.assertIn(b"Feeling good.", body)
        self.assertEqual(response.getheader("Content-Type"), "application/pdf")

    def test_german_archive_404s_when_only_the_english_sibling_exists(self):
        # An edition whose close happened before this package shipped (or
        # whose German render failed) never gets a retroactive ".de." file -
        # the write-once guarantee is per file, not "per edition".
        status, _ = self.request("/gazette/2026-09-21.de.html")  # open, never compiled
        self.assertEqual(status, 404)

    def test_archive_pdf_404s_when_no_archive_file_exists(self):
        status, _ = self.request("/gazette/2026-09-21.pdf")  # open, never compiled/archived
        self.assertEqual(status, 404)

    def test_archive_html_404s_when_no_archive_file_exists(self):
        status, _ = self.request("/gazette/2026-09-21.html")  # open, never compiled/archived
        self.assertEqual(status, 404)
        status, _ = self.request("/gazette/2099-01-01.html")  # unknown edition
        self.assertEqual(status, 404)

    def test_archive_route_rejects_non_date_and_traversal_ids(self):
        for bad in ("/gazette/../../../etc/passwd.html", "/gazette/not-a-date.html", "/gazette/2026-09-20/../secret.html",
                    "/gazette/../../../etc/passwd.pdf", "/gazette/not-a-date.pdf",
                    "/gazette/../../../etc/passwd.de.html", "/gazette/not-a-date.de.pdf"):
            status, _ = self.request(bad)
            self.assertEqual(status, 404, bad)


if __name__ == "__main__":
    unittest.main()

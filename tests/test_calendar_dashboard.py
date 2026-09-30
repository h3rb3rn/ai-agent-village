"""P77: Dashboard calendar overlay under /agents - read-only cross-agent
day view. Same real-HTTP-server test harness as test_gazette_dashboard.py."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("VILLAGE_ROOT", "/tmp/ai-village-calendar-dashboard")
# web.webui reads these once at import time into module-level constants
# (see SIGNAL_USER/SIGNAL_PASSWORD); whichever test file imports it first
# in the whole discovery run fixes them for every other file sharing the
# process. test_contact_security.py/test_webui_hardening.py already
# defend against this same way - matched here so alphabetical discovery
# order ("calendar" < "contact") cannot leave those other files starved.
os.environ.setdefault("VILLAGE_SIGNAL_AUTH_USER", "synthetic-user")
os.environ.setdefault("VILLAGE_SIGNAL_AUTH_PASSWORD", "synthetic-password")
sys.path.insert(0, os.path.abspath("web"))

from http.server import ThreadingHTTPServer
from web import webui
from village.calendar import CalendarStore


class CalendarDashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        (root / "board").mkdir()
        (root / "telemetry").mkdir()
        cls.saved = (webui.ROOT, webui.EVENTS, webui.INBOX, webui.AGENT_TELEMETRY, webui.ASSETS)
        webui.ROOT = root
        webui.EVENTS = root / "board/events.jsonl"
        webui.INBOX = root / "board/organic-inbox.jsonl"
        webui.AGENT_TELEMETRY = root / "telemetry/agent-events.jsonl"
        webui.ASSETS = Path(__file__).resolve().parents[1] / "web"  # the real observatory.html, checked into the repo
        webui.EVENTS.write_text("")

        cls.today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        store = CalendarStore(root / "board" / "coordination.sqlite3")
        cls.shared = store.create_event("01-king", "Standup", "standup", cls.today, "09:00", 15,
                                        attendees=["02-explorer"])
        cls.personal = store.create_event("03-librarian", "Fokusarbeit", "focus", cls.today, "10:00", 60)
        # A different day - must never appear in a query for cls.today.
        store.create_event("01-king", "Nächste Woche", "meeting", "2099-01-01", "09:00", 30)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        webui.ROOT, webui.EVENTS, webui.INBOX, webui.AGENT_TELEMETRY, webui.ASSETS = cls.saved
        cls.tmp.cleanup()

    def request(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        response = conn.getresponse()
        body = response.read().decode("utf-8", "replace")
        conn.close()
        return response.status, body

    def test_agents_route_serves_the_spa_shell(self):
        status, body = self.request("/agents")
        self.assertEqual(status, 200)
        self.assertIn("AI VILLAGE", body)

    def test_api_calendar_defaults_to_today_when_no_date_given(self):
        status, body = self.request("/api/calendar")
        self.assertEqual(status, 200)
        ids = [e["id"] for e in json.loads(body)]
        self.assertIn(self.shared["id"], ids)
        self.assertIn(self.personal["id"], ids)

    def test_api_calendar_scopes_to_the_requested_day(self):
        _, body = self.request(f"/api/calendar?date={self.today}")
        ids = [e["id"] for e in json.loads(body)]
        self.assertIn(self.shared["id"], ids)
        self.assertEqual(len(ids), 2)  # not the 2099-01-01 event

    def test_api_calendar_includes_attendees_for_shared_events(self):
        _, body = self.request(f"/api/calendar?date={self.today}")
        rows = {e["id"]: e for e in json.loads(body)}
        attendees = {a["agent_id"] for a in rows[self.shared["id"]]["attendees"]}
        self.assertEqual(attendees, {"01-king", "02-explorer"})

    def test_api_calendar_rejects_a_malformed_date_and_falls_back_to_today(self):
        status, body = self.request("/api/calendar?date=not-a-date")
        self.assertEqual(status, 200)
        ids = [e["id"] for e in json.loads(body)]
        self.assertIn(self.shared["id"], ids)  # today's events, not an error

    def test_api_calendar_a_day_with_no_events_returns_an_empty_list(self):
        status, body = self.request("/api/calendar?date=2020-01-01")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])


if __name__ == "__main__":
    unittest.main()

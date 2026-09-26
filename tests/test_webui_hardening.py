"""Web UI: /activity must survive sparse events; login is throttled; cookie flags; Content-Length safety."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("VILLAGE_ROOT", "/tmp/ai-village-webui-hardening")
os.environ["VILLAGE_SIGNAL_AUTH_USER"] = "synthetic-user"
os.environ["VILLAGE_SIGNAL_AUTH_PASSWORD"] = "synthetic-password"
sys.path.insert(0, os.path.abspath("web"))

from http.server import ThreadingHTTPServer
from web import webui


class WebUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(); root = Path(cls.tmp.name)
        (root / "board").mkdir(); (root / "telemetry").mkdir()
        cls.saved = (webui.EVENTS, webui.INBOX, webui.AGENT_TELEMETRY, webui.SIGNAL_USER, webui.SIGNAL_PASSWORD)
        webui.EVENTS = root / "board/events.jsonl"; webui.INBOX = root / "board/organic-inbox.jsonl"
        webui.AGENT_TELEMETRY = root / "telemetry/agent-events.jsonl"
        webui.SIGNAL_USER, webui.SIGNAL_PASSWORD = "synthetic-user", "synthetic-password"
        rows = [
            {"timestamp": "2026-01-01T00:00:00+00:00", "agent": "02-b", "event": "direct_message", "detail": "to=03-c; chars=3"},
            {"timestamp": "2026-01-01T00:00:01+00:00", "event": "board_message", "detail": "<script>x</script>"},
            {"timestamp": "2026-01-01T00:00:02+00:00", "agent": "01-a", "name": "a", "role": "king", "event": "agent_start", "detail": "ok"},
        ]
        webui.EVENTS.write_text("".join(json.dumps(r) + "\n" for r in rows))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close()
        webui.EVENTS, webui.INBOX, webui.AGENT_TELEMETRY, webui.SIGNAL_USER, webui.SIGNAL_PASSWORD = cls.saved
        cls.tmp.cleanup()

    def setUp(self):
        webui.LOGIN_FAILURES.clear(); webui.SESSIONS.clear()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse(); data = response.read().decode("utf-8", "replace")
        result = (response.status, data, response); conn.close(); return result

    def test_activity_renders_events_without_name_or_role(self):
        status, body, _ = self.request("GET", "/activity")
        self.assertEqual(status, 200)
        self.assertIn("direct_message", body)
        self.assertNotIn("<script>x</script>", body)  # escaped
        self.assertIn("&lt;script&gt;", body)

    def test_api_routes_ignore_query_string(self):
        status, body, _ = self.request("GET", "/api/activity?limit=5")
        self.assertEqual(status, 200); self.assertIsInstance(json.loads(body), list)

    def test_login_is_throttled_after_repeated_failures(self):
        form = "username=synthetic-user&password=wrong"
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(form))}
        codes = [self.request("POST", "/contact/login", form, headers)[0] for _ in range(webui.LOGIN_MAX_FAILURES + 1)]
        self.assertEqual(codes[:webui.LOGIN_MAX_FAILURES], [401] * webui.LOGIN_MAX_FAILURES)
        self.assertEqual(codes[-1], 429)
        good = "username=synthetic-user&password=synthetic-password"
        self.assertEqual(self.request("POST", "/contact/login", good, {"Content-Length": str(len(good))})[0], 429)

    def test_successful_login_sets_secure_cookie_behind_https_proxy(self):
        good = "username=synthetic-user&password=synthetic-password"
        status, _, response = self.request("POST", "/contact/login", good, {"Content-Length": str(len(good)), "X-Forwarded-Proto": "https"})
        self.assertEqual(status, 303)
        cookie = response.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie); self.assertIn("Secure", cookie); self.assertIn("SameSite=Lax", cookie)
        status, _, response = self.request("POST", "/contact/login", good, {"Content-Length": str(len(good))})
        self.assertNotIn("Secure", response.getheader("Set-Cookie"))

    def test_malformed_content_length_is_rejected_cleanly(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("POST", "/contact/login"); conn.putheader("Content-Length", "abc"); conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400); conn.close()

    def test_expired_sessions_are_pruned_on_login(self):
        webui.SESSIONS["old"] = {"expires": 1, "csrf": "x"}
        good = "username=synthetic-user&password=synthetic-password"
        self.request("POST", "/contact/login", good, {"Content-Length": str(len(good))})
        self.assertNotIn("old", webui.SESSIONS)


if __name__ == "__main__":
    unittest.main()

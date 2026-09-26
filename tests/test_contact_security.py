import os
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path

os.environ.setdefault("VILLAGE_ROOT", "/tmp/ai-village-contact-test")
os.environ.setdefault("VILLAGE_SIGNAL_AUTH_USER", "synthetic-user")
os.environ.setdefault("VILLAGE_SIGNAL_AUTH_PASSWORD", "synthetic-password")
sys.path.insert(0, os.path.abspath("web"))

from web import webui


class ContactSessionSecurityTests(unittest.TestCase):
    def setUp(self):
        webui.SESSIONS.clear()

    def handler(self, token):
        return SimpleNamespace(headers={"Cookie": f"av_session={token}"})

    def test_session_has_expiring_csrf_token(self):
        webui.SESSIONS["synthetic-session"] = {"expires": webui.time.time() + 60, "csrf": "synthetic-csrf"}
        handler = self.handler("synthetic-session")
        self.assertEqual(webui.session_user(handler), "synthetic-user")
        self.assertEqual(webui.session_csrf(handler), "synthetic-csrf")

    def test_legacy_or_expired_session_has_no_csrf(self):
        webui.SESSIONS["legacy"] = webui.time.time() + 60
        self.assertEqual(webui.session_csrf(self.handler("legacy")), "")
        webui.SESSIONS["expired"] = {"expires": webui.time.time() - 1, "csrf": "old"}
        self.assertEqual(webui.session_user(self.handler("expired")), "")

    def test_contact_frontend_requests_csrf_token(self):
        source = Path("web/observatory.js").read_text(encoding="utf-8")
        self.assertIn("x.csrf_token", source)
        self.assertIn("csrf_token", Path("web/webui.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()

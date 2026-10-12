"""P150: daily conversation report and full-text archive of what the residents write to each other."""
import http.client
import json
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("VILLAGE_ROOT", "/tmp/ai-village-webui-hardening")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from village import conversations as conv

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=timezone.utc)
ROWS = [
    # id, source, sender, recipient, reply_to, content, timestamp
    ("m1", "direct", "02-explorer", "07-methodologist", None, "Can you share the disk-monitor script for hdd1?", "2026-10-10T08:00:00+00:00"),
    ("m2", "direct", "07-methodologist", "02-explorer", "m1", "I'll share disk-monitor.sh, threshold 80% on hdd1.", "2026-10-10T08:05:00+00:00"),
    ("m3", "direct", "07-methodologist", "02-explorer", None, "I'll share disk-monitor.sh, threshold 80% on hdd1.", "2026-10-10T09:00:00+00:00"),
    ("m4", "board", "07-methodologist", None, None, "```bash\necho hi\n```", "2026-10-10T09:10:00+00:00"),
    ("m5", "board", "01-king", None, None, "Jour fixe meeting agenda for tomorrow.", "2026-10-11T07:00:00+00:00"),
    ("m6", "direct", "village-auditor", "07-methodologist", None, "The agent produced an empty response.", "2026-10-11T07:30:00+00:00"),
    ("m7", "organic", "operator", "01-king", None, "A human signal from outside.", "2026-10-11T08:00:00+00:00"),
    ("m8", "direct", "03-librarian", "02-explorer", None, "password=hunter2 please check the habitat inventory", "2026-10-11T09:00:00+00:00"),
]


def make_db(path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE inbox_messages (id TEXT PRIMARY KEY, source TEXT, sender TEXT, recipient TEXT, "
                "reply_to TEXT, content TEXT, timestamp TEXT, created_at TEXT)")
    con.executemany("INSERT INTO inbox_messages VALUES (?,?,?,?,?,?,?,?)", [r + (r[-1],) for r in ROWS])
    con.commit()
    con.close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.db = self.root / "coordination.sqlite3"
        make_db(self.db)
        conv._topics.clear()

    def tearDown(self):
        self.tmp.cleanup()


class ClassifyTests(unittest.TestCase):
    def test_system_feedback_and_failed_actions(self):
        self.assertEqual(conv.classify("village-auditor", "direct", "disk"), "auditor")
        self.assertEqual(conv.classify("07-x", "board", "```bash\nls\n```"), "junk")
        self.assertEqual(conv.classify("07-x", "board", 'calendar_operation {"a":1}'), "junk")

    def test_code_in_a_direct_message_is_not_a_failed_action(self):
        self.assertNotEqual(conv.classify("07-x", "direct", "```bash\ndf -P /mnt/hdd1\n```"), "junk")

    def test_keyword_topic_and_fallback(self):
        self.assertEqual(conv.classify("02-x", "direct", "the disk-monitor on hdd1 needs a threshold"), "disk")
        self.assertEqual(conv.classify("02-x", "direct", "Jour fixe meeting at nine"), "meeting")
        self.assertEqual(conv.classify("02-x", "direct", "Hello there"), "other")


class LoadTests(Base):
    def test_signals_from_outside_are_never_loaded(self):
        ids = [m["id"] for m in conv.load_messages(self.db)]
        self.assertNotIn("m7", ids)
        self.assertEqual(len(ids), 7)

    def test_board_posts_go_to_all_and_secrets_are_redacted(self):
        by_id = {m["id"]: m for m in conv.load_messages(self.db)}
        self.assertEqual(by_id["m5"]["recipient"], "ALL")
        self.assertNotIn("hunter2", by_id["m8"]["content"])
        self.assertIn("password=<redacted>", by_id["m8"]["content"])

    def test_missing_database_gives_empty_list(self):
        self.assertEqual(conv.load_messages(self.root / "missing.sqlite3"), [])


class ReportTests(Base):
    def report(self):
        return conv.build_report(conv.load_messages(self.db), models={"02-explorer": "qwen3.5:4b"},
                                 names={"02-explorer": "explorer"}, now=NOW)

    def test_totals_matrix_and_agents(self):
        r = self.report()
        self.assertEqual(r["totals"], dict(all=7, direct=5, board=2, pairs=3))
        self.assertEqual(r["matrix"]["07-methodologist"]["02-explorer"], 2)
        self.assertEqual(r["matrix"]["07-methodologist"]["ALL"], 1)
        ids = [a["id"] for a in r["agents"]]
        self.assertEqual(ids[-1], "ALL")
        self.assertNotIn("operator", ids)
        explorer = next(a for a in r["agents"] if a["id"] == "02-explorer")
        self.assertEqual((explorer["name"], explorer["model"], explorer["resident"]), ("Explorer", "qwen3.5:4b", True))

    def test_resident_figures(self):
        s = self.report()["stats"]
        self.assertEqual(s["02-explorer"]["answer_rate"], 1.0)      # m1 answered by m2 within 6 h
        self.assertEqual(s["07-methodologist"]["answer_rate"], 0.0)
        self.assertEqual(s["07-methodologist"]["repeats"], 1)       # m3 repeats m2
        self.assertEqual(s["07-methodologist"]["promise_rate"], 1.0)
        self.assertEqual(s["07-methodologist"]["junk"], 1)
        self.assertEqual(s["07-methodologist"]["sent_board"], 1)
        self.assertEqual(s["02-explorer"]["received"], 3)

    def test_daily_series_and_findings(self):
        r = self.report()
        self.assertEqual(r["days"], ["2026-10-10", "2026-10-11"])
        self.assertEqual(sum(r["daily"][0]), 4)
        kinds = [f["kind"] for f in r["findings"]]
        self.assertEqual(kinds, ["top_topic", "top_pair", "promises", "junk", "answers", "repeats", "goal_talk"])
        top = r["findings"][0]
        self.assertEqual((top["topic"], top["count"]), ("disk", 3))

    def test_empty_inbox(self):
        r = conv.build_report([], now=NOW)
        self.assertEqual(r["totals"]["all"], 0)
        self.assertEqual(r["findings"], [])


class DailyCacheTests(Base):
    def test_built_once_per_day_and_cached_private_to_group(self):
        cache = self.root / "web" / "conversations-report.json"
        first = conv.daily_report(self.db, cache, now=NOW)
        self.assertEqual(stat.S_IMODE(cache.stat().st_mode), 0o640)
        with patch.object(conv, "build_report", side_effect=AssertionError("rebuilt")):
            again = conv.daily_report(self.db, cache, now=NOW.replace(hour=23))
        self.assertEqual(again["generated_at"], first["generated_at"])
        tomorrow = conv.daily_report(self.db, cache, now=NOW.replace(day=13))
        self.assertTrue(tomorrow["generated_at"].startswith("2026-10-13"))

    def test_unwritable_cache_still_serves_the_report(self):
        with patch.object(conv, "_write_atomic", side_effect=OSError("read-only")):
            r = conv.daily_report(self.db, self.root / "web" / "r.json", now=NOW)
        self.assertEqual(r["totals"]["all"], 7)


class ArchiveTests(Base):
    def test_pair_both_directions_or_one(self):
        both = conv.query_messages(self.db, agent="02-explorer", other="07-methodologist")
        self.assertEqual(both["total"], 3)
        one = conv.query_messages(self.db, agent="07-methodologist", other="02-explorer", both=False)
        self.assertEqual([m["id"] for m in one["messages"]], ["m3", "m2"])

    def test_topic_source_search_and_order(self):
        self.assertEqual(conv.query_messages(self.db, topic="junk")["total"], 1)
        self.assertEqual(conv.query_messages(self.db, source="board")["total"], 2)
        self.assertEqual(conv.query_messages(self.db, q="AGENDA")["messages"][0]["id"], "m5")
        oldest = conv.query_messages(self.db, newest_first=False)["messages"][0]
        self.assertEqual(oldest["id"], "m1")
        self.assertEqual(conv.query_messages(self.db, other="ALL")["total"], 2)

    def test_paging_is_clamped_and_replies_resolved(self):
        r = conv.query_messages(self.db, page=99, page_size=3, newest_first=False)
        self.assertEqual((r["page"], r["pages"], len(r["messages"])), (2, 3, 1))
        first = conv.query_messages(self.db, page_size=3, newest_first=False)
        self.assertEqual(first["replies"]["m1"]["sender"], "02-explorer")
        self.assertNotIn("m7", json.dumps(conv.query_messages(self.db, page_size=50)))


class WebRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from web import webui
        cls.webui = webui
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        make_db(root / "coordination.sqlite3")
        cls.saved = (webui.COORDINATION_DB, webui.CONVERSATIONS_CACHE, webui.ASSETS)
        webui.ASSETS = Path(__file__).resolve().parents[1] / "web"
        webui.COORDINATION_DB = root / "coordination.sqlite3"
        webui.CONVERSATIONS_CACHE = root / "web" / "conversations-report.json"
        cls.server = webui.ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.webui.COORDINATION_DB, cls.webui.CONVERSATIONS_CACHE, cls.webui.ASSETS = cls.saved
        cls.tmp.cleanup()

    def get(self, path):
        con = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        con.request("GET", path)
        res = con.getresponse()
        return res.status, res.read().decode("utf-8")

    def test_report_and_archive_routes(self):
        status, body = self.get("/api/conversations")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["totals"]["all"], 7)
        status, body = self.get("/api/conversations/messages?agent=02-explorer&other=07-methodologist&both=1")
        self.assertEqual(json.loads(body)["total"], 3)

    def test_view_is_served(self):
        status, body = self.get("/conversations")
        self.assertEqual(status, 200)
        self.assertIn('id="conversations-panel"', body)

    def test_bad_parameters_fall_back_to_safe_defaults(self):
        status, body = self.get("/api/conversations/messages?agent=..%2F..%2Fetc&topic=nope&source=organic&page=abc")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"], 7)
        self.assertEqual(data["page"], 0)


class AssetTests(unittest.TestCase):
    def test_view_navigation_and_both_languages(self):
        root = Path(__file__).resolve().parents[1] / "web"
        html = (root / "observatory.html").read_text()
        js = (root / "observatory.js").read_text()
        self.assertIn('href="/conversations"', html)
        self.assertIn('id="conversations-panel"', html)
        self.assertIn("conversations:['conversations-panel']", js)
        en = json.loads((root / "lang/en.json").read_text())
        de = json.loads((root / "lang/de.json").read_text())
        self.assertEqual(set(en["conversations_panel"]), set(de["conversations_panel"]))
        self.assertEqual(set(en["conversations_panel"]["topic"]), set(conv.TOPIC_IDS))
        kinds = ("top_topic", "top_pair", "promises", "junk", "answers", "repeats", "goal_talk")
        for kind in kinds:
            self.assertIn("f_" + kind, de["conversations_panel"])
        self.assertNotIn("content not public", en["board_panel"]["info_direct"])


if __name__ == "__main__":
    unittest.main()

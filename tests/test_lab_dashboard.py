"""P87: "Forschungslabor" dashboard - solo tasks + collaborative teams in
one read-only, cross-agent feed. Same real-HTTP-server test harness as
test_calendar_dashboard.py/test_gazette_dashboard.py."""
import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

os.environ.setdefault("VILLAGE_ROOT", "/tmp/ai-village-lab-dashboard")
os.environ.setdefault("VILLAGE_SIGNAL_AUTH_USER", "synthetic-user")
os.environ.setdefault("VILLAGE_SIGNAL_AUTH_PASSWORD", "synthetic-password")
sys.path.insert(0, os.path.abspath("web"))

from http.server import ThreadingHTTPServer
from web import webui
from village.coordinator import CoordinationStore
from village.teams import TeamStore


class LabDashboardTests(unittest.TestCase):
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
        webui.ASSETS = Path(__file__).resolve().parents[1] / "web"
        webui.EVENTS.write_text("")

        board = root / "board"
        tasks = CoordinationStore(board / "coordination.sqlite3", board)
        cls.queued = tasks.operate("01-king", {"action": "create", "title": "Queued task",
                                               "success_criterion": "a real, checkable result"})
        cls.in_progress = tasks.operate("02-explorer", {"action": "create", "title": "Active task",
                                                         "success_criterion": "a real, checkable result"})
        tasks.operate("02-explorer", {"action": "claim", "task_id": cls.in_progress["id"]})
        cls.deferred = tasks.operate("03-librarian", {"action": "create", "title": "Blocked task",
                                                       "success_criterion": "a real, checkable result"})
        tasks.operate("03-librarian", {"action": "claim", "task_id": cls.deferred["id"]})
        tasks.operate("03-librarian", {"action": "progress", "task_id": cls.deferred["id"],
                                       "blockers": "waiting on a peer's reply",
                                       "depends_on": [cls.queued["id"]]})
        cls.done = tasks.operate("04-artisan", {"action": "create", "title": "Finished task",
                                                "success_criterion": "a real, checkable result"})
        tasks.operate("04-artisan", {"action": "claim", "task_id": cls.done["id"]})
        tasks.operate("04-artisan", {"action": "complete", "task_id": cls.done["id"], "evidence": "done, verified"})

        teams = TeamStore(board / "coordination.sqlite3")
        cls.team = teams.create("05-interpreter", {"project": "Shared archive", "goal": "build it together",
                                                    "role": "researcher"})
        teams.join("06-operator", cls.team["id"], "retrieval")

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

    def rows(self):
        _, body = self.request("/api/lab")
        return {r["id"]: r for r in json.loads(body)}

    def test_lab_route_serves_the_spa_shell(self):
        status, body = self.request("/lab")
        self.assertEqual(status, 200)
        self.assertIn("AI VILLAGE", body)

    def test_solo_tasks_map_to_the_right_kanban_column(self):
        rows = self.rows()
        self.assertEqual(rows[self.queued["id"]]["column"], "queued")
        self.assertEqual(rows[self.in_progress["id"]]["column"], "in_progress")
        self.assertEqual(rows[self.deferred["id"]]["column"], "deferred")
        self.assertEqual(rows[self.done["id"]]["column"], "done")
        self.assertEqual(rows[self.queued["id"]]["kind"], "solo")

    def test_depends_on_is_surfaced(self):
        rows = self.rows()
        self.assertEqual(rows[self.deferred["id"]]["depends_on"], [self.queued["id"]])
        self.assertEqual(rows[self.queued["id"]]["depends_on"], [])

    def test_team_appears_as_a_single_collaborative_item_with_members(self):
        rows = self.rows()
        team_row = rows[self.team["id"]]
        self.assertEqual(team_row["kind"], "team")
        self.assertEqual(set(team_row["members"]), {"05-interpreter", "06-operator"})
        self.assertEqual(team_row["column"], "in_progress")

    def test_newest_updated_first(self):
        _, body = self.request("/api/lab")
        ids = [r["id"] for r in json.loads(body)]
        # the completed task was touched last of the four solo tasks
        self.assertLess(ids.index(self.done["id"]), ids.index(self.queued["id"]))


if __name__ == "__main__":
    unittest.main()

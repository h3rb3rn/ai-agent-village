import json
import tempfile
import unittest
from pathlib import Path

from web.event_history import read_history


class EventHistoryTests(unittest.TestCase):
    def test_merges_rotated_files_deduplicates_and_orders(self):
        with tempfile.TemporaryDirectory() as td:
            current, previous = Path(td) / "events.jsonl", Path(td) / "events.previous.jsonl"
            rows = [
                {"timestamp": "2026-01-01T00:00:01+00:00", "event_id": "a", "event": "old"},
                {"timestamp": "2026-01-01T00:00:02+00:00", "event_id": "b", "event": "middle"},
            ]
            previous.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
            current.write_text(json.dumps(rows[1]) + "\n" + json.dumps({"timestamp": "2026-01-01T00:00:03+00:00", "event_id": "c", "event": "new"}) + "\n")
            result = read_history((current, previous), limit=10)
            self.assertEqual([x["event_id"] for x in result], ["c", "b", "a"])

    def test_cursor_and_limit_are_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"
            path.write_text("\n".join(json.dumps({"timestamp": f"2026-01-01T00:00:0{i}+00:00", "event_id": str(i)}) for i in range(5)))
            result = read_history((path,), limit=2, before="2026-01-01T00:00:04+00:00")
            self.assertEqual(len(result), 2)
            self.assertTrue(all(x["event_id"] in {"3", "2", "1", "0"} for x in result))


if __name__ == "__main__":
    unittest.main()

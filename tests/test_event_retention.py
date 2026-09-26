import json
import tempfile
import unittest
from pathlib import Path

from village.event_retention import rotate_jsonl


class EventRetentionTests(unittest.TestCase):
    def test_rotation_keeps_archives_and_counts_dropped_lines(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td); path = root / "events.jsonl"; counter = root / "event-retention.json"
            path.write_text("new\n")
            (root / "events.jsonl.1").write_text("one\ntwo\n")
            (root / "events.jsonl.2").write_text("old\n")
            result = rotate_jsonl(path, max_bytes=1, keep=2, counter_path=counter)
            self.assertTrue(result["rotated"])
            self.assertEqual(result["dropped"], 1)
            self.assertEqual((root / "events.jsonl.1").read_text(), "new\n")
            self.assertEqual(json.loads(counter.read_text())["dropped_lines"], 1)

    def test_small_file_is_not_rotated(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"; path.write_text("ok\n")
            result = rotate_jsonl(path, max_bytes=100)
            self.assertFalse(result["rotated"])
            self.assertEqual(path.read_text(), "ok\n")


if __name__ == "__main__":
    unittest.main()

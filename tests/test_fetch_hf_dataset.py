"""P37-continuation: pulls a HuggingFace dataset config into a single pinned
local JSON file via the public datasets-server rows API - JSON pages, no
parquet/pandas dependency. Network is always mocked here; the real endpoint
is exercised manually (see docs/evidence/P37-import.md)."""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("fetch_hf_dataset", ROOT / "scripts/fetch-hf-dataset.py")
fetch_hf_dataset = importlib.util.module_from_spec(spec)
sys.modules["fetch_hf_dataset"] = fetch_hf_dataset
spec.loader.exec_module(fetch_hf_dataset)


def page_response(rows, num_rows_total):
    body = json.dumps({"rows": [{"row_idx": i, "row": r} for i, r in enumerate(rows)],
                       "num_rows_total": num_rows_total}).encode("utf-8")

    class _Resp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return body
    return _Resp()


class FetchAllRowsTests(unittest.TestCase):
    def test_paginates_until_num_rows_total_is_reached(self):
        all_rows = [{"question": f"q{i}"} for i in range(250)]
        pages = [all_rows[0:100], all_rows[100:200], all_rows[200:250]]
        opener = MagicMock(side_effect=[page_response(p, 250) for p in pages])
        result = self._call(opener)
        self.assertEqual(len(result), 250)
        self.assertEqual(result[0], {"question": "q0"})
        self.assertEqual(result[-1], {"question": "q249"})
        self.assertEqual(opener.call_count, 3)

    def _call(self, opener):
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            return fetch_hf_dataset.fetch_all_rows("ds", "cfg", "train", sleep_seconds=0)
        finally:
            urllib.request.urlopen = original

    def test_limit_stops_early_even_if_more_rows_exist(self):
        rows = [{"q": i} for i in range(100)]
        opener = MagicMock(return_value=page_response(rows, 10000))
        result = self._call_with_limit(opener, limit=37)
        self.assertEqual(len(result), 37)

    def _call_with_limit(self, opener, limit):
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            return fetch_hf_dataset.fetch_all_rows("ds", "cfg", "train", limit=limit, sleep_seconds=0)
        finally:
            urllib.request.urlopen = original

    def test_retries_on_429_and_502_then_succeeds(self):
        import urllib.error
        rate_limited = urllib.error.HTTPError("u", 429, "too many", {"Retry-After": "0"}, None)
        bad_gateway = urllib.error.HTTPError("u", 502, "bad gateway", {}, None)
        opener = MagicMock(side_effect=[rate_limited, bad_gateway, page_response([{"q": 1}], 1)])
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = opener
        with patch("time.sleep"):
            try:
                result = fetch_hf_dataset.fetch_all_rows("ds", "cfg", "train", sleep_seconds=0)
            finally:
                urllib.request.urlopen = original
        self.assertEqual(result, [{"q": 1}])
        self.assertEqual(opener.call_count, 3)

    def test_non_retryable_http_error_is_raised_immediately(self):
        import urllib.error
        not_found = urllib.error.HTTPError("u", 404, "not found", {}, None)
        opener = MagicMock(side_effect=[not_found])
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            with self.assertRaises(RuntimeError):
                fetch_hf_dataset.fetch_all_rows("ds", "cfg", "train", sleep_seconds=0)
        finally:
            urllib.request.urlopen = original
        self.assertEqual(opener.call_count, 1)

    def test_empty_page_stops_pagination(self):
        opener = MagicMock(return_value=page_response([], 500))
        result = self._call(opener)
        self.assertEqual(result, [])


class CheckpointResumeTests(unittest.TestCase):
    """P38-continuation: a large fetch (e.g. 240k+ Wikipedia rows, ~2400
    requests) must not lose all progress to one exhausted retry budget deep
    into the run - this is what makes fetch_all_rows resumable."""

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="village-checkpoint-"))
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)
        self.checkpoint = self.tmp / "out.json.partial.jsonl"

    def _call(self, opener, **kwargs):
        import urllib.request
        original = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            return fetch_hf_dataset.fetch_all_rows("ds", "cfg", "train", sleep_seconds=0,
                                                    checkpoint_path=self.checkpoint, **kwargs)
        finally:
            urllib.request.urlopen = original

    def test_successful_rows_are_written_to_the_checkpoint_as_they_arrive(self):
        rows = [{"q": i} for i in range(150)]
        opener = MagicMock(side_effect=[page_response(rows[0:100], 150), page_response(rows[100:150], 150)])
        self._call(opener)
        checkpointed = fetch_hf_dataset._load_checkpoint(self.checkpoint)
        self.assertEqual(len(checkpointed), 150)
        self.assertEqual(checkpointed[0], {"q": 0})
        self.assertEqual(checkpointed[-1], {"q": 149})

    def test_a_second_call_resumes_from_the_checkpoint_instead_of_refetching(self):
        rows = [{"q": i} for i in range(150)]
        first_opener = MagicMock(side_effect=[page_response(rows[0:100], 150),
                                              RuntimeError("simulated exhaustion mid-run")])
        with self.assertRaises(RuntimeError):
            self._call(first_opener)
        self.assertEqual(len(fetch_hf_dataset._load_checkpoint(self.checkpoint)), 100)

        # Resume: only the remaining page should be requested, at offset=100.
        second_opener = MagicMock(return_value=page_response(rows[100:150], 150))
        result = self._call(second_opener)
        self.assertEqual(len(result), 150)
        self.assertEqual(result, rows)
        requested_url = second_opener.call_args[0][0]
        self.assertIn("offset=100", requested_url)
        self.assertEqual(second_opener.call_count, 1)

    def test_a_fully_checkpointed_dataset_needs_no_further_requests(self):
        rows = [{"q": i} for i in range(50)]
        with self.checkpoint.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        # A page request at offset=50 legitimately returns empty - already complete.
        opener = MagicMock(return_value=page_response([], 50))
        result = self._call(opener)
        self.assertEqual(result, rows)

    def test_main_deletes_the_checkpoint_after_a_successful_full_run(self):
        with patch.object(fetch_hf_dataset, "fetch_all_rows", return_value=[{"q": 1}]):
            with self.checkpoint.open("w") as handle:
                handle.write('{"q": 1}\n')
            out = self.tmp / "out.json"
            sys.argv = ["fetch-hf-dataset.py", "--dataset-id", "d", "--config", "c", "--output", str(out)]
            fetch_hf_dataset.main()
        self.assertFalse(self.checkpoint.exists())
        self.assertEqual(json.loads(out.read_text()), [{"q": 1}])

    def test_no_checkpoint_flag_disables_checkpointing_entirely(self):
        rows = [{"q": 1}]
        with patch.object(fetch_hf_dataset, "fetch_all_rows", return_value=rows) as fetch:
            out = self.tmp / "out.json"
            sys.argv = ["fetch-hf-dataset.py", "--dataset-id", "d", "--config", "c",
                       "--output", str(out), "--no-checkpoint"]
            fetch_hf_dataset.main()
        self.assertIsNone(fetch.call_args.kwargs["checkpoint_path"])


class MainWritesDigestedFileTests(unittest.TestCase):
    def test_output_file_digest_matches_written_bytes(self):
        import hashlib
        import tempfile
        rows = [{"question": "q", "answer": "a"}]
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.json"
            fetch_hf_dataset.fetch_all_rows = MagicMock(return_value=rows)
            sys.argv = ["fetch-hf-dataset.py", "--dataset-id", "d", "--config", "c",
                       "--output", str(out)]
            fetch_hf_dataset.main()
            digest = hashlib.sha256(out.read_bytes()).hexdigest()
            self.assertEqual(json.loads(out.read_text()), rows)
            self.assertEqual(len(digest), 64)


if __name__ == "__main__":
    unittest.main()

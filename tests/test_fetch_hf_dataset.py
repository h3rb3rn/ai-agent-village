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

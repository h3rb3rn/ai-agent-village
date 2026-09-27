"""P33 continuation: bulk dataset import into the shared memory gateway.

An operator action (not a resident action): writes directly to the gateway's
own SQLite + outbox so Chroma/Neo4j projection still picks the rows up, but
bypasses the per-agent hourly quota that exists to bound a runaway LLM loop,
not a deliberate one-time seed load. Digest-verified source, deterministic
idempotency keys (safe to re-run), never fabricates content - every imported
row is a verbatim transform of the source file.
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("import_dataset", ROOT / "scripts/import-dataset.py")
import_dataset = importlib.util.module_from_spec(spec)
sys.modules["import_dataset"] = spec.loader.exec_module(import_dataset) or import_dataset

gateway = import_dataset._load_gateway()


SAMPLE = [
    {"input": "Compress data.txt using bzip2", "output": "bzip2 data.txt"},
    {"input": "List files in the current directory", "output": "ls"},
    {"input": "", "output": "ls -a"},  # missing task: must be skipped
    {"input": "Show disk usage", "output": ""},  # missing command: must be skipped
]


class LinuxCommandTransformTests(unittest.TestCase):
    def test_pairs_become_content_and_skip_incomplete_rows(self):
        records = list(import_dataset.linux_command_pairs(SAMPLE))
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["content"], "Task: Compress data.txt using bzip2\nCommand: bzip2 data.txt")
        self.assertEqual(records[0]["kind"], "reference")

    def test_non_list_input_is_rejected(self):
        with self.assertRaises(ValueError):
            list(import_dataset.linux_command_pairs({"not": "a list"}))


class DigestVerificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.file = self.tmp / "data.json"
        self.file.write_text(json.dumps(SAMPLE))

    def test_matching_digest_passes(self):
        import hashlib
        real = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.assertEqual(import_dataset.verify_digest(self.file, real), real)

    def test_mismatched_digest_is_rejected(self):
        with self.assertRaises(ValueError):
            import_dataset.verify_digest(self.file, "0" * 64)

    def test_empty_expected_digest_skips_verification(self):
        import_dataset.verify_digest(self.file, "")  # must not raise


class ImportIntoGatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        gateway.DB = Path(self.tmp.name) / "memory.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def import_sample(self, limit=None, dry_run=False):
        records = import_dataset.linux_command_pairs(SAMPLE)
        return import_dataset.import_records(
            records, dataset_id="org/name", digest="abc123",
            agent_label="dataset-import", limit=limit, dry_run=dry_run,
        )

    def test_valid_rows_are_imported_incomplete_rows_are_skipped(self):
        stats = self.import_sample()
        self.assertEqual(stats["imported"], 2)
        self.assertEqual(stats["skipped_empty"], 0)  # the transform already dropped incomplete pairs

        conn = gateway.db()
        rows = conn.execute("SELECT agent, scope, kind, source_event, confidence FROM memories").fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["agent"], "dataset-import")
            self.assertEqual(row["scope"], "shared")
            self.assertEqual(row["kind"], "reference")
            self.assertEqual(row["source_event"], "dataset:org/name:abc123")
            self.assertEqual(row["confidence"], 1.0)

    def test_outbox_entries_are_written_so_projection_still_runs(self):
        self.import_sample()
        conn = gateway.db()
        count = conn.execute("SELECT count(*) FROM memory_outbox").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_rerunning_the_same_import_is_idempotent(self):
        first = self.import_sample()
        second = self.import_sample()
        self.assertEqual(first["imported"], 2)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped_duplicate"], 2)
        conn = gateway.db()
        count = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)  # not 4

    def test_dry_run_writes_nothing(self):
        stats = self.import_sample(dry_run=True)
        self.assertEqual(stats["imported"], 2)
        conn = gateway.db()
        count = conn.execute("SELECT count(*) FROM memories").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_limit_caps_the_import_count(self):
        stats = self.import_sample(limit=1)
        self.assertEqual(stats["imported"], 1)

    def test_oversized_content_is_skipped_not_truncated_silently_into_something_else(self):
        big = [{"input": "x" * 20000, "output": "echo hi"}]
        stats = import_dataset.import_records(
            import_dataset.linux_command_pairs(big), dataset_id="org/name", digest="abc123",
            agent_label="dataset-import", limit=None, dry_run=False,
        )
        self.assertEqual(stats["skipped_oversized"], 1)
        self.assertEqual(stats["imported"], 0)


if __name__ == "__main__":
    unittest.main()

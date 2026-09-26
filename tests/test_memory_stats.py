import sqlite3
import unittest

from memory.gateway import projection_stats


class ProjectionStatsTests(unittest.TestCase):
    def test_reports_lag_and_status_without_backend_assumptions(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
        CREATE TABLE memory_outbox(sequence_id INTEGER PRIMARY KEY);
        CREATE TABLE projection_states(
          backend TEXT PRIMARY KEY, last_sequence_id INTEGER NOT NULL,
          last_projected_at TEXT, error_count INTEGER NOT NULL, last_error TEXT,
          status TEXT NOT NULL
        );
        INSERT INTO memory_outbox(sequence_id) VALUES (1),(2),(3);
        INSERT INTO projection_states VALUES ('chroma', 2, NULL, 0, NULL, 'active');
        INSERT INTO projection_states VALUES ('neo4j', 1, NULL, 2, 'offline', 'degraded');
        """)
        result = projection_stats(conn)
        self.assertEqual(result["max_sequence_id"], 3)
        self.assertEqual(result["backends"]["chroma"]["lag"], 1)
        self.assertEqual(result["backends"]["neo4j"]["status"], "degraded")


if __name__ == "__main__":
    unittest.main()

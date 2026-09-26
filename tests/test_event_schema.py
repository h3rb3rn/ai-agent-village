import json
import tempfile
import unittest
from pathlib import Path

from web.runtime import Resident


class EventSchemaTests(unittest.TestCase):
    def test_runtime_events_have_stable_run_and_unique_event_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env = {
                'AGENT_ID': '01-test', 'AGENT_NAME': 'test', 'AGENT_ROLE': 'resident',
                'VILLAGE_ROOT': str(root), 'HOME': str(root / 'users' / 'test'),
            }
            resident = Resident(env)
            resident.event('test_one', 'one')
            resident.event('test_two', 'two')
            rows = [json.loads(line) for line in (root / 'board' / 'events.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row['schema_version'] == '1.0' for row in rows))
            self.assertEqual(len({row['event_id'] for row in rows}), 2)
            self.assertEqual(len({row['run_id'] for row in rows}), 1)


if __name__ == '__main__':
    unittest.main()

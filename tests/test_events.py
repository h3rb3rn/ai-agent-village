import json
import tempfile
import unittest
from pathlib import Path

from village.events import append_event, make_event


class EventEnvelopeTests(unittest.TestCase):
    def test_envelope_has_identity_and_utc_timestamp(self):
        event = make_event(source='test', kind='observation', detail='ok')
        self.assertEqual(event['schema_version'], '1.0')
        self.assertTrue(event['event_id'])
        self.assertEqual(event['run_id'], 'test')
        self.assertIn('+00:00', event['timestamp'])

    def test_append_is_jsonl_and_preserves_fields(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'events.jsonl'
            append_event(path, source='test', kind='alert', alert={'code': 'x'})
            row = json.loads(path.read_text())
            self.assertEqual(row['alert']['code'], 'x')


if __name__ == '__main__':
    unittest.main()

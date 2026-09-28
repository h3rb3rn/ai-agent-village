import ast
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from village.meetings import MeetingStore

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location('meeting_scheduler', ROOT / 'scripts/meeting-scheduler.py')
meeting_scheduler = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(meeting_scheduler)


class MeetingSchedulerPolicyTests(unittest.TestCase):
    def test_scheduler_checks_active_meetings_before_creating_one(self):
        text = (ROOT / 'scripts/meeting-scheduler.py').read_text()
        tree = ast.parse(text)
        self.assertIn('if meetings.active():', text)
        self.assertIn('continue', text)
        self.assertIsNotNone(tree)


class RecordUnreportedTests(unittest.TestCase):
    """P58-follow-up (operator directive: 'Nicht nur beobachten wenn du GAPs
    identifizierst, sondern proaktiv loesen'): force-closing a stale meeting
    on age alone used to erase all trace of who never reported - up to 4h of
    gated (P56) non-compliance with zero lasting consequence."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='village-scheduler-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.meetings = MeetingStore(self.tmp / 'coordination.sqlite3')
        self.events_path = self.tmp / 'events.jsonl'
        self.meetings.schedule('jour_fixe', 'agenda', '2026-09-28T02:00:00Z', 'rotating-council', 'm1')
        self.meetings.report('m1', '01-king', achieved='x', evidence='y', next_step='z', blockers='')

    def _events(self):
        if not self.events_path.exists():
            return []
        return [json.loads(line) for line in self.events_path.read_text().splitlines() if line.strip()]

    def test_residents_who_never_reported_get_one_event_each(self):
        meeting_scheduler.record_unreported(self.meetings, self.events_path, 'm1',
                                             ['01-king', '02-explorer', '03-librarian'])
        unreported_agents = {e['agent'] for e in self._events() if e.get('event') == 'meeting_unreported'}
        self.assertEqual(unreported_agents, {'02-explorer', '03-librarian'})

    def test_a_resident_who_reported_gets_no_event(self):
        meeting_scheduler.record_unreported(self.meetings, self.events_path, 'm1', ['01-king'])
        self.assertEqual(self._events(), [])

    def test_event_carries_both_kind_and_event_fields(self):
        # village/events.py::append_event writes 'kind'; the Auditor's
        # existing signatures all read 'event' - both must be present so
        # this signal is visible to the same scan() pipeline as every
        # resident-authored event, without special-casing one detector.
        meeting_scheduler.record_unreported(self.meetings, self.events_path, 'm1', ['02-explorer'])
        e = self._events()[0]
        self.assertEqual(e['kind'], 'meeting_unreported')
        self.assertEqual(e['event'], 'meeting_unreported')
        self.assertEqual(e['agent'], '02-explorer')
        self.assertIn('meeting_id=m1', e['detail'])


if __name__ == '__main__':
    unittest.main()

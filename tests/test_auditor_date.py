"""P153 (F08): the auditor's judge knows today's date and never calls today 'the future'."""
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from village.auditor import AuditStore
from village.auditor_llm import ReviewCandidate, build_request, full_audit_cycle, misdated_future, select_candidates
from village.coordinator import CoordinationStore

NOW = datetime(2026, 10, 12, 0, 40, tzinfo=timezone.utc)
# The live finding of 2026-10-12 00:40 UTC (07-methodologist), shortened.
LIVE_FALSE_POSITIVE = ("The message claims 'Jour Fixe 2026-10-12' is scheduled for 'today', but the date 2026-10-12 "
                       "is in the future relative to any current real-world time. Correct the date to the actual "
                       "current date (e.g., 2024-05-21).")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def judge_says(problem, solution='fix it'):
    body = {'message': {'content': json.dumps({'has_issue': True, 'category': 'date', 'problem': problem,
                                               'solution': solution, 'confidence': 0.95})}}
    return lambda request, timeout=0: FakeResponse(json.dumps(body).encode())


class PromptTests(unittest.TestCase):
    def test_current_date_and_event_time_are_in_every_request(self):
        cand = ReviewCandidate('07-methodologist', 'm', 'e1', 'a public board message', 'Jour Fixe 2026-10-12 today',
                               '2026-10-12T00:39:58+00:00')
        prompt = json.loads(build_request(cand, now=NOW).data)['messages'][1]['content']
        self.assertIn('Current date and time: 2026-10-12 00:40 UTC', prompt)
        self.assertIn('produced at 2026-10-12T00:39:58+00:00', prompt)
        self.assertIn('a date up to today is not in the future', prompt)

    def test_candidates_carry_the_event_timestamp(self):
        events = [{'agent': 'a', 'event': 'board_message', 'event_id': 'e1', 'timestamp': '2026-10-12T00:39:58+00:00',
                   'detail': 'message=hello'}]
        self.assertEqual(select_candidates(events)[0].timestamp, '2026-10-12T00:39:58+00:00')

    def test_old_positional_construction_still_works(self):
        self.assertEqual(ReviewCandidate('a', 'm', 'e', 'k', 't').timestamp, '')


class GoldCaseTests(unittest.TestCase):
    def test_today_called_future_is_a_misjudgement(self):
        self.assertTrue(misdated_future(LIVE_FALSE_POSITIVE, 'Jour Fixe 2026-10-12 today', NOW))

    def test_a_real_future_date_is_kept(self):
        self.assertFalse(misdated_future('The deadline 2026-12-01 is in the future, it cannot have passed.',
                                         'The 2026-12-01 deadline has passed.', NOW))

    def test_findings_without_future_or_without_dates_are_kept(self):
        self.assertFalse(misdated_future('The command is missing a closing quote.', 'echo "x', NOW))
        self.assertFalse(misdated_future('This refers to a future meeting that was never scheduled.', 'see you then', NOW))


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='village-audit-date-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = AuditStore(self.tmp / 'audit.sqlite3')
        self.coord = CoordinationStore(self.tmp / 'coordination.sqlite3', self.tmp)
        self.events = [{'agent': '07-methodologist', 'event': 'board_message', 'event_id': 'e1', 'model': 'm',
                        'timestamp': '2026-10-12T00:39:58+00:00', 'detail': 'message=Jour Fixe 2026-10-12 is today.'}]

    def test_the_live_false_positive_is_never_delivered(self):
        writer = MagicMock(return_value={'id': 'mem'})
        stats = full_audit_cycle(self.events, self.store, coordination_store=self.coord, memory_writer=writer,
                                 opener=judge_says(LIVE_FALSE_POSITIVE), now=NOW)
        self.assertEqual((stats['llm_findings'], stats['llm_date_rejected'], stats['llm_delivered']), (0, 1, 0))  # discarded, not counted as a finding
        writer.assert_not_called()

    def test_an_ordinary_finding_is_still_delivered(self):
        stats = full_audit_cycle(self.events, self.store, coordination_store=self.coord,
                                 memory_writer=MagicMock(return_value={'id': 'mem'}),
                                 opener=judge_says('The message repeats itself without new information.'), now=NOW)
        self.assertEqual(stats['llm_date_rejected'], 0)
        self.assertEqual(stats['llm_findings'], 1)


if __name__ == '__main__':
    unittest.main()

"""P154: the auditor's judge never reviews an empty output and never hands a resident its own verdict schema."""
import io
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from village.auditor import AuditStore
from village.auditor_llm import select_candidates, full_audit_cycle
from village.coordinator import CoordinationStore

NOW = datetime(2026, 10, 12, 13, 33, tzinfo=timezone.utc)
# The live finding of 2026-10-12 13:33 UTC to 01-king, verbatim without the "[village-auditor]" prefix.
LIVE_SCHEMA_ECHO = ("The agent produced an empty response instead of analyzing the input. The agent must generate a "
                    "JSON verdict with has_issue, category, problem, solution, and confidence fields.")


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def judge_says(problem, solution='fix it'):
    body = {'message': {'content': json.dumps({'has_issue': True, 'category': 'output', 'problem': problem,
                                               'solution': solution, 'confidence': 0.95})}}
    return lambda request, timeout=0: FakeResponse(json.dumps(body).encode())


def rejected(event_id, reason, preview, agent='01-king'):
    return {'agent': agent, 'event': 'invalid_decision_detail', 'event_id': event_id, 'model': 'm',
            'timestamp': '2026-10-12T13:32:00+00:00', 'detail': f'reason={reason}; preview={preview}'}


class SelectionTests(unittest.TestCase):
    def test_empty_outputs_never_reach_the_judge(self):
        events = [rejected('e1', 'no final answer returned', ''),
                  rejected('e2', 'output budget exhausted', '   '),
                  {'agent': 'a', 'event': 'board_message', 'event_id': 'e3', 'detail': 'message='}]
        self.assertEqual(select_candidates(events), [])

    def test_outputs_with_text_are_still_selected(self):
        events = [rejected('e1', 'action is not an object', '"hello"'),
                  {'agent': 'a', 'event': 'board_message', 'event_id': 'e2', 'detail': 'message=hello'}]
        self.assertEqual([c.source_event_id for c in select_candidates(events)], ['e1', 'e2'])


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='village-audit-empty-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = AuditStore(self.tmp / 'audit.sqlite3')
        self.coord = CoordinationStore(self.tmp / 'coordination.sqlite3', self.tmp)

    def cycle(self, events, problem, writer):
        return full_audit_cycle(events, self.store, coordination_store=self.coord, memory_writer=writer,
                                opener=judge_says(problem), now=NOW)

    def test_the_live_empty_case_makes_no_judge_call_and_no_delivery(self):
        writer = MagicMock(return_value={'id': 'mem'})
        opener = MagicMock(side_effect=AssertionError('judge must not be called'))
        stats = full_audit_cycle([rejected('e1', 'no final answer returned', '')], self.store,
                                 coordination_store=self.coord, memory_writer=writer, opener=opener, now=NOW)
        self.assertEqual((stats['llm_candidates'], stats['llm_findings']), (0, 0))
        opener.assert_not_called()
        writer.assert_not_called()

    def test_a_schema_echo_on_a_real_text_is_discarded(self):
        writer = MagicMock(return_value={'id': 'mem'})
        stats = self.cycle([rejected('e1', 'action is not an object', '"ok"')], LIVE_SCHEMA_ECHO, writer)
        self.assertEqual((stats['llm_findings'], stats['llm_schema_echo_rejected'], stats['llm_delivered']), (0, 1, 0))
        writer.assert_not_called()

    def test_an_ordinary_finding_is_still_counted(self):
        stats = self.cycle([rejected('e1', 'action is not an object', '"ok"')],
                           'The response is a bare string instead of an action.', MagicMock(return_value={'id': 'mem'}))
        self.assertEqual((stats['llm_findings'], stats['llm_schema_echo_rejected']), (1, 0))


if __name__ == '__main__':
    unittest.main()

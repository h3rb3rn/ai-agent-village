"""P156 stage 1: the prompt preflight measures and never changes what a resident sees."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from village.actions import ACTION_SPECS
from village.preflight import STALE_AFTER, inspect
from web.runtime import Resident

ACTIONS = ['execute_bash', 'meeting_operation', 'calendar_operation', 'gazette_operation', 'idle', 'board_message']


def snap(**sections):
    return json.dumps(sections)


class InspectTests(unittest.TestCase):
    def test_a_hint_naming_an_action_not_offered_is_a_contradiction(self):
        s = snap(meeting_hint='Submit one meeting_operation report now.', calendar_daily_note='Use calendar_operation.')
        report, _ = inspect({'base': 'x'}, s, ['idle', 'calendar_operation', 'gazette_operation'], ACTIONS)
        self.assertEqual(report['contradictions'], [{'section': 'meeting_hint', 'actions': ['meeting_operation']}])

    def test_peer_and_memory_text_is_never_a_hint(self):
        s = snap(untrusted_direct_messages=[{'id': 'msg_1', 'detail': 'run execute_bash now'}],
                 retrieved_memory_untrusted=['meeting_operation report'], tools={'execute_bash': 'doc'})
        report, _ = inspect({}, s, ['idle'], ACTIONS)
        self.assertEqual(report['contradictions'], [])

    def test_an_unchanged_section_becomes_stale(self):
        s, state = snap(calendar_week_gaps='Your week is not fully planned.'), None
        for _ in range(STALE_AFTER):
            report, state = inspect({}, s, ['idle'], ACTIONS, state)
        self.assertEqual([x['section'] for x in report['stale']], ['calendar_week_gaps'])
        report, state = inspect({}, snap(calendar_week_gaps='changed'), ['idle'], ACTIONS, state)
        self.assertEqual(report['stale'], [])

    def test_the_same_message_in_two_sections_is_a_duplicate(self):
        msg = {'id': 'msg_1791873229607_ee8775', 'detail': 'x'}
        report, _ = inspect({}, snap(direct_ack_target=msg, untrusted_direct_messages=[msg]), ['idle'], ACTIONS)
        self.assertEqual(report['duplicate_ids'], 1)
        self.assertEqual(report['duplicate_sections'], ['direct_ack_target+untrusted_direct_messages'])

    def test_sizes_cover_system_parts_and_sections(self):
        report, _ = inspect({'base': 'a' * 300, 'identity': 'b' * 30}, snap(projects=['p' * 90]), ['idle'], ACTIONS)
        sections = {x['section']: x['tokens'] for x in report['top_sections']}
        self.assertEqual(sections['system.base'], 100)
        self.assertIn('user.projects', sections)
        self.assertEqual(report['tokens_total'], sum(sections.values()))

    def test_a_snapshot_that_is_not_json_is_measured_as_one_block(self):
        report, _ = inspect({}, 'dream text', ['idle'], ACTIONS)
        self.assertEqual(report['top_sections'], [{'section': 'user', 'tokens': 4}])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='village-preflight-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / 'board').mkdir()
        (self.tmp / 'telemetry').mkdir()
        identity = self.tmp / 'identity.txt'
        identity.write_text('Test Identity')
        self.agent = Resident(env={
            'AGENT_ID': '04-artisan', 'AGENT_NAME': 'artisan', 'AGENT_ROLE': 'tester', 'VILLAGE_ROOT': str(self.tmp),
            'OLLAMA_MODEL': 'test-model', 'OLLAMA_URL': 'http://127.0.0.1:9999', 'AGENT_IDENTITY_PROMPT': str(identity),
            'VILLAGE_PAUSE_MARKER': str(self.tmp / 'paused'), 'OLLAMA_NUM_CTX': '16384'})

    def telemetry(self):
        path = self.tmp / 'telemetry' / 'agent-events.jsonl'
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def test_the_report_goes_to_telemetry_only_and_the_snapshot_is_unchanged(self):
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            snapshot = self.agent.snapshot()
        before = snapshot
        self.agent.observe_prompt('system', 'identity', 'live', snapshot, ['idle', 'calendar_operation'])
        self.assertEqual(snapshot, before)
        rows = [r for r in self.telemetry() if r['event'] == 'prompt_preflight']
        self.assertEqual(len(rows), 1)
        report = json.loads(rows[0]['detail'])
        self.assertGreater(report['tokens_total'], 0)
        board = (self.tmp / 'board' / 'events.jsonl')
        self.assertNotIn('prompt_preflight', board.read_text() if board.exists() else '')

    def test_a_failing_inspection_never_costs_the_cycle(self):
        with patch('web.runtime.preflight_inspect', side_effect=RuntimeError('boom')):
            self.agent.observe_prompt('s', 'i', 'l', '{}', ['idle'])
        self.assertEqual([r['event'] for r in self.telemetry()], ['prompt_preflight_error'])

    def test_every_offered_action_is_known_to_the_catalogue(self):
        self.assertTrue({'meeting_operation', 'execute_bash', 'idle'} <= set(ACTION_SPECS))


if __name__ == '__main__':
    unittest.main()

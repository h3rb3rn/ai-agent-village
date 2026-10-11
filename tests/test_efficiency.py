"""P135: measured waste removed - crash on non-object arguments, dead tasks in
every prompt, repeated meeting reports, prompt prefix not cacheable. Local."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from runtime import Resident, topic_saturation, order_for_prefix_cache, PROJECTS_IN_CONTEXT
from decision import decision


class DecisionTests(unittest.TestCase):
    def test_string_arguments_are_a_malformed_action_not_a_crash(self):
        for args in ('ls -la', ['a', 'b']):
            parsed = decision({'message': {'content': json.dumps({'name': 'execute_bash', 'arguments': args})}})
            self.assertIn('arguments must be a JSON object', parsed.get('fallback_reason', ''))


class SaturationTests(unittest.TestCase):
    def test_counts_recurring_words_of_finished_tasks_only(self):
        tasks = ([{'title': f'disk-monitor.sh variant {i}', 'status': 'abandoned'} for i in range(6)]
                 + [{'title': 'disk-monitor.sh live', 'status': 'open'}]
                 + [{'title': 'compression idea', 'status': 'complete'}])
        sat = topic_saturation(tasks)
        self.assertEqual(sat[0]['topic'], 'disk-monitor.sh')
        self.assertEqual(sat[0]['finished_or_abandoned_tasks'], 6)
        self.assertNotIn('compression', [x['topic'] for x in sat])  # below threshold


class PrefixOrderTests(unittest.TestCase):
    def test_stable_fields_first_volatile_last(self):
        ctx = {'measured_resources': 1, 'last_action_feedback': 2, 'other': 3, 'identity': 4, 'tools': 5}
        keys = list(order_for_prefix_cache(ctx))
        self.assertEqual(keys[:2], ['identity', 'tools'])
        self.assertEqual(keys[-1], 'last_action_feedback')
        self.assertLess(keys.index('other'), keys.index('measured_resources'))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        self.env = dict(os.environ, AGENT_ID='02-b', AGENT_NAME='b', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), VILLAGE_MAX_OUTPUT_BYTES='4096',
                        VILLAGE_COMMAND_TIMEOUT_SECONDS='2', OLLAMA_NUM_CTX='262144',
                        VILLAGE_POLICY_FILE='/nonexistent', VILLAGE_POLICY_LOCAL_FILE=str(self.root / 'none.json'))
        for target in ('runtime.calendar_is_workday', 'runtime.gazette_deadline_passed'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False)
        p.start(); self.addCleanup(p.stop)
        self.agent = Resident(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self):
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            return json.loads(self.agent.snapshot())

    def test_only_live_tasks_reach_the_prompt_own_first(self):
        dead = [{'id': f'd{i}', 'title': f'disk-monitor.sh take {i}', 'owner': '03-c', 'status': 'abandoned'}
                for i in range(40)]
        live = [{'id': f'o{i}', 'title': f'open {i}', 'owner': None, 'status': 'open'} for i in range(20)]
        mine = {'id': 'mine', 'title': 'my work', 'owner': '02-b', 'status': 'active'}
        self.agent.tasks.path.write_text(json.dumps(dead + live + [mine]))
        ctx = self.snapshot()
        ids = [p['id'] for p in ctx['projects']]
        self.assertEqual(ids[0], 'mine')
        self.assertFalse(any(i.startswith('d') for i in ids))
        self.assertLessEqual(len(ids), PROJECTS_IN_CONTEXT)
        self.assertEqual(ctx['village_topic_saturation'][0]['topic'], 'disk-monitor.sh')

    def test_snapshot_starts_with_stable_fields(self):
        keys = list(self.snapshot())
        self.assertEqual(keys[0], 'runtime')
        self.assertGreater(keys.index('measured_resources'), keys.index('projects'))
        self.assertEqual(keys[-1], 'last_action_feedback') if 'last_action_feedback' in keys else None

    def test_meeting_operation_hidden_once_reported(self):
        self.agent.meetings.schedule('standup', 'status', '2026-10-11T08:00:00Z', meeting_id='m1')
        self.assertIn('meeting_operation', self.agent.effective_allowed_actions())
        self.agent.meetings.report('m1', '02-b', 'did x', 'file y', 'next z', 'none')
        self.assertNotIn('meeting_operation', self.agent.effective_allowed_actions())


if __name__ == '__main__':
    unittest.main()

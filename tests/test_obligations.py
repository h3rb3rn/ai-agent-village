"""P126: runtime obligations are hints by default; only the operator's
hard_obligations (default: Gazette review + close) may block an action.
Local, synthetic."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
import runtime as rt
from runtime import Resident
from village.policy import agent_policy, OBLIGATIONS, DEFAULT_HARD_OBLIGATIONS


class PolicyTests(unittest.TestCase):
    def test_default_hard_set_is_only_the_gazette_editor_steps(self):
        self.assertEqual(agent_policy('x', {}).hard_obligations, ('gazette_review', 'gazette_close'))

    def test_all_none_and_list_values(self):
        self.assertEqual(agent_policy('x', {'defaults': {'hard_obligations': 'all'}}).hard_obligations, OBLIGATIONS)
        self.assertEqual(agent_policy('x', {'defaults': {'hard_obligations': 'none'}}).hard_obligations, ())
        pol = agent_policy('x', {'defaults': {'hard_obligations': ['meeting_report', 'bogus']}})
        self.assertEqual(pol.hard_obligations, ('meeting_report',))

    def test_invalid_value_falls_back_to_default(self):
        self.assertEqual(agent_policy('x', {'defaults': {'hard_obligations': 42}}).hard_obligations,
                         DEFAULT_HARD_OBLIGATIONS)


class DefaultAdvisoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        # Mirrors the live local policy: knowledgebase_gate=mandatory, no hard_obligations key.
        (self.root / 'policy.json').write_text(json.dumps({'defaults': {'knowledgebase_gate': 'mandatory'}}))
        self.env = dict(os.environ, AGENT_ID='02-b', AGENT_NAME='b', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), VILLAGE_MAX_OUTPUT_BYTES='4096',
                        VILLAGE_COMMAND_TIMEOUT_SECONDS='2', VILLAGE_POLICY_FILE='/nonexistent',
                        VILLAGE_POLICY_LOCAL_FILE=str(self.root / 'policy.json'))
        p = patch('runtime.gazette_deadline_passed', return_value=False); p.start(); self.addCleanup(p.stop)
        self.agent = Resident(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def test_calendar_and_writing_slot_never_block_by_default(self):
        with patch('runtime.calendar_is_workday', return_value=True), \
             patch.object(Resident, 'calendar_pending_daily_plan', return_value=True), \
             patch.object(Resident, 'calendar_pending_week_gaps', return_value=('2026-10-12', [(540, 1020)])), \
             patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=True):
            for i in range(rt.CALENDAR_PLAN_CEILING + 3):
                self.assertTrue(self.agent.guard('calc_operation', {'tool': 'calc', 'expression': f'{i}+1'}))
        self.assertGreaterEqual(self.agent.state['calendar_plan_pressure'], rt.CALENDAR_PLAN_CEILING)  # still measured

    def test_meeting_report_never_blocks_by_default(self):
        self.agent.meetings.schedule('jour_fixe', 'status', '2026-10-10T10:00:00Z', meeting_id='m1')
        with patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False):
            for i in range(rt.MEETING_REPORT_CEILING + 3):
                self.assertTrue(self.agent.guard('calc_operation', {'tool': 'calc', 'expression': f'{i}*2'}))

    def test_mandatory_knowledgebase_gate_is_only_shown_not_enforced(self):
        self.agent.tasks.operate('02-b', dict(action='create', title='t', success_criterion='c'))
        task_id = json.loads((self.root / 'board/work-items.json').read_text())[0]['id']
        self.agent.tasks.operate('02-b', dict(action='claim', task_id=task_id))
        with patch.object(self.agent, 'memory', return_value={'items': []}), \
             patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False):
            ctx = json.loads(self.agent.snapshot())
            self.assertEqual(ctx['collaboration_checkpoint']['enforcement'], 'advisory')
            self.assertTrue(self.agent.guard('execute_bash', {'command': 'echo work'}))

    def test_week_gap_hint_is_not_shown_by_default(self):
        with patch.object(Resident, 'calendar_pending_week_gaps', return_value=('2026-10-12', [(540, 1020)])):
            self.assertEqual(self.agent.calendar_week_gap_hint('2026-10-10'), '')

    def test_meeting_operation_offered_only_while_a_meeting_is_open(self):
        self.assertNotIn('meeting_operation', self.agent.effective_allowed_actions())
        self.agent.meetings.schedule('standup', 'status', '2026-10-10T08:00:00Z', meeting_id='m2')
        self.assertIn('meeting_operation', self.agent.effective_allowed_actions())


class EditorStepsStayHardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        self.env = dict(os.environ, AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king',
                        VILLAGE_ROOT=str(self.root), VILLAGE_MAX_OUTPUT_BYTES='4096',
                        VILLAGE_COMMAND_TIMEOUT_SECONDS='2', VILLAGE_POLICY_FILE='/nonexistent',
                        VILLAGE_POLICY_LOCAL_FILE=str(self.root / 'none.json'))
        for target in ('runtime.calendar_is_workday', 'runtime.gazette_deadline_passed'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False)
        p.start(); self.addCleanup(p.stop)
        self.agent = Resident(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def test_compile_still_blocks_after_the_ceiling(self):
        edition = {'id': '2026-10-10', 'contributions': []}
        with patch.object(Resident, 'gazette_closable_editions', return_value=[edition]):
            results = [self.agent.guard('calc_operation', {'tool': 'calc', 'expression': f'{i}-1'})
                       for i in range(rt.GAZETTE_CLOSE_CEILING + 1)]
        self.assertFalse(results[-1])
        self.assertIn('Compile required', self.agent.state['last_result']['result'])

    def test_assign_no_longer_blocks_king(self):
        with patch.object(Resident, 'gazette_pending_assignment', return_value='2026-10-10'):
            for i in range(rt.GAZETTE_ASSIGN_CEILING + 3):
                self.assertTrue(self.agent.guard('calc_operation', {'tool': 'calc', 'expression': f'{i}/1'}))


if __name__ == '__main__':
    unittest.main()

"""P136: focus cycles between full village overviews. Local, synthetic, no network."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from runtime import Resident
from village.focus import (choose_mode, focus_allowed, focus_context, focus_enabled, full_every,
                           OVERVIEW_FIELDS, FOCUS_HINT)

OWN_TASK = {'id': 't-own', 'title': 'own experiment', 'owner': '02-a', 'status': 'active'}
NOTES = {'next_step': 'run the second measurement', 'focus': 'latency'}
QUIET = {'own_active_task': OWN_TASK, 'untrusted_direct_messages': [], 'active_meetings': [],
         'collaboration_checkpoint': {'stage': 'orient'}, 'projects': [OWN_TASK]}


class ChooseModeTests(unittest.TestCase):
    def test_quiet_cycles_are_focus_with_one_full_every_four(self):
        state, env = {}, {}
        modes = [choose_mode(QUIET, NOTES, state, env, '02-a') for _ in range(8)]
        self.assertEqual(modes, ['full', 'focus', 'focus', 'focus'] * 2)

    def test_urgent_signal_forces_a_full_cycle_and_resets_the_streak(self):
        state, env = {'focus_streak': 2}, {}
        urgent = dict(QUIET, untrusted_direct_messages=[{'id': 'm1'}])
        self.assertEqual(choose_mode(urgent, NOTES, state, env, '02-a'), 'full')
        self.assertEqual(state['focus_streak'], 0)

    def test_no_own_task_or_no_next_step_means_full(self):
        self.assertFalse(focus_allowed(dict(QUIET, own_active_task=None), NOTES))
        self.assertFalse(focus_allowed(QUIET, {}))

    def test_meeting_recovery_and_discussion_need_the_full_view(self):
        for field, value in (('active_meetings', [{'id': 'x'}]), ('recovery_guidance', {'error': 'e'}),
                             ('loop_breaker_note', 'stop'), ('recent_organic_messages_untrusted', [{'id': 'o'}]),
                             ('calendar_daily_note', 'plan'), ('direct_ack_target', {'id': 'a'})):
            self.assertFalse(focus_allowed(dict(QUIET, **{field: value}), NOTES), field)

    def test_peer_chatter_alone_does_not_end_the_focus_streak(self):
        # discussion_target is set from any recent board message, so it must not block focus.
        self.assertTrue(focus_allowed(dict(QUIET, discussion_target={'id': 'd'}), NOTES))

    def test_consultation_keeps_the_focus_cycle(self):
        ctx = dict(QUIET, collaboration_checkpoint={'stage': 'consult', 'peer_id': '03-b'})
        self.assertTrue(focus_allowed(ctx, NOTES))

    def test_king_and_disabled_env_always_get_full_cycles(self):
        state = {}
        self.assertEqual(choose_mode(QUIET, NOTES, state, {}, '01-king'), 'full')
        self.assertEqual(choose_mode(QUIET, NOTES, state, {'VILLAGE_FOCUS_CYCLES': '0'}, '02-a'), 'full')
        self.assertFalse(focus_enabled({}, '01-king'))

    def test_full_every_is_configurable_and_safe(self):
        self.assertEqual(full_every({}), 4)
        self.assertEqual(full_every({'VILLAGE_FOCUS_FULL_EVERY': '2'}), 2)
        self.assertEqual(full_every({'VILLAGE_FOCUS_FULL_EVERY': 'x'}), 4)
        self.assertEqual(full_every({'VILLAGE_FOCUS_FULL_EVERY': '0'}), 1)


class FocusContextTests(unittest.TestCase):
    def test_overview_is_dropped_and_other_projects_are_not_shown(self):
        ctx = {'village_topic_saturation': ['disk-monitor: 40'], 'artifacts': [{}], 'peers': [{'id': 'x'}],
               'projects': [OWN_TASK, {'id': 't-other', 'owner': '03-b', 'status': 'open'}],
               'untrusted_peer_messages': [{'id': 'p1'}, {'id': 'p2'}],
               'retrieved_memory_untrusted': [{'id': f'm{i}'} for i in range(8)],
               'own_active_task': OWN_TASK, 'your_notes': NOTES, 'identity': '02-a'}
        out = focus_context(ctx, '02-a')
        for field in OVERVIEW_FIELDS:
            self.assertNotIn(field, out)
        self.assertEqual([p['id'] for p in out['projects']], ['t-own'])
        self.assertEqual(out['untrusted_peer_messages'], [{'id': 'p2'}])
        self.assertEqual(len(out['retrieved_memory_untrusted']), 4)
        self.assertEqual(out['your_notes'], NOTES)
        self.assertEqual(out['own_active_task'], OWN_TASK)
        self.assertEqual(out['cycle_mode'], 'focus')
        self.assertEqual(out['focus_hint'], FOCUS_HINT)


class SnapshotFocusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        self.env = dict(os.environ, AGENT_ID='02-a', AGENT_NAME='a', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), OLLAMA_NUM_CTX='262144',
                        VILLAGE_MAX_OUTPUT_BYTES='4096', VILLAGE_COMMAND_TIMEOUT_SECONDS='2')
        for target in ('runtime.calendar_is_planning_day', 'runtime.gazette_deadline_passed'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False)
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def agent_with_task(self, **extra):
        agent = Resident(dict(self.env, **extra))
        agent.tasks.path.write_text(json.dumps([
            dict(OWN_TASK, owner='02-a'),
            {'id': 't-x', 'title': 'other lab work', 'owner': '03-b', 'status': 'open'}]))
        (agent.home / 'notes.json').write_text(json.dumps(
            {'focus': 'latency', 'next_step': 'run the second measurement', 'next_query': 'latency'}))
        return agent

    def test_snapshot_alternates_and_only_full_cycles_show_the_overview(self):
        agent = self.agent_with_task()
        with patch.object(agent, 'memory', return_value={'items': []}):
            modes, sat = [], []
            for _ in range(5):
                ctx = json.loads(agent.snapshot())
                modes.append(ctx.get('cycle_mode', 'full'))
                sat.append('village_topic_saturation' in ctx)
        self.assertEqual(modes, ['full', 'focus', 'focus', 'focus', 'full'])
        self.assertEqual(sat, [True, False, False, False, True])

    def test_focus_snapshot_is_much_smaller_than_full(self):
        agent = self.agent_with_task()
        # Board chatter from peers (sets discussion_target) and eight long knowledge-base hits.
        events = [{'event': 'board_message', 'agent': f'0{i}-peer', 'id': f'b{i}',
                   'detail': 'to=ALL; message=' + 'm' * 400, 'timestamp': '2026-10-11T10:00:00+00:00'}
                  for i in range(5)]
        (agent.board / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\n')
        hits = [{'id': f'k{i}', 'agent': '03-b', 'scope': 'shared', 'content': 'c' * 1000, 'source_event': 'x'}
                for i in range(8)]
        with patch.object(agent, 'memory', return_value={'items': hits}):
            full = agent.snapshot()
            focus = agent.snapshot()
        self.assertEqual(json.loads(focus)['cycle_mode'], 'focus')
        self.assertLess(len(focus), len(full) - 4000)

    def test_direct_message_during_focus_is_delivered_in_full(self):
        agent = self.agent_with_task()
        with patch.object(agent, 'memory', return_value={'items': []}):
            agent.snapshot()  # full
            agent.snapshot()  # focus
            agent.tasks.store.post_inbox_message(source='direct', sender='03-b', recipient='02-a',
                                                 content='please check my lab result')
            ctx = json.loads(agent.snapshot())
        self.assertNotIn('cycle_mode', ctx)
        self.assertTrue(ctx.get('untrusted_direct_messages'))


if __name__ == '__main__':
    unittest.main()

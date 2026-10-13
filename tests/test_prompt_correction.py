"""P158: the prompt never asks for an action the cycle does not offer; calendar and stale notes shrink."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from village.actions import ACTION_SPECS
from village.preflight import CALENDAR_UPCOMING, REPEAT_EVERY, STALE_AFTER, correct, inspect
from web.runtime import Resident

LOOP_BREAKER = ['idle', 'gazette_operation', 'calendar_operation']  # live 06-operator, 2026-10-13


def live_context():
    """The seven contradicting sections stage 1 measured for 06-operator under the loop breaker."""
    return {
        'direct_ack_target': {'id': 'msg_aaaaaaaaaaaa', 'agent': '02-interpreter', 'detail': 'to=06-operator; message=Can you check the log?'},
        'school_lesson': 'Today: answer with school_operation answer lesson=...',
        'recovery_guidance': {'failed_action': 'task_operation', 'error': 'task t1 not found',
                              'directive': 'Previous action did not succeed. Formulate a revised hypothesis ...'},
        'last_action_feedback': {'action': 'task_operation', 'ok': False, 'result': 'task t1 not found'},
        'your_model_gaps': {'model': 'nemotron-3-nano:4b', 'gaps': [{'signature': 'observed:arguments_not_object'}],
                            'hint': 'Report skills it lacks with capability_operation; store facts with memory_remember.'},
        'your_repeated_mistakes': [
            {'category': 'action_unavailable', 'count': 9, 'last_problem': 'sent meeting_operation without an open meeting',
             'last_solution': 'Use meeting_operation only while a meeting is open.'},
            {'category': 'format_violation', 'count': 4, 'last_problem': 'arguments as text', 'last_solution': 'Send idle.'}],
        'untrusted_peer_messages': [{'detail': 'please use board_message and execute_bash'}],
    }


class CorrectionTests(unittest.TestCase):
    def test_no_hint_asks_for_an_action_that_is_not_offered(self):
        context = live_context()
        fixes, _ = correct(context, LOOP_BREAKER, ACTION_SPECS, minute=600)
        report, _ = inspect({}, json.dumps(context), LOOP_BREAKER, ACTION_SPECS)
        self.assertEqual(report['contradictions'], [])  # facts (last result, failed action) stay, unflagged
        self.assertEqual({f['section'] for f in fixes}, {'direct_ack_target', 'school_lesson', 'recovery_guidance',
                                                          'your_model_gaps', 'your_repeated_mistakes'})

    def test_facts_and_peer_text_are_never_rewritten(self):
        context = live_context()
        correct(context, LOOP_BREAKER, ACTION_SPECS, minute=600)
        original = live_context()
        for key in ('last_action_feedback', 'untrusted_peer_messages'):
            self.assertEqual(context[key], original[key])
        self.assertEqual(context['recovery_guidance']['error'], 'task t1 not found')
        self.assertEqual(context['your_model_gaps']['gaps'], original['your_model_gaps']['gaps'])
        self.assertEqual(context['your_repeated_mistakes'][0]['count'], 9)
        self.assertIn('not offered this cycle', context['your_repeated_mistakes'][0]['last_solution'])
        self.assertEqual(context['your_repeated_mistakes'][1], original['your_repeated_mistakes'][1])
        self.assertIn('calendar_operation', context['recovery_guidance']['directive'])

    def test_nothing_changes_when_everything_is_offered(self):
        context = live_context()
        fixes, _ = correct(context, ACTION_SPECS, ACTION_SPECS, minute=600)
        self.assertEqual((fixes, context), ([], live_context()))

    def test_calendar_keeps_only_the_next_entries(self):
        events = [{'id': f'cal_{h:02d}', 'title': f'slot {h}', 'kind': 'focus', 'start_time': f'{h:02d}:00',
                   'duration_minutes': 60, 'status': 'planned', 'organizer': 'a', 'notes': 'x' * 900, 'series_id': 's',
                   'created_at': 't', 'updated_at': 't',
                   'attendees': [{'event_id': f'cal_{h:02d}', 'agent_id': 'a', 'response': 'accepted', 'updated_at': 't'}]}
                  for h in range(24)]
        context = {'calendar_today_untrusted': events}
        fixes, _ = correct(context, ACTION_SPECS, ACTION_SPECS, minute=10 * 60 + 30)
        kept = context['calendar_today_untrusted']
        self.assertEqual([e['id'] for e in kept], ['cal_10', 'cal_11', 'cal_12', 'cal_13', 'cal_14'][:CALENDAR_UPCOMING])
        self.assertNotIn('attendees', kept[0])  # organizer only
        self.assertLess(len(kept[0]['notes']), 200)
        self.assertIn('19 of today', context['calendar_today_omitted'])
        self.assertGreater(fixes[0]['tokens'], 5000)

    def test_a_past_day_leaves_no_calendar_section(self):
        context = {'calendar_today_untrusted': [{'id': 'c', 'start_time': '08:00', 'duration_minutes': 15}]}
        correct(context, ['idle'], ACTION_SPECS, minute=23 * 60)
        self.assertNotIn('calendar_today_untrusted', context)
        self.assertNotIn('calendar_operation', context['calendar_today_omitted'])

    def test_an_unchanged_recurring_note_is_shown_less_often(self):
        state, shown = None, []
        for _ in range(STALE_AFTER + 2 * REPEAT_EVERY):
            context = {'notes_hint': 'Your notes are empty.', 'calendar_daily_note': 'You have not touched your calendar.'}
            _, state = correct(context, ACTION_SPECS, ACTION_SPECS, minute=0, previous=state)
            shown.append('notes_hint' in context)
            self.assertIn('calendar_daily_note', context)  # carries the planning gate: always shown
        self.assertEqual(shown, [True] * STALE_AFTER + [False] * (REPEAT_EVERY - 1) + [True] + [False] * (REPEAT_EVERY - 1) + [True])

    def test_a_changed_note_is_shown_again_at_once(self):
        state = None
        for _ in range(STALE_AFTER + 1):
            _, state = correct({'notes_hint': 'old'}, ACTION_SPECS, ACTION_SPECS, minute=0, previous=state)
        context = {'notes_hint': 'new'}
        correct(context, ACTION_SPECS, ACTION_SPECS, minute=0, previous=state)
        self.assertEqual(context['notes_hint'], 'new')


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='village-correction-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for d in ('board', 'telemetry'):
            (self.tmp / d).mkdir()
        identity = self.tmp / 'identity.txt'
        identity.write_text('Test Identity')
        self.agent = Resident(env={
            'AGENT_ID': '06-operator', 'AGENT_NAME': 'operator', 'AGENT_ROLE': 'operator', 'VILLAGE_ROOT': str(self.tmp),
            'OLLAMA_MODEL': 'test-model', 'OLLAMA_URL': 'http://127.0.0.1:9999', 'AGENT_IDENTITY_PROMPT': str(identity),
            'VILLAGE_PAUSE_MARKER': str(self.tmp / 'paused'), 'OLLAMA_NUM_CTX': '16384'})

    def test_corrections_go_to_telemetry_only(self):
        self.agent.effective_allowed_actions = lambda: list(LOOP_BREAKER)
        context = live_context()
        self.agent.correct_prompt(context)
        self.assertIsNone(context['direct_ack_target'])
        self.assertIn('prompt_corrected', (self.tmp / 'telemetry' / 'agent-events.jsonl').read_text())
        board = self.tmp / 'board' / 'events.jsonl'
        self.assertNotIn('prompt_corrected', board.read_text() if board.exists() else '')

    def test_a_correction_error_never_costs_a_cycle(self):
        self.agent.effective_allowed_actions = lambda: (_ for _ in ()).throw(RuntimeError('boom'))
        context = live_context()
        self.agent.correct_prompt(context)
        self.assertEqual(context, live_context())
        self.assertIn('prompt_correction_error', (self.tmp / 'telemetry' / 'agent-events.jsonl').read_text())

    def test_the_loop_breaker_note_names_only_offered_actions(self):
        self.agent.state['invalid_streak'] = 99
        snapshot = json.loads(self.agent.snapshot())
        note = snapshot['loop_breaker_note']
        # no open meeting: P126/P135 hide meeting_operation, so neither it nor its example is named
        self.assertNotIn('meeting_operation', note)
        self.assertNotIn("open meeting's own report", note)
        offered = self.agent.effective_allowed_actions()
        self.assertIn(f"This cycle only accepts: {', '.join(offered)}.", note)


if __name__ == '__main__':
    unittest.main()

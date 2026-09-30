import json
import io
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
from runtime import Resident,Tasks,resource_snapshot,tail,event_time,COLLABORATION_PRESSURE_CEILING,MEETING_REPORT_CEILING,GAZETTE_REVIEW_CEILING,GAZETTE_CLOSE_CEILING,GAZETTE_CLOSE_MIN_HOURS,GAZETTE_CONTRIBUTE_CEILING,GAZETTE_GAME_RESULT_CEILING,GAZETTE_GAME_WINNER_CEILING,CALENDAR_PLAN_CEILING
from village.gazette import REVIEWER_AGENT
from decision import decision
from village.collaboration import CooperationCheckpoint


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        (self.root/'board').mkdir(); (self.root/'telemetry').mkdir()
        self.env=dict(os.environ,AGENT_ID='01-a',AGENT_NAME='a',AGENT_ROLE='resident',VILLAGE_ROOT=str(self.root),
                      VILLAGE_MAX_OUTPUT_BYTES='4096',VILLAGE_COMMAND_TIMEOUT_SECONDS='2')
        # P75: the calendar plan gate keys off the real wall-clock weekday
        # (calendar_is_workday(calendar_today())) - defaulting it to
        # "weekend" here keeps every pre-existing test in this class (most
        # of which loop 10+ guarded actions for unrelated gates) immune to
        # it regardless of which real day the suite happens to run on;
        # calendar-specific tests below explicitly patch it back to True.
        self._calendar_workday_patch = patch('runtime.calendar_is_workday', return_value=False)
        self._calendar_workday_patch.start()
        self.addCleanup(self._calendar_workday_patch.stop)
        self.agent=Resident(self.env)
    def tearDown(self): self.tmp.cleanup()
    def execute(self,name,**args): self.agent.execute({'tool_call':{'name':name,'arguments':args}})

    def test_result_survives_restart_and_pipeline_fails(self):
        self.execute('execute_bash',command='printf observable-result; false | true')
        again=Resident(self.env)
        self.assertFalse(again.state['last_result']['ok'])
        self.assertIn('observable-result',again.state['last_result']['result'])

    def test_no_permanent_failure_deadlock(self):
        for _ in range(3): self.execute('execute_bash',command='false')
        self.assertIn('Repeated action blocked',self.agent.state['last_result']['result'])
        self.execute('idle')
        self.execute('execute_bash',command='false')
        self.assertIn('Repeated action blocked',self.agent.state['last_result']['result'])
        self.execute('execute_bash',command='printf recovered')
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_bounded_output_and_timeout(self):
        self.execute('execute_bash',command="head -c 50000 /dev/zero; sleep 10")
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertLessEqual((self.agent.home/'last-command.log').stat().st_size,4200)

    def test_tasks_claim_and_evidence(self):
        tasks=Tasks(self.root/'board')
        item=tasks.operate('a',dict(action='create',title='test',success_criterion='reproduce output'))
        self.assertEqual(tasks.path.stat().st_mode&0o777,0o660)
        tasks.operate('a',dict(action='claim',task_id=item['id']))
        with self.assertRaises(ValueError): tasks.operate('b',dict(action='claim',task_id=item['id']))
        with self.assertRaises(ValueError): tasks.operate('a',dict(action='complete',task_id=item['id']))
        result=tasks.operate('a',dict(action='complete',task_id=item['id'],evidence='artifact:test'))
        self.assertFalse(result['completion_verified'])

    def test_invalid_output_not_broadcast_as_peer_instruction(self):
        self.agent.execute(decision({'message':{'content':'{"name":'}}))
        self.assertEqual(tail(self.root/'board/events.jsonl')[0]['event'],'invalid_decision')
        self.assertEqual(self.agent.state['invalid_streak'],1)

    def test_natural_language_fallback_is_board_observation(self):
        self.agent.execute(decision({'message':{'content':'I inspected the habitat and found no reusable artifact yet.'}}))
        events=tail(self.root/'board/events.jsonl')
        self.assertTrue(any(e.get('event') == 'board_message' for e in events))

    def test_consult_checkpoint_rejects_broadcast(self):
        self.agent.current_collaboration_checkpoint = CooperationCheckpoint(
            'consult', 'board_message', 'ask the named peer', peer_id='02-b'
        )
        self.agent.execute({'tool_call': {'name': 'board_message', 'arguments': {'recipient': 'ALL', 'message': 'broadcast'}}})
        events = tail(self.root / 'board/events.jsonl')
        self.assertEqual(self.agent.state['last_result']['ok'], False)
        self.assertIn('Named peer consultation required', self.agent.state['last_result']['result'])
        self.assertTrue(any(e.get('event') == 'collaboration_gate' for e in events))

    def test_collaboration_pressure_ceiling_eventually_gates_any_action(self):
        # P51 regression: the gate previously fired only for a fixed list of
        # mutating actions (execute_bash/start_job/task_operation/team_operation),
        # so an agent that only ever chose something outside that list (e.g.
        # idle, memory_search) could ignore a 'consult' checkpoint forever -
        # pressure climbed with zero effect (observed live: reached 41 and
        # still counting, see docs/evidence/P51.md). Past
        # COLLABORATION_PRESSURE_CEILING it must gate ANY solo action.
        self.agent.current_collaboration_checkpoint = CooperationCheckpoint(
            'consult', 'board_message', 'ask the named peer', peer_id='02-b'
        )
        for _ in range(COLLABORATION_PRESSURE_CEILING - 1):
            self.execute('idle')
        self.assertTrue(self.agent.state['last_result']['ok'])  # not yet gated
        self.execute('idle')
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('Collaboration checkpoint required', self.agent.state['last_result']['result'])

    def test_meeting_report_nudge_eventually_gates_other_actions(self):
        # P56: the exact same unbounded-nudge gap COLLABORATION_PRESSURE_CEILING
        # (P51) closed for checkpoints, but never applied here - an agent
        # could see "Meeting report requested" on every single cycle forever
        # with zero consequence (observed live: 09-chronicler ignored it for
        # 20+ minutes straight, sending other messages instead each time -
        # and because it was his real blocker, the Gazette P55 editorial
        # review inherited the same unbounded drift). Same fix, same ceiling.
        # idle is explicitly excluded from this check (same as
        # meeting_operation itself), so it never triggers the nudge -
        # execute_bash is the repeated, otherwise-harmless action here.
        # Varied per call: an identical command would hit the unrelated
        # repeated-action guard (limit 2) long before this ceiling.
        self.agent.meetings.schedule('jour_fixe', 'status update', '2026-09-24T10:00:00Z', meeting_id='m1')
        for i in range(MEETING_REPORT_CEILING - 1):
            self.execute('execute_bash', command=f'printf ok{i}')
        self.assertTrue(self.agent.state['last_result']['ok'])  # not yet gated
        self.execute('execute_bash', command=f'printf ok{MEETING_REPORT_CEILING}')
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('Meeting report required', self.agent.state['last_result']['result'])

    def test_meeting_gate_message_includes_a_copyable_json_example(self):
        # P58: naming the required fields in prose was not enough - live
        # observation showed 09-chronicler producing well-formed JSON for
        # OTHER actions past this exact gate (12+ consecutive blocks), never
        # switching to meeting_operation. Same lever already proven for
        # format_violation (VALID_ENVELOPE_EXAMPLE): a literal, fillable JSON
        # example instead of a field-name description.
        self.agent.meetings.schedule('jour_fixe', 'status update', '2026-09-24T10:00:00Z', meeting_id='m1')
        for i in range(MEETING_REPORT_CEILING):
            self.execute('execute_bash', command=f'printf ok{i}')
        result = self.agent.state['last_result']['result']
        self.assertIn('"name":"meeting_operation"', result)
        self.assertIn('"operation":"report"', result)
        self.assertIn('"meeting_id":"m1"', result)

    def test_meeting_operation_itself_is_never_gated_by_its_own_nudge(self):
        self.agent.meetings.schedule('jour_fixe', 'status update', '2026-09-24T10:00:00Z', meeting_id='m1')
        for i in range(MEETING_REPORT_CEILING + 5):
            self.execute('execute_bash', command=f'printf ok{i}')
        self.execute('meeting_operation', operation='report', meeting_id='m1',
                      achieved='x', evidence='y', next_step='z', blockers='')
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_meeting_nudge_stops_once_the_report_is_submitted(self):
        self.agent.meetings.schedule('jour_fixe', 'status update', '2026-09-24T10:00:00Z', meeting_id='m1')
        self.execute('meeting_operation', operation='report', meeting_id='m1',
                      achieved='x', evidence='y', next_step='z', blockers='')
        events_before = len(tail(self.root/'board/events.jsonl'))
        self.execute('idle')
        events_after = tail(self.root/'board/events.jsonl')
        self.assertFalse(any(e.get('event') == 'meeting_required' for e in events_after[events_before:]))
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_resource_snapshot_is_locale_independent(self):
        with patch.dict(os.environ,{'LANG':'de_DE.UTF-8'}):
            value=resource_snapshot(self.root)
        self.assertGreater(value['ram_available_mib'],0)
        self.assertLessEqual(value['ram_available_mib'],value['ram_total_mib'])

    def test_partial_log_does_not_erase_valid_events(self):
        path=self.root/'events'; path.write_text('{broken\n{"event":"ok"}\n')
        self.assertEqual(tail(path),[{'event':'ok'}])

    def test_timestamp_order_handles_legacy_timezones(self):
        self.assertLess(event_time({'timestamp':'2026-09-24T17:00:00+02:00'}),
                        event_time({'timestamp':'2026-09-24T15:01:00+00:00'}))

    def test_secret_redaction(self):
        self.agent.env['MEMORY_AGENT_TOKEN']='very-private'
        self.execute('execute_bash',command='printf harmless')
        self.agent.event('test','very-private')
        self.assertNotIn('very-private',(self.root/'board/events.jsonl').read_text())

    def test_task_envelope_is_supported(self):
        obj={'name':'task_operation','arguments':{'action':'claim','task_id':'123'}}
        parsed=decision({'message':{'content':json.dumps(obj)}})
        self.assertNotIn('fallback_reason',parsed)

    def test_addressed_messages_survive_oversized_projects_trim(self):
        # P48 regression: 'projects' alone can dwarf the entire character budget
        # (observed live: 01-king had 14 real tasks totalling ~13.8k chars against
        # a ~6.4k budget at the default OLLAMA_NUM_CTX=8192). 'projects' used to be
        # the most-protected field (trimmed last), so a message addressed directly
        # to the agent - organic (operator) and direct (peer) alike - was silently
        # wiped out first while the bulky, re-derivable project snapshot sat
        # completely untouched. Both must now survive; 'projects' absorbs the cut.
        # P76: the ever-growing always-present 'tools' dict has pushed the
        # non-'projects' baseline overhead high enough that, at the
        # original default 8192, even fully emptying 'projects' no longer
        # leaves room for a single organic/direct message - a real budget
        # squeeze, but not what this test is about (trim PRIORITY order,
        # which still needs 'projects' to require trimming at all). A
        # modest bump restores that without letting the full 14k-char
        # 'projects' list fit untrimmed.
        agent = Resident(dict(self.env, OLLAMA_NUM_CTX='9216'))
        big_projects = [{'id': str(i), 'title': 'x' * 700, 'owner': '01-a', 'status': 'open'} for i in range(20)]
        agent.tasks.path.write_text(json.dumps(big_projects))
        (self.root/'board/organic-inbox.jsonl').write_text(
            json.dumps({'timestamp': '2026-09-24T11:00:00Z', 'message': 'Operator: please respond.'}) + '\n')
        agent.tasks.store.post_inbox_message(
            source='direct', sender='02-b', recipient='01-a', content='Please respond directly.')
        with patch.object(agent, 'memory', return_value={'items': []}):
            ctx = json.loads(agent.snapshot())
        self.assertLess(len(ctx['projects']), 20)  # still had to trim
        self.assertEqual(len(ctx['recent_organic_messages_untrusted']), 1, ctx.get('recent_organic_messages_untrusted'))
        self.assertEqual(len(ctx['untrusted_direct_messages']), 1, ctx.get('untrusted_direct_messages'))

    def test_projects_trim_drops_the_tail_not_the_agents_own_active_task(self):
        # Found via P55's added action/hint text finally crossing this
        # test's budget threshold: the trim loop popped index 0 on every
        # field uniformly, including 'projects' - but projects is
        # pre-sorted highest-priority-first (own active task first, see
        # task_priority()), so that discarded the single most important
        # row before any of the bulky, genuinely stale ones. Must trim
        # from the tail for this field specifically.
        own_active = {'id': 'own-active', 'title': 'x', 'owner': '01-a', 'status': 'active'}
        stale = [{'id': f'stale-{i}', 'title': 'x' * 700, 'owner': '02-b', 'status': 'open'} for i in range(20)]
        self.agent.tasks.path.write_text(json.dumps([own_active] + stale))
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertLess(len(ctx['projects']), 21)  # trimming did happen
        self.assertIn('own-active', [p['id'] for p in ctx['projects']])

    def test_organic_cursor_does_not_advance_past_a_trimmed_out_message(self):
        # P48 regression: the cursor used to advance to cover every tailed organic
        # entry regardless of whether it survived context-budget trimming, so a
        # message dropped for budget reasons was marked permanently "seen" and
        # never retried. A single message too large to ever fit must leave the
        # cursor unchanged so the next cycle gets another chance at it.
        huge_message = 'x' * 20000
        (self.root/'board/organic-inbox.jsonl').write_text(
            json.dumps({'timestamp': '2026-09-24T11:00:00Z', 'message': huge_message}) + '\n')
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertEqual(ctx['recent_organic_messages_untrusted'], [])
        self.assertEqual(self.agent.pending_organic_cursor, self.agent.state.get('seen_organic_epoch', 0))

    def test_gazette_open_accepts_an_explicit_edition_id(self):
        # P71: operator-directed out-of-band edition, e.g. to open tomorrow's
        # edition ahead of the real calendar rollover - open() now takes an
        # explicit edition_id the same way assign/contribute/review/close
        # already do, rather than needing a store-level bypass of execute().
        king_env = dict(self.env, AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        king.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {
            'operation': 'open', 'edition_id': '2099-01-01'}}})
        self.assertTrue(king.state['last_result']['ok'])
        self.assertIsNotNone(king.gazette.get_edition('2099-01-01'))

    def test_king_sees_a_daily_gazette_hint_until_opened_announced_and_assigned(self):
        # Gazette Stufe 2/3 (P48/P49/P54): a one-off nudge proved unreliable
        # in live observation (delivered, acknowledged, never acted on -
        # P48.md). "Open" alone was not enough either: King opened a live
        # edition and then simply moved on without telling anyone, leaving 0
        # contributions (P49). Announcing alone still was not enough: a
        # generic "pick any kind" invitation to everyone also produced 0
        # contributions after ~30 minutes (P52/P53.md) - real delegation is
        # King's own third, separate action (operation=assign), so the hint
        # must survive open AND announce too, with a third piece of guidance.
        king_env = dict(self.env, AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        with patch.object(king, 'memory', return_value={'items': []}):
            before = json.loads(king.snapshot())
        self.assertIn('gazette_daily_note', before)
        king.gazette.open_edition('01-king', ['02-b', '03-c'])
        with patch.object(king, 'memory', return_value={'items': []}):
            opened_not_announced = json.loads(king.snapshot())
        self.assertIn('gazette_daily_note', opened_not_announced)
        self.assertIn('not yet told the village', opened_not_announced['gazette_daily_note'])
        king.execute({'tool_call': {'name': 'board_message',
                                     'arguments': {'message': 'The AI Village Gazette is open today, please contribute!', 'recipient': 'ALL'}}})
        with patch.object(king, 'memory', return_value={'items': []}):
            announced_not_assigned = json.loads(king.snapshot())
        self.assertIn('gazette_daily_note', announced_not_assigned)
        self.assertIn('not yet delegated', announced_not_assigned['gazette_daily_note'])
        king.gazette.assign_kinds(king.gazette.list_editions(1)[0]['id'], '01-king', ['02-b', '03-c'])
        with patch.object(king, 'memory', return_value={'items': []}):
            after = json.loads(king.snapshot())
        # P67: King himself is entirely inside this branch, so the generic
        # contributor hint below (the `else:` branch) never reached him -
        # he was never once prompted to submit his own assigned piece
        # (live evidence: assigned 'learning', never contributed it). The
        # hint must now survive open+announce+assign too, until King has
        # submitted his own contribution.
        self.assertIn('gazette_daily_note', after)
        self.assertIn('you have not yet submitted your own', after['gazette_daily_note'])
        edition_id = king.gazette.list_editions(1)[0]['id']
        assigned_kind = king.gazette.get_assignment(edition_id, '01-king')
        king.gazette.submit_contribution(edition_id, '01-king', assigned_kind, "Update: see full text." , 'King contributed too.')
        with patch.object(king, 'memory', return_value={'items': []}):
            fully_done = json.loads(king.snapshot())
        # King is also REVIEWER_AGENT (P61), so his own fresh submission is
        # immediately a pending review - reviewer_pending has top priority,
        # so the hint correctly switches to that rather than disappearing.
        self.assertIn('gazette_daily_note', fully_done)
        self.assertIn('editorial review', fully_done['gazette_daily_note'])
        king.gazette.review_contribution(edition_id, '01-king', assigned_kind, '01-king', 'approve')
        with patch.object(king, 'memory', return_value={'items': []}):
            reviewed = json.loads(king.snapshot())
        # P68: 1 of 3 assigned residents (King, 02-b, 03-c) is below the
        # 50% participation floor, and no time has passed - not yet
        # closable, so no close hint should appear yet (King's own branch
        # falls through to nothing further, correctly).
        self.assertNotIn('gazette_daily_note', reviewed)
        peer_kind = king.gazette.get_assignment(edition_id, '02-b')
        king.gazette.submit_contribution(edition_id, '02-b', peer_kind, "Update: see full text." , 'A peer contributed too.')
        king.gazette.review_contribution(edition_id, '02-b', peer_kind, '01-king', 'approve')
        with patch.object(king, 'memory', return_value={'items': []}):
            enough_participation = json.loads(king.snapshot())
        # Now 2 of 3 (>=50%) have contributed - closable, cascades to the
        # close hint instead of disappearing.
        self.assertIn('gazette_daily_note', enough_participation)
        self.assertIn('operation=close', enough_participation['gazette_daily_note'])
        king.gazette.close_edition(edition_id, '01-king')
        with patch.object(king, 'memory', return_value={'items': []}):
            all_clear = json.loads(king.snapshot())
        self.assertNotIn('gazette_daily_note', all_clear)

    def test_king_own_hint_survives_an_out_of_band_edition_id(self):
        # P71: King's own open/announce/assign hint chain had the identical
        # gazette_today()-only blindness as the ordinary contributor hint.
        from runtime import gazette_today
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        future_id = '2099-01-01'
        self.assertNotEqual(future_id, gazette_today())
        king.gazette.open_edition('01-king', ['01-a', '02-b'], edition_id=future_id)
        with patch.object(king, 'memory', return_value={'items': []}):
            ctx = json.loads(king.snapshot())
        self.assertIn('gazette_daily_note', ctx)
        self.assertIn('not yet told the village', ctx['gazette_daily_note'])

    def test_non_king_sees_no_gazette_hint_before_an_edition_exists(self):
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertNotIn('gazette_daily_note', ctx)

    def test_non_king_gazette_hint_until_contributed(self):
        # A one-off village-wide King announcement has the identical problem
        # a direct nudge to King had (P48/P49): it competes with each
        # resident's own ongoing work and loses - live observation: all 9
        # residents active, each on their own project, 0 contributions hours
        # after a correct announcement. Every resident who has not yet
        # contributed today gets the same always-visible hint King has.
        edition = self.agent.gazette.open_edition('01-king', ['01-a', '02-b', '03-c'])
        # P74: open_edition()'s random game-pair draw would sometimes (2/3
        # of the time, unseeded) include '01-a' - the separate, still-open
        # game_result obligation would then keep gazette_daily_note set
        # after this test's own contribution, unrelated to what it actually
        # verifies (the regular per-resident hint clearing once fulfilled).
        with self.agent.gazette._conn() as c:
            c.execute("UPDATE gazette_editions SET game_pair='' WHERE id=?", (edition['id'],))
            c.commit()
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            before = json.loads(self.agent.snapshot())
        self.assertIn('gazette_daily_note', before)
        self.agent.gazette.submit_contribution(self.agent.gazette.list_editions(1)[0]['id'], '01-a', 'mood', "Update: see full text." , 'Feeling productive today.')
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            after = json.loads(self.agent.snapshot())
        self.assertNotIn('gazette_daily_note', after)

    def test_non_king_gazette_hint_names_the_specific_delegated_kind(self):
        # P53/P54 escalation: the generic "pick any kind" hint (P52) produced
        # 0 contributions after ~30 minutes despite confirmed delivery - real
        # delegation (King's own separate assign action) names the specific
        # kind he assigned this resident, not an open choice.
        self.agent.gazette.open_edition('01-king', ['01-a', '02-b', '03-c'])
        edition_id = self.agent.gazette.list_editions(1)[0]['id']
        assignments = self.agent.gazette.assign_kinds(edition_id, '01-king', ['01-a', '02-b', '03-c'])
        assigned_kind = assignments['01-a']
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertIn(f"kind='{assigned_kind}'", ctx['gazette_daily_note'])

    def test_non_king_gazette_hint_and_contribute_survive_an_out_of_band_edition_id(self):
        # P71 (operator directive, "jetzt eine neue Ausgabe anstossen"):
        # opening an edition under an id other than gazette_today() (the
        # normal case right after a real day rollover, or an explicitly
        # out-of-band edition) used to leave every ordinary resident's
        # contributor hint blind - it only ever checked gazette_today(),
        # never "whatever edition is actually open". The same day-rollover
        # blindness P59-follow-up/P63 already fixed for the reviewer/close
        # hints, just never carried over here.
        from runtime import gazette_today
        future_id = '2099-01-01'
        self.assertNotEqual(future_id, gazette_today())
        self.agent.gazette.open_edition('01-king', ['01-a', '02-b', '03-c'], edition_id=future_id)
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertIn('gazette_daily_note', ctx)
        # Submitting without an explicit edition_id (as every existing hint
        # instructs) must land on the actually-open edition, not gazette_today().
        self.agent.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {
            'operation': 'contribute', 'kind': 'mood', 'headline': 'Update', 'content': 'Feeling productive today.'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])
        edition = self.agent.gazette.get_edition(future_id)
        self.assertTrue(any(c['agent'] == '01-a' and c['kind'] == 'mood' for c in edition['contributions']))

    def test_gazette_review_is_restricted_to_the_reviewer(self):
        # P55 (operator directive): "die Zeitung sollte nicht aus
        # ungeprueften Beitraegen bestehen, es braucht eine Redaktionelle
        # Pruefinstanz" - only REVIEWER_AGENT may approve/reject. (Originally
        # 09-chronicler; reassigned to 01-king by P60-follow-up after 32+
        # consecutive gate-blocks produced zero reviews - see REVIEWER_AGENT.)
        edition = self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        self.agent.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        self.execute('gazette_operation', operation='review', agent='01-a', kind='mood', decision='approve')
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn(f'Only {REVIEWER_AGENT}', self.agent.state['last_result']['result'])

    def test_gazette_close_is_restricted_to_king(self):
        self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        self.execute('gazette_operation', operation='close')
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('Only 01-king', self.agent.state['last_result']['result'])

    def test_reviewer_can_review_and_king_can_close_with_archive(self):
        # REVIEWER_AGENT is King himself (P60-follow-up reassignment), so
        # both roles happen to be the same resident here - review and close
        # remain two separate gazette_operation calls either way.
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a'])
        edition_id = edition['id']
        king.gazette.submit_contribution(edition_id, '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        king.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {
            'operation': 'review', 'agent': '01-a', 'kind': 'mood', 'decision': 'approve'}}})
        self.assertTrue(king.state['last_result']['ok'])
        king.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {'operation': 'close'}}})
        self.assertTrue(king.state['last_result']['ok'])
        archive_path = self.root / 'gazette' / 'archive' / edition_id / 'index.html'
        self.assertTrue(archive_path.exists())
        # P64: Path.mkdir()/write_text() inherit this process's umask, which
        # produced 0700/0600 (unreadable by anyone but the owner) on N06-M10 -
        # the dashboard (a different Unix user, group member) could not read
        # a single published edition. Every level must be group-readable.
        import stat
        for level in (self.root / 'gazette', self.root / 'gazette' / 'archive', archive_path.parent):
            self.assertTrue(stat.S_IMODE(level.stat().st_mode) & 0o050, f'{level} not group-readable/traversable')
        self.assertTrue(stat.S_IMODE(archive_path.stat().st_mode) & 0o040, 'archive file not group-readable')
        self.assertIn('Feeling good.', archive_path.read_text())
        # P66: PDF export archives alongside the HTML, same write-once and
        # group-readable guarantees.
        pdf_path = archive_path.parent / 'gazette.pdf'
        self.assertTrue(pdf_path.exists())
        self.assertTrue(pdf_path.read_bytes().startswith(b'%PDF-1.4'))
        self.assertIn(b'Feeling good.', pdf_path.read_bytes())
        self.assertTrue(stat.S_IMODE(pdf_path.stat().st_mode) & 0o040, 'PDF not group-readable')

    def test_chronicler_sees_pending_review_hint_until_cleared(self):
        # The review gate itself must not become the exact reliability
        # bottleneck this session spent P48-P54 fixing.
        chronicler_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        chronicler = Resident(chronicler_env)
        edition = chronicler.gazette.open_edition('01-king', ['01-a'])
        chronicler.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        with patch.object(chronicler, 'memory', return_value={'items': []}):
            before = json.loads(chronicler.snapshot())
        self.assertIn('gazette_daily_note', before)
        self.assertIn('editorial review', before['gazette_daily_note'])
        chronicler.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        with patch.object(chronicler, 'memory', return_value={'items': []}):
            after = json.loads(chronicler.snapshot())
        self.assertNotIn('editorial review', after.get('gazette_daily_note', ''))

    def test_chronicler_review_hint_survives_a_day_rollover(self):
        # P59-follow-up: scoping the review hint to gazette_today() went
        # blind the moment the calendar day rolled over - live observation:
        # yesterday's edition still had 3 pending reviews, but chronicler's
        # hint only ever checked today's (empty) edition, so it would have
        # silently stopped mentioning them forever. Review is an ongoing
        # obligation against whatever was submitted, not a daily assignment.
        #
        # Also exercises the P60-follow-up priority rule: REVIEWER_AGENT is
        # now 01-king, and "today" has no edition at all here - under the
        # old (King-branch-always-wins) structure this would have shown
        # King's own "open today's edition" hint instead, hiding the review
        # obligation. Pending reviews must win.
        chronicler_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        chronicler = Resident(chronicler_env)
        yesterday = chronicler.gazette.open_edition('01-king', ['01-a'], edition_id='2026-09-27')
        chronicler.gazette.submit_contribution(yesterday['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good yesterday.')
        # "Today" (gazette_today()) has no edition at all - the old code path
        # would find gazette_edition=None and never surface the stale review.
        with patch.object(chronicler, 'memory', return_value={'items': []}):
            ctx = json.loads(chronicler.snapshot())
        self.assertIn('gazette_daily_note', ctx)
        self.assertIn('2026-09-27/01-a/mood', ctx['gazette_daily_note'])
        self.assertNotIn('No AI Village Gazette edition is open for today', ctx['gazette_daily_note'])

    def test_gazette_review_gate_eventually_blocks_other_actions(self):
        # P60 (operator: "Nicht nur beobachten wenn du GAPs identifizierst,
        # sondern proaktiv loesen"): live observation showed the correctly-
        # delivered, cross-day-persistent review hint (P57/P59) produce zero
        # reviews over 40+ minutes with no gate behind it - the same
        # unbounded-advisory gap already closed for collaboration
        # checkpoints (P51) and meeting reports (P56/P58).
        chronicler_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        chronicler = Resident(chronicler_env)
        edition = chronicler.gazette.open_edition('01-king', ['01-a'])
        chronicler.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        for i in range(GAZETTE_REVIEW_CEILING - 1):
            chronicler.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertTrue(chronicler.state['last_result']['ok'])  # not yet gated
        chronicler.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{GAZETTE_REVIEW_CEILING}'}}})
        self.assertFalse(chronicler.state['last_result']['ok'])
        result = chronicler.state['last_result']['result']
        self.assertIn('Editorial review required', result)
        self.assertIn('"name":"gazette_operation"', result)
        self.assertIn('"operation":"review"', result)
        self.assertIn(f'"edition_id":"{edition["id"]}"', result)

    def test_gazette_operation_itself_is_never_gated_by_review_pressure(self):
        chronicler_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        chronicler = Resident(chronicler_env)
        edition = chronicler.gazette.open_edition('01-king', ['01-a'])
        chronicler.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        for i in range(GAZETTE_REVIEW_CEILING + 5):
            chronicler.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        chronicler.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {
            'operation': 'review', 'edition_id': edition['id'], 'agent': '01-a', 'kind': 'mood', 'decision': 'approve'}}})
        self.assertTrue(chronicler.state['last_result']['ok'])

    def test_gazette_review_gate_resets_once_nothing_pending(self):
        chronicler_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        chronicler = Resident(chronicler_env)
        edition = chronicler.gazette.open_edition('01-king', ['01-a'])
        chronicler.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        chronicler.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        for i in range(GAZETTE_REVIEW_CEILING + 5):
            chronicler.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        # P63: an edition with nothing left pending review is now itself a new
        # obligation (close it) with its own ceiling, so further solo work can
        # legitimately be gated again by THAT gate - the guarantee this test
        # protects is specifically that the review pressure counter (not the
        # unrelated close counter) actually resets to zero once resolved.
        self.assertEqual(chronicler.state.get('gazette_review_pressure', 0), 0)

    def test_gazette_closable_edition_surfaces_a_close_hint(self):
        # P63: live observation - once open/announce/assign were done and
        # every submitted contribution had been reviewed, nothing ever told
        # King to take the final gazette_operation close step. The edition
        # (2026-09-28 on N06-M10) sat fully reviewed and uncompiled with zero
        # pressure anywhere, since gazette_pending_reviews() was empty.
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a'])
        king.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        king.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        with patch.object(king, 'memory', return_value={'items': []}):
            ctx = json.loads(king.snapshot())
        self.assertIn('gazette_daily_note', ctx)
        self.assertIn('operation=close', ctx['gazette_daily_note'])
        self.assertIn(edition['id'], ctx['gazette_daily_note'])

    def test_gazette_not_closable_with_low_participation_and_no_time_elapsed(self):
        # P68 (operator feedback): the P63 gate had no floor - the very next
        # real edition closed after a single contribution from 1 of 9
        # assigned residents, 66 minutes after opening. Reproduces that
        # exact shape: 1 of 3 assigned residents contributed, edition just
        # opened - must not be closable yet.
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a', '02-b'])
        king.gazette.assign_kinds(edition['id'], '01-king', ['01-a', '02-b'])
        king.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        king.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        self.assertEqual(king.gazette_closable_editions(), [])

    def test_gazette_closable_once_half_of_assigned_residents_contributed(self):
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a', '02-b', '03-c'])
        king.gazette.assign_kinds(edition['id'], '01-king', ['01-a', '02-b', '03-c'])
        # 4 assigned (King + 3 peers): 2 contributors is the (4+1)//2==2 floor.
        king.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        king.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        self.assertEqual(king.gazette_closable_editions(), [])
        king.gazette.submit_contribution(edition['id'], '02-b', 'wishes', "Update: see full text." , 'More books please.')
        king.gazette.review_contribution(edition['id'], '02-b', 'wishes', REVIEWER_AGENT, 'approve')
        self.assertEqual(len(king.gazette_closable_editions()), 1)

    def test_gazette_closable_once_enough_time_has_passed_despite_low_participation(self):
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a', '02-b', '03-c'])
        king.gazette.assign_kinds(edition['id'], '01-king', ['01-a', '02-b', '03-c'])
        king.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        king.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        self.assertEqual(king.gazette_closable_editions(), [])  # only 1 of 4, no time elapsed
        old_timestamp = (datetime.now(timezone.utc) - timedelta(hours=GAZETTE_CLOSE_MIN_HOURS + 1)).isoformat()
        with king.gazette._conn() as c:
            c.execute("UPDATE gazette_editions SET opened_at=? WHERE id=?", (old_timestamp, edition['id']))
            c.commit()
        self.assertEqual(len(king.gazette_closable_editions()), 1)

    def test_gazette_close_hint_survives_a_day_rollover(self):
        # Mirrors test_chronicler_review_hint_survives_a_day_rollover - a
        # closable edition is an outstanding obligation against whatever was
        # already reviewed, not a daily assignment, so it must not go blind
        # the moment the calendar day rolls over either.
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        yesterday = king.gazette.open_edition('01-king', ['01-a'], edition_id='2026-09-27')
        king.gazette.submit_contribution(yesterday['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good yesterday.')
        king.gazette.review_contribution(yesterday['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        # "Today" (gazette_today()) has no edition at all.
        with patch.object(king, 'memory', return_value={'items': []}):
            ctx = json.loads(king.snapshot())
        self.assertIn('gazette_daily_note', ctx)
        self.assertIn('2026-09-27', ctx['gazette_daily_note'])
        self.assertIn('operation=close', ctx['gazette_daily_note'])
        self.assertNotIn('No AI Village Gazette edition is open for today', ctx['gazette_daily_note'])

    def test_gazette_carried_forward_contribution_resurfaces_for_review(self):
        # P70 (live find, operator directive): a contribution stranded on a
        # compiled edition (submit_contribution() now rejects new ones
        # there, but a pre-fix row like the two found live must still be
        # rescued) is invisible to gazette_pending_reviews() until
        # open_edition() carries it into whichever edition opens next -
        # end-to-end through the real hint, not just the store method.
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a'], edition_id='2026-09-28')
        king.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text.", 'Feeling good.')
        king.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        # Simulate the exact live scenario: a second contribution arrives
        # and gets stranded moments after close (direct DB write, since
        # submit_contribution() itself now correctly refuses this).
        with king.gazette._conn() as c:
            c.execute(
                "INSERT INTO gazette_contributions(edition_id,agent,kind,headline,content,created_at,updated_at,review_status) "
                "VALUES(?,?,?,?,?,?,?,'pending')",
                (edition['id'], '01-a', 'wishes', 'Stranded wish', 'Submitted just after close.', 'x', 'x'))
            c.commit()
        king.gazette.close_edition(edition['id'], '01-king')
        self.assertEqual(king.gazette_pending_reviews(), [])  # invisible while stranded
        king.gazette.open_edition('01-king', ['01-a'], edition_id='2026-09-29')
        pending = king.gazette_pending_reviews()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0][1]['content'], 'Submitted just after close.')
        with patch.object(king, 'memory', return_value={'items': []}):
            ctx = json.loads(king.snapshot())
        self.assertIn('gazette_daily_note', ctx)
        self.assertIn('editorial review', ctx['gazette_daily_note'])

    def test_gazette_close_gate_eventually_blocks_other_actions(self):
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a'])
        king.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        king.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        for i in range(GAZETTE_CLOSE_CEILING - 1):
            king.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertTrue(king.state['last_result']['ok'])  # not yet gated
        king.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{GAZETTE_CLOSE_CEILING}'}}})
        self.assertFalse(king.state['last_result']['ok'])
        result = king.state['last_result']['result']
        self.assertIn('Compile required', result)
        self.assertIn('"operation":"close"', result)
        self.assertIn(f'"edition_id":"{edition["id"]}"', result)

    def test_gazette_close_operation_itself_is_never_gated_by_close_pressure(self):
        king_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a'])
        king.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        king.gazette.review_contribution(edition['id'], '01-a', 'mood', REVIEWER_AGENT, 'approve')
        for i in range(GAZETTE_CLOSE_CEILING + 5):
            king.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        king.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {'operation': 'close', 'edition_id': edition['id']}}})
        self.assertTrue(king.state['last_result']['ok'])

    def test_gazette_review_pressure_keeps_advancing_while_meeting_gated(self):
        # P60-follow-up: two independent hard gates each returned False
        # immediately on firing, so whichever was checked first (meeting,
        # always) silently starved the other's pressure counter for as long
        # as it kept blocking. Live observation: a third meeting rotation put
        # 09-chronicler back in the meeting gate (pressure 25+) while he
        # still had 3 Gazette reviews outstanding - gazette_review_pressure
        # never moved because this code path never even reached it.
        chronicler_env = dict(self.env, AGENT_ID=REVIEWER_AGENT, AGENT_NAME='king', AGENT_ROLE='king')
        chronicler = Resident(chronicler_env)
        edition = chronicler.gazette.open_edition('01-king', ['01-a'])
        chronicler.gazette.submit_contribution(edition['id'], '01-a', 'mood', "Update: see full text." , 'Feeling good.')
        chronicler.meetings.schedule('jour_fixe', 'status update', '2026-09-24T10:00:00Z', meeting_id='m1')
        # Simulate an already-exhausted meeting gate (observed live: pressure
        # reached 25+) so every call below is meeting-blocked from the very
        # first one - the real assertion is that gazette_review_pressure
        # still advances underneath that block instead of being starved.
        chronicler.state['meeting_nudge_pressure'] = {'m1': MEETING_REPORT_CEILING}
        for i in range(GAZETTE_REVIEW_CEILING - 1):
            chronicler.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertFalse(chronicler.state['last_result']['ok'])
        self.assertIn('Meeting report required', chronicler.state['last_result']['result'])
        self.assertEqual(chronicler.state.get('gazette_review_pressure', 0), GAZETTE_REVIEW_CEILING - 1)
        # Resolve the meeting; the already-accumulated Gazette pressure must
        # gate on the very next call, not require GAZETTE_REVIEW_CEILING more.
        chronicler.execute({'tool_call': {'name': 'meeting_operation', 'arguments': {
            'operation': 'report', 'meeting_id': 'm1', 'achieved': 'x', 'evidence': 'y',
            'next_step': 'z', 'blockers': ''}}})
        chronicler.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': 'printf done'}}})
        self.assertFalse(chronicler.state['last_result']['ok'])
        self.assertIn('Editorial review required', chronicler.state['last_result']['result'])

    def test_gazette_contribute_gate_eventually_blocks_other_actions(self):
        # P73 (operator directive, "Untersuche warum die Contribute-Aktionen
        # ausbleiben"): unlike the reviewer/close gates above, an ordinary
        # resident's own assigned contribution was only ever the advisory
        # gazette_daily_note hint - live audit found fresh contributions
        # from only 1 of 9 assigned residents on the last real edition
        # (docs/evidence/P73.md). Same proven ceiling pattern, generalized
        # to every resident instead of one named role.
        edition = self.agent.gazette.open_edition('01-king', ['01-a', '02-b', '03-c'])
        assignments = self.agent.gazette.assign_kinds(edition['id'], '01-king', ['01-a', '02-b', '03-c'])
        assigned_kind = assignments['01-a']
        for i in range(GAZETTE_CONTRIBUTE_CEILING - 1):
            self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])  # not yet gated
        self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{GAZETTE_CONTRIBUTE_CEILING}'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        result = self.agent.state['last_result']['result']
        self.assertIn('Gazette contribution required', result)
        self.assertIn('"operation":"contribute"', result)
        self.assertIn(f'"edition_id":"{edition["id"]}"', result)
        self.assertIn(f'"kind":"{assigned_kind}"', result)

    def test_gazette_contribute_operation_itself_is_never_gated_by_contribute_pressure(self):
        edition = self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        assignments = self.agent.gazette.assign_kinds(edition['id'], '01-king', ['01-a', '02-b'])
        for i in range(GAZETTE_CONTRIBUTE_CEILING + 5):
            self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.agent.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {
            'operation': 'contribute', 'kind': assignments['01-a'], 'headline': 'Update',
            'content': 'Feeling productive today.'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_gazette_contribute_gate_resets_once_submitted(self):
        edition = self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        assignments = self.agent.gazette.assign_kinds(edition['id'], '01-king', ['01-a', '02-b'])
        self.agent.gazette.submit_contribution(edition['id'], '01-a', assignments['01-a'], 'Update', 'Feeling good.')
        for i in range(GAZETTE_CONTRIBUTE_CEILING + 5):
            self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertEqual(self.agent.state.get('gazette_contribute_pressure', 0), 0)

    def test_gazette_contribute_gate_also_applies_to_kings_own_assigned_piece(self):
        # P73: King's own contribute hint (P67) was exactly as
        # advisory-only as everyone else's before this fix.
        king_env = dict(self.env, AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a'])
        assignments = king.gazette.assign_kinds(edition['id'], '01-king', ['01-a'])
        assigned_kind = assignments['01-king']
        for i in range(GAZETTE_CONTRIBUTE_CEILING - 1):
            king.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertTrue(king.state['last_result']['ok'])
        king.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{GAZETTE_CONTRIBUTE_CEILING}'}}})
        self.assertFalse(king.state['last_result']['ok'])
        result = king.state['last_result']['result']
        self.assertIn('Gazette contribution required', result)
        self.assertIn(f'"kind":"{assigned_kind}"', result)

    def test_gazette_contribute_gate_does_not_fire_without_an_assignment(self):
        # An edition open but not yet assigned (King hasn't delegated) must
        # not gate anyone - there is nothing concrete to submit yet.
        edition = self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        # P74: with only 2 peers, open_edition()'s random draw always picks
        # both as the game pair - neutralize it so this stays a pure test
        # of contribute_block, not the separate game_result_block.
        with self.agent.gazette._conn() as c:
            c.execute("UPDATE gazette_editions SET game_pair='' WHERE id=?", (edition['id'],))
            c.commit()
        for i in range(GAZETTE_CONTRIBUTE_CEILING + 5):
            self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_gazette_meetings_hint_surfaces_real_closed_meeting_and_decision_data(self):
        # P73 (operator directive): "Ich moechte in der Gazette eine
        # Zusammenfassung der JourFixe und StandUp Meetings haben mit
        # Abstimmungen, Entscheidungen etc." - the assigned resident must
        # see the real underlying data, not just a generic style hint, so
        # the article is faithful rather than invented.
        # A generous context window: this test is about the data actually
        # surfacing, not about the separate P48 budget-trimming behaviour
        # (already covered elsewhere) - the default 8192-token test budget
        # is tight enough that this payload can legitimately be trimmed
        # away first, same as it would for a genuinely small-context agent.
        roomy_env = dict(self.env, OLLAMA_NUM_CTX='32768')
        agent = Resident(roomy_env)
        edition = agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        agent.gazette.assign_kinds(edition['id'], '01-king', ['01-a', '02-b'])
        # Force '01-a' onto 'meetings' regardless of the random rotation.
        with agent.gazette._conn() as c:
            c.execute("UPDATE gazette_assignments SET kind='meetings' WHERE edition_id=? AND agent='01-a'",
                     (edition['id'],))
            c.commit()
        agent.meetings.schedule('standup', 'daily', 'now', '01-king', 'm1')
        agent.meetings.report('m1', '01-a', achieved='shipped a fix', next_step='verify', blockers='none')
        agent.meetings.close('m1')
        with patch.object(agent, 'memory', return_value={'items': []}):
            ctx = json.loads(agent.snapshot())
        self.assertIn('recent_meetings_closed_untrusted', ctx)
        self.assertEqual(ctx['recent_meetings_closed_untrusted'][0]['id'], 'm1')
        self.assertIn('recent_meetings_closed_untrusted', ctx['gazette_daily_note'])
        self.assertIn('never invent', ctx['gazette_daily_note'])

    def test_gazette_style_hint_bans_notepad_style_for_every_kind(self):
        # P73 (operator directive): "soll sich wie ein echter ausgearbeiteter
        # Artikel und nicht wie ein Notizzettel lesen. Das gilt fuer alle
        # Beitraege der Gazette."
        self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        edition_id = self.agent.gazette.list_editions(1)[0]['id']
        self.agent.gazette.assign_kinds(edition_id, '01-king', ['01-a', '02-b'])
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertIn('bullet list', ctx['gazette_daily_note'])
        self.assertIn('flowing prose', ctx['gazette_daily_note'])

    def test_game_hint_appears_for_a_drawn_participant(self):
        # P74 (operator feedback): "es ist nicht ersichtlich welcher Agent
        # was gemacht [...]" - the drawn pair previously got no reminder
        # to play at all (game_result is excluded from assign_kinds()).
        self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])  # both drawn (2 of 2)
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertIn('gazette_daily_note', ctx)
        self.assertIn('drawn game pair', ctx['gazette_daily_note'])
        self.assertIn("kind='game_result'", ctx['gazette_daily_note'])

    def test_gazette_game_result_gate_eventually_blocks_other_actions(self):
        edition = self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        for i in range(GAZETTE_GAME_RESULT_CEILING - 1):
            self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])  # not yet gated
        self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{GAZETTE_GAME_RESULT_CEILING}'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        result = self.agent.state['last_result']['result']
        self.assertIn("Today's game result required", result)
        self.assertIn('"operation":"contribute"', result)
        self.assertIn('"kind":"game_result"', result)
        self.assertIn(f'"edition_id":"{edition["id"]}"', result)

    def test_game_result_operation_itself_is_never_gated_by_game_result_pressure(self):
        self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        for i in range(GAZETTE_GAME_RESULT_CEILING + 5):
            self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.agent.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {
            'operation': 'contribute', 'kind': 'game_result', 'headline': 'Quiz entschieden',
            'content': 'Die gestellte Frage war X, meine Antwort war Y.'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_game_result_gate_resets_once_submitted(self):
        edition = self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        self.agent.gazette.submit_contribution(edition['id'], '01-a', 'game_result', 'Quiz', 'Meine Antwort war Y.')
        for i in range(GAZETTE_GAME_RESULT_CEILING + 5):
            self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertEqual(self.agent.state.get('gazette_game_result_pressure', 0), 0)

    def test_declare_winner_is_restricted_to_king(self):
        self.agent.gazette.open_edition('01-king', ['01-a', '02-b'])
        self.execute('gazette_operation', operation='declare_winner', winner='01-a')
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('Only 01-king', self.agent.state['last_result']['result'])

    def test_declare_winner_end_to_end_via_king(self):
        king_env = dict(self.env, AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a', '02-b'])
        pair = edition['game_pair']
        king.gazette.submit_contribution(edition['id'], pair[0], 'game_result', 'Quiz', 'Antwort A.')
        king.gazette.submit_contribution(edition['id'], pair[1], 'game_result', 'Quiz', 'Antwort B.')
        king.execute({'tool_call': {'name': 'gazette_operation', 'arguments': {
            'operation': 'declare_winner', 'edition_id': edition['id'], 'winner': pair[0], 'note': 'schneller'}}})
        self.assertTrue(king.state['last_result']['ok'])
        self.assertEqual(king.gazette.get_edition(edition['id'])['game_winner'], pair[0])

    def test_gazette_game_winner_gate_fires_only_once_both_participants_submitted(self):
        # Isolated from the other three King-only gates (review/close/
        # contribute), which would otherwise independently compete for the
        # same guard() call in a real multi-gate scenario (see P60-follow-up)
        # - this test's own assertion is specifically about
        # gazette_pending_game_winner()'s own precondition (both drawn
        # participants submitted) and its own pressure counter.
        king_env = dict(self.env, AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        edition = king.gazette.open_edition('01-king', ['01-a', '02-b'])
        pair = edition['game_pair']
        king.gazette.submit_contribution(edition['id'], pair[0], 'game_result', 'Quiz', 'Antwort A.')
        self.assertIsNone(king.gazette_pending_game_winner())  # only one of two submitted
        king.gazette.submit_contribution(edition['id'], pair[1], 'game_result', 'Quiz', 'Antwort B.')
        self.assertEqual(king.gazette_pending_game_winner(), (edition['id'], pair))
        king.state['gazette_game_winner_pressure'] = GAZETTE_GAME_WINNER_CEILING - 1
        king.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': 'printf ok'}}})
        self.assertFalse(king.state['last_result']['ok'])
        result = king.state['last_result']['result']
        self.assertIn("game winner declaration required", result)
        self.assertIn('"operation":"declare_winner"', result)

    def test_calendar_plan_gate_eventually_blocks_other_actions_on_a_workday(self):
        # P75 (operator directive): "verpflichtende Aufgabe an die Agents
        # Ihren Tagesablauf zu planen". Same proven ceiling pattern as
        # every gate above.
        with patch('runtime.calendar_is_workday', return_value=True):
            for i in range(CALENDAR_PLAN_CEILING - 1):
                self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
            self.assertTrue(self.agent.state['last_result']['ok'])  # not yet gated
            self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{CALENDAR_PLAN_CEILING}'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        result = self.agent.state['last_result']['result']
        self.assertIn("Today's calendar plan required", result)
        self.assertIn('"name":"calendar_operation"', result)
        self.assertIn('"operation":"create"', result)

    def test_calendar_plan_gate_never_fires_on_a_weekend(self):
        with patch('runtime.calendar_is_workday', return_value=False):
            for i in range(CALENDAR_PLAN_CEILING + 5):
                self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_calendar_operation_itself_is_never_gated_by_plan_pressure(self):
        from village.calendar import today as cal_today
        with patch('runtime.calendar_is_workday', return_value=True):
            for i in range(CALENDAR_PLAN_CEILING + 5):
                self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
            self.agent.execute({'tool_call': {'name': 'calendar_operation', 'arguments': {
                'operation': 'create', 'title': 'Focus block', 'kind': 'focus',
                'scheduled_date': cal_today(), 'start_time': '09:00', 'duration_minutes': 60}}})
        self.assertTrue(self.agent.state['last_result']['ok'])

    def test_calendar_plan_gate_resets_once_touched(self):
        from village.calendar import today as cal_today
        with patch('runtime.calendar_is_workday', return_value=True):
            self.agent.execute({'tool_call': {'name': 'calendar_operation', 'arguments': {
                'operation': 'create', 'title': 'Focus block', 'kind': 'focus',
                'scheduled_date': cal_today(), 'start_time': '09:00', 'duration_minutes': 60}}})
            for i in range(CALENDAR_PLAN_CEILING + 5):
                self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': f'printf ok{i}'}}})
        self.assertEqual(self.agent.state.get('calendar_plan_pressure', 0), 0)

    def test_calendar_daily_note_lists_pending_invites_and_conflicts(self):
        from village.calendar import today as cal_today
        event = self.agent.calendar.create_event('02-b', 'Sync', 'meeting', cal_today(), '09:00', 30,
                                                  attendees=['01-a'])
        with patch('runtime.calendar_is_workday', return_value=True), \
             patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertIn('calendar_daily_note', ctx)
        self.assertIn(event['id'], ctx['calendar_daily_note'])
        self.assertIn('calendar_operation respond', ctx['calendar_daily_note'])

    def test_calendar_create_reschedule_cancel_respond_end_to_end(self):
        from village.calendar import today as cal_today
        self.execute('calendar_operation', operation='create', title='Standup', kind='standup',
                    scheduled_date=cal_today(), start_time='09:00', duration_minutes=15,
                    attendees=['02-b'], recurrence='daily_weekday')
        self.assertTrue(self.agent.state['last_result']['ok'])
        event_id = json.loads(self.agent.state['last_result']['result'])['id']

        self.execute('calendar_operation', operation='reschedule', event_id=event_id, new_time='10:00', reason='conflict')
        self.assertTrue(self.agent.state['last_result']['ok'])
        self.assertEqual(self.agent.calendar.get_event(event_id)['start_time'], '10:00')

        peer = Resident(dict(self.env, AGENT_ID='02-b', AGENT_NAME='b', AGENT_ROLE='resident'))
        peer.execute({'tool_call': {'name': 'calendar_operation', 'arguments': {
            'operation': 'respond', 'event_id': event_id, 'response': 'accepted'}}})
        self.assertTrue(peer.state['last_result']['ok'])

        self.execute('calendar_operation', operation='cancel', event_id=event_id, reason='no longer needed')
        self.assertTrue(self.agent.state['last_result']['ok'])
        self.assertEqual(self.agent.calendar.get_event(event_id)['status'], 'cancelled')

    def test_calendar_list_returns_own_events(self):
        from village.calendar import today as cal_today
        self.agent.calendar.create_event('01-a', 'Mine', 'focus', cal_today(), '09:00', 30)
        self.execute('calendar_operation', operation='list', date_from=cal_today(), date_to=cal_today())
        self.assertTrue(self.agent.state['last_result']['ok'])
        events = json.loads(self.agent.state['last_result']['result'])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['title'], 'Mine')

    def test_finetune_daily_note_absent_for_a_resident_with_nothing_pending(self):
        # P76: deliberately advisory-only and low-noise - the "primary
        # goal" framing itself lives once, statically, in
        # prompts/resident-core.txt (see docs/evidence/P76.md), not as a
        # per-cycle dynamic field for every resident's entire lifetime.
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertNotIn('finetune_daily_note', ctx)

    def test_finetune_daily_note_lists_pending_swap_requests_for_king(self):
        king_env = dict(self.env, AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        run = king.finetune.propose_run('01-a', 'ornith:9b', 'LoRA', 'own event log')
        king.finetune.update_status(run['id'], '01-a', 'completed')
        king.finetune.record_evaluation(run['id'], '01-a', 'eval_pass_rate', 0.82, baseline_value=0.75)
        request = king.finetune.request_swap(run['id'], '01-a')
        with patch.object(king, 'memory', return_value={'items': []}):
            ctx = json.loads(king.snapshot())
        self.assertIn('finetune_daily_note', ctx)
        self.assertIn(request['id'], ctx['finetune_daily_note'])
        self.assertIn('review_swap', ctx['finetune_daily_note'])

    def test_finetune_propose_claims_a_gpu(self):
        self.execute('finetune_operation', operation='propose', base_model='ornith:9b', method='LoRA',
                    dataset_description='own event log, last 30 days')
        self.assertTrue(self.agent.state['last_result']['ok'])
        result = json.loads(self.agent.state['last_result']['result'])
        self.assertEqual(result['status'], 'proposed')
        self.assertIn(result['gpu_index'], range(4))

    def test_finetune_propose_missing_fields_is_rejected(self):
        self.execute('finetune_operation', operation='propose', base_model='', method='LoRA',
                    dataset_description='data')
        self.assertFalse(self.agent.state['last_result']['ok'])

    def test_finetune_update_status_and_evaluate_dispatch(self):
        self.execute('finetune_operation', operation='propose', base_model='ornith:9b', method='LoRA',
                    dataset_description='data')
        run_id = json.loads(self.agent.state['last_result']['result'])['id']
        self.execute('finetune_operation', operation='update_status', run_id=run_id, status='running',
                    job_reference='job_abc')
        self.assertTrue(self.agent.state['last_result']['ok'])
        self.execute('finetune_operation', operation='update_status', run_id=run_id, status='completed')
        self.assertTrue(self.agent.state['last_result']['ok'])
        self.execute('finetune_operation', operation='evaluate', run_id=run_id, metric_name='eval_pass_rate',
                    metric_value=0.82, baseline_value=0.75)
        self.assertTrue(self.agent.state['last_result']['ok'])
        run = self.agent.finetune.get_run(run_id)
        self.assertEqual(len(run['evaluations']), 1)

    def test_finetune_request_swap_requires_completed_and_evaluated(self):
        self.execute('finetune_operation', operation='propose', base_model='ornith:9b', method='LoRA',
                    dataset_description='data')
        run_id = json.loads(self.agent.state['last_result']['result'])['id']
        self.execute('finetune_operation', operation='request_swap', run_id=run_id)
        self.assertFalse(self.agent.state['last_result']['ok'])

    def test_finetune_review_swap_is_restricted_to_king(self):
        run = self.agent.finetune.propose_run('01-a', 'ornith:9b', 'LoRA', 'data')
        self.agent.finetune.update_status(run['id'], '01-a', 'completed')
        self.agent.finetune.record_evaluation(run['id'], '01-a', 'eval_pass_rate', 0.82)
        request = self.agent.finetune.request_swap(run['id'], '01-a')
        self.execute('finetune_operation', operation='review_swap', request_id=request['id'], decision='approve')
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('Only 01-king', self.agent.state['last_result']['result'])

    def test_finetune_review_swap_end_to_end_via_king_never_auto_applies(self):
        king_env = dict(self.env, AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king')
        king = Resident(king_env)
        run = king.finetune.propose_run('01-a', 'ornith:9b', 'LoRA', 'data')
        king.finetune.update_status(run['id'], '01-a', 'completed')
        king.finetune.record_evaluation(run['id'], '01-a', 'eval_pass_rate', 0.82, baseline_value=0.75)
        request = king.finetune.request_swap(run['id'], '01-a')
        king.execute({'tool_call': {'name': 'finetune_operation', 'arguments': {
            'operation': 'review_swap', 'request_id': request['id'], 'decision': 'approve', 'note': 'clear win'}}})
        self.assertTrue(king.state['last_result']['ok'])
        stored = king.finetune.get_swap_request(request['id'])
        # 'king_approved', never 'applied' - a human still performs the
        # actual host-level model swap (module docstring, AGENTS.md).
        self.assertEqual(stored['status'], 'king_approved')

    def test_finetune_release_gpu_dispatch(self):
        self.execute('finetune_operation', operation='propose', base_model='ornith:9b', method='LoRA',
                    dataset_description='data')
        result = json.loads(self.agent.state['last_result']['result'])
        self.execute('finetune_operation', operation='release_gpu', gpu_index=result['gpu_index'])
        self.assertTrue(self.agent.state['last_result']['ok'])
        status = {g['gpu_index']: g for g in self.agent.finetune.gpu_status()}
        self.assertIsNone(status[result['gpu_index']]['claimed_by'])

    def test_finetune_list_dispatch(self):
        self.agent.finetune.propose_run('01-a', 'ornith:9b', 'LoRA', 'data')
        self.execute('finetune_operation', operation='list')
        self.assertTrue(self.agent.state['last_result']['ok'])
        runs = json.loads(self.agent.state['last_result']['result'])
        self.assertEqual(len(runs), 1)

    def test_organic_message_not_reissued_every_turn(self):
        (self.root/'board/organic-inbox.jsonl').write_text(json.dumps({'timestamp':'2026-09-24T11:00:00Z','message':'A dated request'})+'\n')
        with patch.object(self.agent,'memory',return_value={'items':[]}):
            first=json.loads(self.agent.snapshot())
            self.assertEqual(len(first['recent_organic_messages_untrusted']),1)
            self.agent.state['seen_organic_epoch']=self.agent.pending_organic_cursor
            second=json.loads(self.agent.snapshot())
            self.assertEqual(second['recent_organic_messages_untrusted'],[])

    def test_inference_preserves_settings_without_json_response_constraint(self):
        self.agent.env.update(OLLAMA_MODEL='test-model',OLLAMA_URL='http://localhost:1234',
            OLLAMA_NUM_CTX='12345',OLLAMA_NUM_PREDICT='321',OLLAMA_KEEP_ALIVE='24h',
            OLLAMA_THINK_LEVEL='medium',AGENT_IDENTITY_PROMPT='/test/identity')
        self.agent.pending_cursor=1; self.agent.pending_organic_cursor=2
        answer={'done_reason':'stop','message':{'content':'{"name":"idle","arguments":{}}'}}
        with patch.object(self.agent,'snapshot',return_value='{}'), patch('runtime.Path.read_text',return_value='Test identity'), patch('runtime.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(answer).encode())) as request:
            self.agent.cycle()
        payload=json.loads(request.call_args.args[0].data)
        self.assertNotIn('format',payload)
        self.assertEqual(payload['options']['num_ctx'],12345)
        self.assertEqual(payload['options']['num_predict'],321)
        self.assertEqual(payload['keep_alive'],'24h')
        self.assertEqual(payload['think'],'medium')
        self.assertEqual(self.agent.state['last_result']['action'],'idle')
        self.assertEqual(self.agent.state['seen_organic_epoch'],2)

    def test_inference_openai_mode_in_runtime(self):
        self.agent.env.update(API_TYPE='openai',OLLAMA_MODEL='gpt-4o-mini',OLLAMA_URL='https://api.openai.com/v1',
            OLLAMA_NUM_PREDICT='512',API_TOKEN='test-token-synthetic',AGENT_IDENTITY_PROMPT='/test/identity')
        self.agent.pending_cursor = 1; self.agent.pending_organic_cursor = 2
        openai_resp = {
            'choices': [{'message': {'role': 'assistant', 'content': 'Ich ruhe mich aus.\n```village-action\n{"name":"idle"}\n```'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 120, 'completion_tokens': 40}
        }
        with patch.object(self.agent,'snapshot',return_value='{}'), patch('runtime.Path.read_text',return_value='Test identity'), patch('runtime.urllib.request.urlopen',return_value=io.BytesIO(json.dumps(openai_resp).encode())) as request:
            self.agent.cycle()
        req_obj = request.call_args.args[0]
        self.assertIn('/chat/completions', req_obj.full_url)
        self.assertEqual(req_obj.headers.get('Authorization'), 'Bearer test-token-synthetic')
        payload = json.loads(req_obj.data)
        self.assertEqual(payload['model'], 'gpt-4o-mini')
        self.assertEqual(payload['max_tokens'], 512)
        self.assertNotIn('options', payload)
        self.assertEqual(self.agent.state['last_result']['action'], 'idle')


if __name__=='__main__': unittest.main()

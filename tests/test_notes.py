"""P125: self-maintained notes as between-turn memory, with kb_id pointers into
the knowledge base and on-demand detail via memory_get. Local, synthetic."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from runtime import Resident
from decision import decision
from village.actions import ACTION_SPECS, action_schema
from village import notes as N


class NotesModuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_or_corrupt_file_yields_empty_notes(self):
        self.assertTrue(N.is_empty(N.load_notes(self.home)))
        N.notes_path(self.home).write_text('{not json')
        self.assertTrue(N.is_empty(N.load_notes(self.home)))

    def test_set_add_remove_roundtrip_and_permissions(self):
        notes, _ = N.apply_operation(N.empty_notes(), {'operation': 'set', 'focus': 'F', 'next_query': 'Q'})
        notes, _ = N.apply_operation(notes, {'operation': 'add', 'note': 'lesson', 'kb_id': 'mem_1'})
        notes, _ = N.apply_operation(notes, {'operation': 'add', 'note': 'reply to 02-b', 'list': 'commitments'})
        N.save_notes(self.home, notes)
        self.assertEqual(N.notes_path(self.home).stat().st_mode & 0o777, 0o600)
        loaded = N.load_notes(self.home)
        self.assertEqual(loaded['focus'], 'F')
        self.assertEqual(loaded['entries'], [{'note': 'lesson', 'kb_id': 'mem_1'}])
        self.assertEqual(loaded['commitments'], ['reply to 02-b'])
        loaded, summary = N.apply_operation(loaded, {'operation': 'remove', 'list': 'entries', 'index': 0})
        self.assertEqual(loaded['entries'], [])
        self.assertIn('removed', summary)

    def test_lists_are_bounded_oldest_dropped(self):
        notes = N.empty_notes()
        for i in range(N.MAX_ENTRIES + 3):
            notes, summary = N.apply_operation(notes, {'operation': 'add', 'note': f'n{i}'})
        self.assertEqual(len(notes['entries']), N.MAX_ENTRIES)
        self.assertEqual(notes['entries'][0]['note'], 'n3')
        self.assertIn('knowledge base', summary)

    def test_fields_are_clipped(self):
        notes, _ = N.apply_operation(N.empty_notes(), {'operation': 'set', 'focus': 'x' * 5000})
        self.assertEqual(len(notes['focus']), N.FIELD_MAX_CHARS)

    def test_invalid_operations_raise(self):
        for args in ({'operation': 'wipe'}, {'operation': 'set'}, {'operation': 'add'},
                     {'operation': 'remove', 'list': 'entries', 'index': 0},
                     {'operation': 'add', 'note': 'x', 'list': 'secrets'}):
            with self.assertRaises(ValueError):
                N.apply_operation(N.empty_notes(), args)

    def test_retrieval_query_prefers_next_query_then_focus(self):
        self.assertEqual(N.retrieval_query({'next_query': 'nq', 'focus': 'f'}, 'fb'), 'nq')
        self.assertEqual(N.retrieval_query({'next_query': '', 'focus': 'f'}, 'fb'), 'f')
        self.assertEqual(N.retrieval_query(N.empty_notes(), 'fb'), 'fb')


class ActionContractTests(unittest.TestCase):
    def test_actions_are_specified_and_schema_valid(self):
        self.assertIn('notes_operation', ACTION_SPECS)
        self.assertIn('memory_get', ACTION_SPECS)
        schema = action_schema(['notes_operation', 'memory_get'])
        self.assertEqual(len(schema['oneOf']), 3)  # + idle

    def test_decision_parser_accepts_both_actions(self):
        for action in ({'name': 'notes_operation', 'arguments': {'operation': 'set', 'focus': 'x'}},
                       {'name': 'memory_get', 'arguments': {'id': 'mem_1'}}):
            parsed = decision({'message': {'content': json.dumps(action)}})
            self.assertFalse(parsed.get('fallback_reason'), parsed)
            self.assertEqual(parsed['tool_call']['name'], action['name'])

    def test_decision_parser_rejects_unknown_notes_operation(self):
        parsed = decision({'message': {'content': json.dumps(
            {'name': 'notes_operation', 'arguments': {'operation': 'wipe'}})}})
        self.assertTrue(parsed.get('fallback_reason'))


class RuntimeNotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        self.env = dict(os.environ, AGENT_ID='01-a', AGENT_NAME='a', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), VILLAGE_MAX_OUTPUT_BYTES='4096',
                        VILLAGE_COMMAND_TIMEOUT_SECONDS='2', OLLAMA_NUM_CTX='16384')
        for target in ('runtime.calendar_is_workday', 'runtime.gazette_deadline_passed'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False)
        p.start(); self.addCleanup(p.stop)
        self.agent = Resident(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def execute(self, name, **args):
        self.agent.execute({'tool_call': {'name': name, 'arguments': args}})
        return self.agent.state['last_result']

    def snapshot(self, memory_result=None):
        with patch.object(self.agent, 'memory', return_value=memory_result or {'items': []}) as mem:
            ctx = json.loads(self.agent.snapshot())
        return ctx, mem

    def test_empty_notes_produce_a_hint(self):
        ctx, _ = self.snapshot()
        self.assertIn('notes_hint', ctx)
        self.assertTrue(N.is_empty(ctx['your_notes']))

    def test_notes_operation_persists_and_appears_every_turn(self):
        result = self.execute('notes_operation', operation='set', focus='measure M10 throughput', next_query='M10 cuda')
        self.assertTrue(result['ok'], result)
        self.execute('notes_operation', operation='add', note='cuda probe failed: no CDI', kb_id='mem_42')
        again = Resident(self.env)  # survives restart
        with patch.object(again, 'memory', return_value={'items': []}) as mem:
            ctx = json.loads(again.snapshot())
        self.assertEqual(ctx['your_notes']['focus'], 'measure M10 throughput')
        self.assertEqual(ctx['your_notes']['entries'][0]['kb_id'], 'mem_42')
        self.assertNotIn('notes_hint', ctx)
        self.assertEqual(mem.call_args[0][1]['query'], 'M10 cuda')  # notes steer retrieval

    def test_invalid_notes_operation_fails_without_crash(self):
        result = self.execute('notes_operation', operation='remove', list='entries', index=3)
        self.assertFalse(result['ok'])
        self.assertIn('out of range', result['result'])

    def test_notes_survive_budget_trimming(self):
        self.execute('notes_operation', operation='set', focus='keep me')
        agent = Resident(dict(self.env, OLLAMA_NUM_CTX='8192'))
        agent.tasks.path.write_text(json.dumps(
            [{'id': str(i), 'title': 'p' * 700, 'owner': '02-b', 'status': 'open'} for i in range(30)]))
        with patch.object(agent, 'memory', return_value={'items': []}):
            ctx = json.loads(agent.snapshot())
        self.assertEqual(ctx['your_notes']['focus'], 'keep me')

    def test_memory_get_fetches_one_record_by_get(self):
        record = {'id': 'mem_42', 'agent': '02-b', 'scope': 'shared', 'content': 'full detail ' + 'd' * 3000}
        with patch.object(self.agent, 'memory', return_value=record) as mem:
            result = self.execute('memory_get', id='mem_42')
        self.assertTrue(result['ok'], result)
        self.assertEqual(mem.call_args[0], ('/v1/memories/mem_42',))  # no body -> GET
        self.assertIn('full detail', result['result'])

    def test_memory_get_rejects_path_like_ids(self):
        with patch.object(self.agent, 'memory') as mem:
            result = self.execute('memory_get', id='../stats')
        self.assertFalse(result['ok'])
        mem.assert_not_called()

    def test_memory_remember_points_at_notes(self):
        with patch.object(self.agent, 'memory', return_value={'id': 'mem_7', 'stored': True}):
            result = self.execute('memory_remember', content='lesson', kind='lesson', scope='shared')
        self.assertIn('kb_id=mem_7', result['result'])

    def test_notes_upkeep_does_not_build_collaboration_pressure(self):
        before = int(self.agent.state.get('collaboration_pressure', 0))
        self.execute('notes_operation', operation='set', focus='x')
        self.assertEqual(int(self.agent.state.get('collaboration_pressure', 0)), before)

    def test_tools_list_offers_the_new_actions(self):
        ctx, _ = self.snapshot()
        self.assertIn('notes_operation', ctx['tools'])
        self.assertIn('memory_get', ctx['tools'])


if __name__ == '__main__':
    unittest.main()

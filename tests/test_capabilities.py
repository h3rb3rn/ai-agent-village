"""P144: register of missing model capabilities. Local, synthetic, no network."""
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from decision import decision
from runtime import Resident
from village.capabilities import (CapabilityStore, TARGETS, DATA_READY_TRAIN, SPEC_READY_HELDOUT,
                                  example_split, observed_signature)


def record(label, reason=None, result='', agent='02-a', model='qwen3.5:4b'):
    return dict(label=label, fallback_reason=reason, result=result, agent=agent, model=model, request_id='req_1')


class SignatureTests(unittest.TestCase):
    def test_runtime_facts_map_to_failure_classes(self):
        cases = [
            (record('invalid_format', 'output budget exhausted'), 'observed:output_budget_exhausted'),
            (record('invalid_format', 'arguments must be a JSON object'), 'observed:arguments_not_object'),
            (record('invalid_format', 'unclosed action block'), 'observed:incomplete_action_block'),
            (record('invalid_format', 'action x is not available to you; choose one of: idle'), 'observed:unavailable_action'),
            (record('invalid_format', 'missing action argument'), 'observed:missing_argument'),
            (record('invalid_format', 'unknown meeting operation: only report'), 'observed:unknown_operation'),
            (record('failed', result='cat: /var/var/ai- village: No such file or directory'), 'observed:nonexistent_path'),
            (record('failed', result='cat: /x: Datei oder Verzeichnis nicht gefunden'), 'observed:nonexistent_path'),
            (record('failed', result='ls: /home/other: Permission denied'), 'observed:permission_boundary'),
            (record('repeat_blocked'), 'observed:repeated_action'),
        ]
        for rec, expected in cases:
            self.assertEqual(observed_signature(rec), expected, rec)

    def test_ordinary_outcomes_are_not_gaps(self):
        self.assertIsNone(observed_signature(record('ok')))
        self.assertIsNone(observed_signature(record('failed', result='exit 1: grep found nothing')))
        self.assertIsNone(observed_signature(record('prose')))

    def test_split_is_deterministic_and_roughly_one_fifth_heldout(self):
        splits = [example_split(f'ex_{i}') for i in range(2000)]
        self.assertEqual(splits, [example_split(f'ex_{i}') for i in range(2000)])
        self.assertTrue(300 < splits.count('heldout') < 500)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CapabilityStore(Path(self.tmp.name) / 'coordination.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_moe_targets_are_seeded_once_including_eight_subtasks(self):
        CapabilityStore(self.store.db_path)
        gaps = self.store.list(limit=100)
        self.assertEqual(len([g for g in gaps if g['origin'] == 'target']), len(TARGETS))
        planner = next(g for g in gaps if 'planner' == g['role'] and '1-8' in g['title'])
        self.assertEqual(planner['status'], 'open')

    def test_observations_count_per_model_and_confirm_across_agents(self):
        gap = self.store.observe(record('repeat_blocked', agent='02-a'))
        self.assertEqual(self.store.view(gap)['status'], 'open')
        self.store.observe(record('repeat_blocked', agent='02-a'))
        self.store.observe(record('repeat_blocked', agent='06-b', model='nemotron-3-nano:4b'))
        view = self.store.view(gap)
        self.assertEqual(view['status'], 'confirmed')
        self.assertEqual({m['model']: m['count'] for m in view['by_model']},
                         {'qwen3.5:4b': 2, 'nemotron-3-nano:4b': 1})
        listed = self.store.list(model='nemotron-3-nano:4b')
        self.assertEqual(listed[0]['id'], gap)
        self.assertEqual(listed[0]['observed_for_model'], 1)
        self.assertEqual(listed[0]['observed_total'], 3)

    def test_evidence_rows_are_bounded_but_counts_are_not(self):
        for _ in range(40):
            gap = self.store.observe(record('repeat_blocked'))
        view = self.store.view(gap)
        self.assertEqual(view['by_model'][0]['count'], 40)
        import sqlite3
        with sqlite3.connect(self.store.db_path) as c:
            rows = c.execute('SELECT COUNT(*) FROM capability_evidence WHERE gap_id=?', (gap,)).fetchone()[0]
        self.assertEqual(rows, 20)

    def test_report_dedupes_by_title_and_counts_as_reproduction(self):
        first = self.store.report('02-a', 'qwen3.5:4b', 'Cannot parse YAML front matter', 'it guesses keys', 'parse keys exactly')
        second = self.store.report('03-b', 'qwen3.5:4b', 'Cannot parse YAML front matter!', 'same here', '')
        self.assertFalse(first['existed'])
        self.assertTrue(second['existed'])
        self.assertEqual(first['gap_id'], second['gap_id'])
        self.assertEqual(second['gap']['status'], 'confirmed')
        with self.assertRaises(ValueError):
            self.store.report('02-a', 'm', 'short', 'x', '')

    def test_examples_move_status_to_spec_and_data_ready(self):
        gap = self.store.report('02-a', 'qwen3.5:4b', 'Keep numbers unchanged in plans', 'drops units', 'copy units')['gap_id']
        i = 0
        while self.store.view(gap)['status'] != 'spec_ready':
            self.store.add_example(gap, '02-a', 'qwen3.5:4b', f'request {i}: 1500 MTU', f'1500 MTU #{i}', 'contains')
            i += 1
            self.assertLess(i, 200)
        while self.store.view(gap)['status'] != 'data_ready':
            self.store.add_example(gap, '02-a', 'qwen3.5:4b', f'request {i}: 1500 MTU', f'1500 MTU #{i}', 'contains')
            i += 1
            self.assertLess(i, 400)
        out = Path(self.tmp.name) / 'train.jsonl'
        n = self.store.export_examples(out, 'train', gap)
        self.assertGreaterEqual(n, DATA_READY_TRAIN)
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertTrue(all(r['split'] == 'train' and r['schema'] == 'village-capability-example/1' for r in rows))
        held = self.store.export_examples(Path(self.tmp.name) / 'held.jsonl', 'heldout', gap)
        self.assertGreaterEqual(held, SPEC_READY_HELDOUT)

    def test_example_validation_and_duplicates(self):
        gap = self.store.list(limit=1)[0]['id']
        first = self.store.add_example(gap, '02-a', 'm', 'in', 'out', 'exact')
        self.assertNotIn('split', first)  # the author never learns the split
        self.assertTrue(self.store.add_example(gap, '02-a', 'm', 'in', 'out', 'exact')['duplicate'])
        with self.assertRaises(ValueError):
            self.store.add_example(gap, '02-a', 'm', 'in', 'out', 'bogus')
        with self.assertRaises(Exception):
            self.store.add_example(gap, '02-a', 'm', 'in', '([unclosed', 'regex')
        with self.assertRaises(ValueError):
            self.store.add_example('cap_missing', '02-a', 'm', 'in', 'out', 'exact')



class KnowledgeFirstTests(unittest.TestCase):
    """P146: skills are trained, knowledge goes to the knowledge base."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = CapabilityStore(Path(self.tmp.name) / 'coordination.sqlite3')

    def tearDown(self):
        self.tmp.cleanup()

    def test_research_and_honesty_targets_are_seeded_as_skills(self):
        gaps = {g['id']: g for g in self.store.list(limit=100)}
        titles = {g['title'] for g in gaps.values()}
        for title in ('Retrieve before answering when the fact is not certain',
                      'Admit a knowledge gap instead of inventing',
                      'Research missing knowledge, then store it with its source',
                      'Answer only from the retrieved text and cite it'):
            self.assertIn(title, titles)
        self.assertTrue(all(g['kind'] == 'skill' for g in gaps.values()))

    def test_knowledge_gap_points_to_the_knowledge_base_and_takes_no_examples(self):
        res = self.store.report('02-a', 'qwen3.5:4b', 'Does not know the M10 driver version',
                                'guessed 470', '', kind='knowledge')
        self.assertEqual(res['gap']['kind'], 'knowledge')
        self.assertIn('memory_remember', res['hint'])
        with self.assertRaises(ValueError) as ctx:
            self.store.add_example(res['gap_id'], '02-a', 'm', 'driver?', '535', 'exact')
        self.assertIn('memory_remember', str(ctx.exception))
        self.store.reproduce(res['gap_id'], '03-b', 'qwen3.5:4b')
        self.assertEqual(self.store.view(res['gap_id'])['status'], 'confirmed')
        with self.assertRaises(ValueError):
            self.store.report('02-a', 'm', 'Some other missing thing', 'd', '', kind='opinion')

    def test_export_never_contains_knowledge(self):
        import sqlite3
        skill = self.store.report('02-a', 'm', 'Drops units from constants', 'd', '')['gap_id']
        fact = self.store.report('02-a', 'm', 'Unknown host name of the lab', 'd', '', kind='knowledge')['gap_id']
        for i in range(30):
            self.store.add_example(skill, '02-a', 'm', f'in {i}', f'out {i}', 'exact')
        with sqlite3.connect(self.store.db_path) as c:  # even if an example slipped in by other means
            c.execute("INSERT INTO capability_examples (id, gap_id, agent, model, input, expected, check_kind, split, "
                      "created_at) VALUES ('ex_fact', ?, '02-a', 'm', 'host?', 'n06', 'exact', 'train', 'x')", (fact,))
        out = Path(self.tmp.name) / 'train.jsonl'
        self.store.export_examples(out, 'train')
        gaps = {json.loads(l)['gap'] for l in out.read_text().splitlines()}
        self.assertEqual(gaps, {'reported:drops_units_from_constants'})

    def test_old_database_is_migrated_with_kind_skill(self):
        import sqlite3
        db = Path(self.tmp.name) / 'old.sqlite3'
        with sqlite3.connect(db) as c:
            c.execute('''CREATE TABLE capability_gaps(id TEXT PRIMARY KEY, signature TEXT UNIQUE, origin TEXT,
                role TEXT, title TEXT, description TEXT, desired_behavior TEXT, acceptance TEXT, status TEXT,
                created_by TEXT, created_at TEXT, updated_at TEXT)''')
            c.execute("INSERT INTO capability_gaps VALUES ('cap_old','reported:x','reported','any','old gap','d','','','open','a','t','t')")
            c.execute('''CREATE TABLE capability_examples(id TEXT PRIMARY KEY, gap_id TEXT, agent TEXT, model TEXT,
                input TEXT, expected TEXT, check_kind TEXT, split TEXT, created_at TEXT)''')
            c.execute("INSERT INTO capability_examples VALUES ('ex_old','cap_old','a','m','i','o','exact','train','t')")
        store = CapabilityStore(db)
        self.assertEqual(store.view('cap_old')['kind'], 'skill')
        out = Path(self.tmp.name) / 'old.jsonl'
        store.export_examples(out, 'train')
        self.assertEqual(json.loads(out.read_text())['source'], 'resident')

    def test_teacher_demonstrations_are_marked_and_exported(self):
        gap = self.store.list(limit=100)
        gap_id = next(g['id'] for g in gap if g['title'] == 'Research missing knowledge, then store it with its source')
        for i in range(40):
            self.store.add_example(gap_id, '01-king', 'qwen3.6:35b', f'question {i}',
                                   f'memory_search -> research_request -> memory_remember #{i}', 'contains', source='teacher')
        out = Path(self.tmp.name) / 'teacher.jsonl'
        n = self.store.export_examples(out, 'train', gap_id)
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        self.assertEqual(len(rows), n)
        self.assertTrue(rows and all(r['source'] == 'teacher' and r['author_model'] == 'qwen3.6:35b' for r in rows))
        with self.assertRaises(ValueError):
            self.store.add_example(gap_id, '01-king', 'm', 'q', 'a', 'exact', source='internet')

    def test_parser_rejects_unknown_kind(self):
        parsed = decision({'message': {'content': json.dumps({'name': 'capability_operation', 'arguments': {
            'operation': 'report', 'title': 'a title here', 'description': 'd', 'kind': 'fact'}})}})
        self.assertIn('skill or knowledge', parsed['fallback_reason'])

class DecisionTests(unittest.TestCase):
    def parse(self, args):
        return decision({'message': {'content': json.dumps({'name': 'capability_operation', 'arguments': args})}})

    def test_valid_and_invalid_operations(self):
        self.assertFalse(self.parse({'operation': 'list'}).get('fallback_reason'))
        self.assertFalse(self.parse({'operation': 'report', 'title': 'a title here', 'description': 'd'}).get('fallback_reason'))
        self.assertIn('unknown capability operation', self.parse({'operation': 'delete'})['fallback_reason'])
        self.assertEqual(self.parse({'operation': 'add_example', 'gap_id': 'cap_x', 'input': 'i'})['fallback_reason'],
                         'missing action argument')
        self.assertFalse(self.parse({'action': 'list'}).get('fallback_reason'))  # action alias


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        self.env = dict(os.environ, AGENT_ID='02-a', AGENT_NAME='a', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), VILLAGE_MAX_OUTPUT_BYTES='4096',
                        VILLAGE_COMMAND_TIMEOUT_SECONDS='2', OLLAMA_MODEL='qwen3.5:4b',
                        OLLAMA_URL='http://localhost:1234', AGENT_IDENTITY_PROMPT='/test/identity')
        for target in ('runtime.calendar_is_planning_day', 'runtime.gazette_deadline_passed'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False)
        p.start(); self.addCleanup(p.stop)
        self.agent = Resident(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cycle(self, content):
        answer = {'done_reason': 'stop', 'message': {'content': content}}
        self.agent.pending_cursor = 1; self.agent.pending_organic_cursor = 2
        with patch.object(self.agent, 'snapshot', return_value='{}'), \
                patch('runtime.Path.read_text', return_value='Test identity'), \
                patch('runtime.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(answer).encode())):
            self.agent.cycle()

    def test_failed_cycle_is_counted_for_the_own_model(self):
        self.run_cycle('{"name":"execute_bash","arguments":{"command":"cat /nonexistent/path"}}')
        gaps = self.agent.capabilities.list(model='qwen3.5:4b')
        top = gaps[0]
        self.assertEqual(top['title'], 'Used a file path that does not exist')
        self.assertEqual(top['observed_for_model'], 1)

    def test_observation_works_with_the_archive_switched_off(self):
        self.agent.env['VILLAGE_TRACE_ARCHIVE'] = '0'
        self.run_cycle('{"name":"execute_bash","arguments":"ls"}')
        self.assertFalse((self.agent.home / 'training').exists())
        self.assertEqual(self.agent.capabilities.list(model='qwen3.5:4b')[0]['observed_for_model'], 1)

    def test_resident_reports_and_sees_its_model_gaps_in_the_snapshot(self):
        self.agent.execute({'tool_call': {'name': 'capability_operation', 'arguments': {
            'operation': 'report', 'title': 'Loses units when copying constants',
            'description': 'wrote 1500 instead of 1500 MTU', 'desired_behavior': 'copy value and unit'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])
        gap_id = json.loads(self.agent.state['last_result']['result'])['gap_id']
        self.agent.execute({'tool_call': {'name': 'capability_operation', 'arguments': {
            'operation': 'add_example', 'gap_id': gap_id, 'input': 'MTU is 1500', 'expected': '1500 MTU', 'check': 'contains'}}})
        self.assertTrue(self.agent.state['last_result']['ok'])
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            ctx = json.loads(self.agent.snapshot())
        self.assertEqual(ctx['your_model_gaps']['model'], 'qwen3.5:4b')
        self.assertEqual(ctx['your_model_gaps']['gaps'][0]['id'], gap_id)

    def test_unknown_gap_is_a_failed_action_not_a_crash(self):
        self.agent.execute({'tool_call': {'name': 'capability_operation', 'arguments': {
            'operation': 'reproduce', 'gap_id': 'cap_nope'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('unknown gap_id', self.agent.state['last_result']['result'])


# P148: these tests exercise the waking cycle; keep them independent of the wall-clock dream window.
_no_dream = None


def setUpModule():
    global _no_dream
    from web.runtime import Resident as _Resident
    _no_dream = patch.object(_Resident, 'is_dreaming', lambda self, minute=None: False)
    _no_dream.start()


def tearDownModule():
    _no_dream.stop()


if __name__ == '__main__':
    unittest.main()

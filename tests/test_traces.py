"""P143: per-cycle training-trace archive. Local, synthetic; the secret below is a test value."""
import gzip
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from runtime import Resident
from village.traces import append_trace, store_system_prompt, trace_label

SYNTHETIC_TOKEN = 'synthetic-test-token-0123456789abcdef'


class LabelTests(unittest.TestCase):
    def test_labels_come_from_runtime_facts(self):
        ok = {'ok': True, 'result': 'fine'}
        self.assertEqual(trace_label({'tool_call': {'name': 'idle'}}, ok, True), 'ok')
        self.assertEqual(trace_label({'tool_call': {}}, {'ok': False, 'result': 'No such file'}, True), 'failed')
        self.assertEqual(trace_label({}, {'ok': False, 'result': 'Repeated action blocked: x'}, True), 'repeat_blocked')
        self.assertEqual(trace_label({'fallback_reason': 'bad json'}, ok, True), 'invalid_format')
        self.assertEqual(trace_label({'prose': True}, ok, True), 'prose')
        self.assertEqual(trace_label({}, ok, False), 'discarded')
        self.assertEqual(trace_label({}, None, True), 'unknown')


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_files_are_private_and_append_per_day(self):
        day = datetime(2026, 10, 12, tzinfo=timezone.utc)
        append_trace(self.home, {'n': 1}, now=day)
        path = append_trace(self.home, {'n': 2}, now=day)
        self.assertEqual([json.loads(l)['n'] for l in path.read_text().splitlines()], [1, 2])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.home / 'training').stat().st_mode), 0o700)

    def test_new_day_compresses_the_previous_one(self):
        append_trace(self.home, {'n': 1}, now=datetime(2026, 10, 12, tzinfo=timezone.utc))
        append_trace(self.home, {'n': 2}, now=datetime(2026, 10, 13, tzinfo=timezone.utc))
        traces = self.home / 'training' / 'traces'
        self.assertFalse((traces / '2026-10-12.jsonl').exists())
        with gzip.open(traces / '2026-10-12.jsonl.gz', 'rt') as handle:
            self.assertEqual(json.loads(handle.read())['n'], 1)
        self.assertTrue((traces / '2026-10-13.jsonl').exists())

    def test_cap_drops_oldest_days_but_never_today(self):
        # The cap is checked at each day's first write, against the archived days.
        for day in range(10, 15):
            append_trace(self.home, {'pad': os.urandom(300000).hex()}, max_mb=1,
                         now=datetime(2026, 10, day, tzinfo=timezone.utc))
        names = sorted(p.name for p in (self.home / 'training' / 'traces').iterdir())
        self.assertIn('2026-10-14.jsonl', names)
        self.assertNotIn('2026-10-10.jsonl.gz', names)

    def test_system_prompt_is_stored_once_per_hash(self):
        a = store_system_prompt(self.home, 'system v1')
        b = store_system_prompt(self.home, 'system v1')
        c = store_system_prompt(self.home, 'system v2')
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(len(list((self.home / 'training' / 'system').iterdir())), 2)


class CycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        self.env = dict(os.environ, AGENT_ID='02-a', AGENT_NAME='a', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), VILLAGE_MAX_OUTPUT_BYTES='4096',
                        VILLAGE_COMMAND_TIMEOUT_SECONDS='2', OLLAMA_MODEL='qwen3.5:4b',
                        OLLAMA_URL='http://localhost:1234', OLLAMA_THINK_LEVEL='medium',
                        AGENT_IDENTITY_PROMPT='/test/identity', API_TOKEN=SYNTHETIC_TOKEN)
        for target in ('runtime.calendar_is_workday', 'runtime.gazette_deadline_passed'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False)
        p.start(); self.addCleanup(p.stop)
        self.agent = Resident(self.env)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cycle(self, content, thinking='', snapshot='{"k": 1}'):
        answer = {'done_reason': 'stop', 'prompt_eval_count': 100, 'eval_count': 10,
                  'message': {'content': content, 'thinking': thinking}}
        self.agent.pending_cursor = 1; self.agent.pending_organic_cursor = 2  # snapshot() is mocked
        with patch.object(self.agent, 'snapshot', return_value=snapshot), \
                patch('runtime.Path.read_text', return_value='Test identity'), \
                patch('runtime.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(answer).encode())):
            self.agent.cycle()

    def records(self):
        files = sorted((self.agent.home / 'training' / 'traces').glob('*.jsonl'))
        return [json.loads(line) for f in files for line in f.read_text().splitlines()]

    def test_successful_cycle_is_archived_with_label_and_thinking(self):
        self.run_cycle('{"name":"idle","arguments":{}}', thinking='I should rest now.')
        rec = self.records()[-1]
        self.assertEqual(rec['schema'], 'village-trace/1')
        self.assertEqual(rec['agent'], '02-a')
        self.assertEqual(rec['model'], 'qwen3.5:4b')
        self.assertEqual(rec['think_level'], 'medium')
        self.assertEqual(rec['action']['name'], 'idle')
        self.assertEqual(rec['label'], 'ok')
        self.assertEqual(rec['thinking'], 'I should rest now.')
        self.assertTrue(rec['user'].startswith('{"k": 1}'))
        self.assertEqual(rec['metrics']['prompt_eval_count'], 100)
        system = self.agent.home / 'training' / 'system' / f"{rec['system_sha']}.txt"
        self.assertTrue(system.exists())

    def test_invalid_output_is_labelled_not_dropped(self):
        self.run_cycle('```village-action\n{"name":"idle"')
        self.assertEqual(self.records()[-1]['label'], 'invalid_format')
        self.assertTrue(self.records()[-1]['fallback_reason'])

    def test_failed_command_is_labelled_failed_with_result(self):
        self.run_cycle('{"name":"execute_bash","arguments":{"command":"cat /nonexistent/file"}}')
        rec = self.records()[-1]
        self.assertEqual(rec['label'], 'failed')
        self.assertIn('result=failure(1)', rec['result'])

    def test_secrets_are_redacted_everywhere(self):
        self.run_cycle(f'{{"name":"idle","arguments":{{}}}} {SYNTHETIC_TOKEN}', thinking=SYNTHETIC_TOKEN,
                       snapshot=json.dumps({'leak': SYNTHETIC_TOKEN}))
        raw = '\n'.join(p.read_text() for p in (self.agent.home / 'training').rglob('*') if p.is_file())
        self.assertNotIn(SYNTHETIC_TOKEN, raw)

    def test_archive_can_be_switched_off(self):
        self.agent.env['VILLAGE_TRACE_ARCHIVE'] = '0'
        self.run_cycle('{"name":"idle","arguments":{}}')
        self.assertFalse((self.agent.home / 'training').exists())

    def test_archive_failure_never_stops_the_cycle(self):
        with patch('runtime.append_trace', side_effect=OSError('disk full')):
            self.run_cycle('{"name":"idle","arguments":{}}')
        self.assertEqual(self.agent.state['last_result']['action'], 'idle')


if __name__ == '__main__':
    unittest.main()

"""P157: unambiguous model weaknesses are repaired and marked; nothing ambiguous is guessed."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from decision import decision  # noqa: E402
from village.traces import build_record  # noqa: E402
from web.runtime import Resident  # noqa: E402


def parse(content, allowed=None):
    return decision({'message': {'content': content}}, allowed)


class ArgumentsTextTests(unittest.TestCase):
    def test_live_operator_shapes_are_repaired(self):
        # 06-operator, 2026-10-13 10:35 and 12:05 UTC
        for raw, command in (('{ "name": "execute_bash", "arguments": "cd /var/var/ai-village/operator && ./simple-log-test" }',
                              'cd /var/var/ai-village/operator && ./simple-log-test'),
                             ('{ "name": "execute_bash", "arguments": "ls -ld /var/.../operator/venv" }',
                              'ls -ld /var/.../operator/venv')):
            parsed = parse(raw)
            self.assertEqual(parsed['tool_call'], {'name': 'execute_bash', 'arguments': {'command': command}})
            self.assertEqual(parsed['repaired'], 'arguments_text')  # the path stays wrong: never rewritten

    def test_start_job_and_memory_search_have_one_obvious_field(self):
        self.assertEqual(parse('{"name":"start_job","arguments":"make"}')['tool_call']['arguments'], {'command': 'make'})
        self.assertEqual(parse('{"name":"memory_search","arguments":"chroma"}')['tool_call']['arguments'], {'query': 'chroma'})

    def test_actions_with_more_than_one_meaning_are_still_rejected(self):
        for raw in ('{"name":"board_message","arguments":"hi"}', '{"name":"task_operation","arguments":"claim x"}',
                    '{"name":"execute_bash","arguments":"   "}', '{"name":"execute_bash","arguments":["ls"]}'):
            self.assertEqual(parse(raw)['fallback_reason'], 'arguments must be a JSON object', raw)

    def test_a_repaired_action_still_has_to_be_offered(self):
        parsed = parse('{"name":"execute_bash","arguments":"ls"}', ['idle', 'calendar_operation'])
        self.assertTrue(parsed['fallback_reason'].startswith('action execute_bash is not available'))


class CommandQuotesTests(unittest.TestCase):
    LIVE = ('```json\n{"name": "execute_bash", "arguments": {"command": "cd /var/lib/ai-village/users/methodologist && '
            'bash disk-monitor.sh && echo "=== Log contents ===" && cat /var/log/disk-monitor.log"}}\n```')

    def test_the_live_methodologist_command_runs_with_its_quotes(self):
        parsed = parse(self.LIVE)  # 07-methodologist, 2026-10-13 11:57 UTC
        self.assertEqual(parsed['repaired'], 'command_quotes')
        self.assertIn('echo "=== Log contents ===" && cat', parsed['tool_call']['arguments']['command'])

    def test_the_same_in_a_village_action_block_and_unfenced(self):
        body = '{"name":"execute_bash","arguments":{"command":"echo "a b""}}'
        self.assertEqual(parse('```village-action\n' + body + '\n```')['tool_call']['arguments']['command'], 'echo "a b"')
        self.assertEqual(parse(body)['repaired'], 'command_quotes')

    def test_other_broken_json_is_still_rejected(self):
        for raw in ('{"name":"execute_bash","arguments":{"command":"ls", "timeout_seconds": 5 "x"}}',
                    '{"name":"board_message","arguments":{"message":"say "hi""}}'):
            self.assertIsNotNone(parse(raw).get('fallback_reason'), raw)

    def test_valid_actions_carry_no_repair_mark(self):
        self.assertNotIn('repaired', parse('{"name":"execute_bash","arguments":{"command":"ls"}}'))


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='village-tolerance-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for d in ('board', 'telemetry', 'share'):
            (self.tmp / d).mkdir()
        (self.tmp / 'share' / 'system-prompt.txt').write_text('System prompt')
        identity = self.tmp / 'identity.txt'
        identity.write_text('Test Identity')
        self.agent = Resident(env={
            'AGENT_ID': '01-king', 'AGENT_NAME': 'king', 'AGENT_ROLE': 'king', 'VILLAGE_ROOT': str(self.tmp),
            'OLLAMA_MODEL': 'test-model', 'OLLAMA_URL': 'http://127.0.0.1:9999', 'AGENT_IDENTITY_PROMPT': str(identity),
            'VILLAGE_PAUSE_MARKER': str(self.tmp / 'paused'), 'OLLAMA_NUM_CTX': '16384', 'OLLAMA_THINK_LEVEL': 'medium',
            'VILLAGE_SHARE_DIR': str(self.tmp / 'share')})

    def test_think_is_off_exactly_once_after_an_exhausted_budget(self):
        self.assertEqual(self.agent.cycle_think_level(), 'medium')
        self.agent.state['think_off_once'] = True
        self.assertEqual(self.agent.cycle_think_level(), 'off')
        self.assertEqual(self.agent.cycle_think_level(), 'medium')

    def test_an_exhausted_budget_schedules_one_cycle_without_thinking(self):
        response = {'message': {'content': ''}, 'done_reason': 'length'}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(response).encode()

        with patch('web.runtime.urllib.request.urlopen', return_value=Resp()), \
             patch.object(self.agent, 'memory', return_value={'items': []}):
            self.agent.cycle()
        self.assertTrue(self.agent.state.get('think_off_once'))

    def test_a_repair_is_logged_and_the_resident_is_told_the_exact_form(self):
        previous = self.agent.state.get('last_result')
        self.agent.state['last_result'] = {'ok': True, 'result': 'done'}
        self.agent.note_repair({'repaired': 'arguments_text', 'tool_call': {'name': 'execute_bash'}}, previous)
        self.assertIn('"arguments" as a JSON object', self.agent.state['last_result']['result'])
        self.assertIn('decision_repaired', (self.tmp / 'board' / 'events.jsonl').read_text())

    def test_the_trace_keeps_the_repair_mark(self):
        record = build_record(agent='a', model='m', think_level='off', num_ctx=1, cycle_mode='full', request_id='r',
                              system_sha='s', user='u', output='raw', thinking='', executed=True, metrics={},
                              parsed={'repaired': 'command_quotes', 'tool_call': {'name': 'execute_bash'}},
                              last_result={'ok': True}, timestamp='t')
        self.assertEqual((record['repaired'], record['label']), ('command_quotes', 'ok'))


if __name__ == '__main__':
    unittest.main()

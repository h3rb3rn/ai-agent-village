import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
from observer import outcome_stats
from decision import decision


def embedded(marker):
    text = (ROOT / 'bootstrap-ai-village.sh').read_text()
    return text.split("<<'" + marker + "'\n", 1)[1].split('\n' + marker + '\n', 1)[0]


class ObservatoryTests(unittest.TestCase):
    def test_free_text_is_a_message_not_code(self):
        answer = decision({'message': {'content': 'Ich untersuche das.\n```bash\nls -l\n```'}})
        self.assertEqual(answer['tool_call']['name'], 'board_message')

    def test_explicit_and_legacy_actions(self):
        block = 'Ich prüfe den Speicher.\n```village-action\n{"name":"execute_bash","arguments":{"command":"df -h"}}\n```'
        self.assertEqual(decision({'message': {'content': block}})['tool_call']['arguments']['command'], 'df -h')
        old = '{"tool_call":{"name":"idle","arguments":{}}}'
        self.assertEqual(decision({'message': {'content': old}})['tool_call']['name'], 'idle')

    def test_incomplete_or_ambiguous_action_never_executes(self):
        for content in ('{"name":', '```village-action\n{}', '```village-action\n{}\n```\n```village-action\n{}\n```', '{"name":"execute_bash","arguments":{"command":123}}'):
            parsed = decision({'message': {'content': content}})
            self.assertEqual(parsed['tool_call']['name'], 'board_message')
            self.assertTrue(parsed.get('fallback_reason'))
        parsed = decision({'done_reason': 'length', 'message': {'content': '{"name":"idle"}'}})
        self.assertEqual(parsed['tool_call']['name'], 'board_message')

    def test_explicit_envelope_tolerates_fence_label_variants(self):
        for label in ('','json','bash'):
            parsed=decision({'message':{'content':'```'+label+'\n{"name":"execute_bash","arguments":{"command":"pwd"}}\n```'}})
            self.assertEqual(parsed['tool_call']['name'],'execute_bash')
        parsed=decision({'message':{'content':'```json\n{"action":"claim","task_id":"x"}\n```'}})
        self.assertTrue(parsed.get('fallback_reason'))

    def test_reasoning_tags_never_execute_or_broadcast(self):
        parsed=decision({'message':{'content':'<think>\n```village-action\n{"name":"execute_bash","arguments":{"command":"false"}}\n```\n</think>\nHello'}})
        self.assertEqual(parsed['tool_call']['arguments']['message'],'Hello')
        parsed=decision({'message':{'content':'<think>unfinished'}})
        self.assertTrue(parsed.get('fallback_reason'))

    def test_terminal_explicit_envelope_and_mixed_blocks(self):
        parsed=decision({'message':{'content':'I will act.\n{"name":"execute_bash","arguments":{"command":"pwd"}}'}})
        self.assertEqual(parsed['tool_call']['name'],'execute_bash')
        parsed=decision({'message':{'content':'```json\n{"name":"idle"}\n```\n```village-action\n{"name":"idle"}\n```'}})
        self.assertTrue(parsed.get('fallback_reason'))

    def test_fallback_preserves_safe_communication(self):
        parsed = decision({'message': {'content': '```village-action\n{"name":"execute_bash"}\n```'}})
        self.assertEqual(parsed['tool_call']['name'], 'board_message')
        self.assertNotIn('execute_bash', parsed['tool_call']['arguments']['message'])

    def test_execution_and_repeats_are_distinct(self):
        events = [dict(agent='a', event='command_start', detail='observation=x; command=ls  -l; message=x'),
                  dict(agent='a', event='command_start', detail='command=ls -l; message=x'),
                  dict(agent='a', event='command_result', detail='result=success; output=error word in output'),
                  dict(agent='a', event='command_result', detail='result=failure(1); command=x'),
                  dict(agent='a', event='invalid_decision'), dict(agent='b', event='board_message')]
        stats = outcome_stats(events)
        self.assertEqual(stats['a']['success_rate'], .5)
        self.assertEqual(stats['a']['repeated_attempts'], 1)
        self.assertEqual(stats['a']['invalid'], 1)
        self.assertIsNone(stats['b']['success_rate'])

    def test_embedded_sources_compile(self):
        for marker in ('WEBUI', 'TELEMETRY'):
            compile(embedded(marker), marker, 'exec')

    def test_observer_read_only_and_partial_events(self):
        with tempfile.TemporaryDirectory() as temp:
            os.environ['VILLAGE_ROOT'] = temp
            ns = {'__name__': 'test_webui'}
            exec(compile(embedded('WEBUI'), 'webui', 'exec'), ns)
            log = Path(temp) / 'events'
            log.write_text(json.dumps({'event': 'command_result', 'detail': 'token=secret password:abcdef'})+'\n{broken\n'+json.dumps({'event': 'ok'})+'\n')
            rows = ns['tail_events'](log)
            self.assertEqual(len(rows), 2)
            self.assertNotIn('abcdef', rows[0]['detail'])
            self.assertNotIn('secret', rows[0]['detail'])
            self.assertEqual(ns['telemetry_history'](), [])
            self.assertFalse((Path(temp)/'telemetry/events.sqlite3').exists())

    def test_assets_have_no_external_dependencies(self):
        for ext in ('html', 'css', 'js'):
            text = (ROOT / 'web' / ('observatory.' + ext)).read_text()
            self.assertNotRegex(text, r'https?://|@import|fonts\.google')


if __name__ == '__main__': unittest.main()

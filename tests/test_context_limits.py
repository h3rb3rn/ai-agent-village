"""P124: artificial context and memory limits lifted.

Live evidence (2026-10-10, docs/analysis/STAGNATION-DIAGNOSTIC-2026-10-10.md):
residents with a 262k-token window received a ~12k-token prompt because the
snapshot was capped at 24k characters, knowledge-base hits were trimmed
first, and memory_search feedback was cut [:6000] then [-2500:], discarding
the best-ranked hits. All tests are local and synthetic.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from runtime import (Resident, context_char_budget, clip_head_tail, compact_memory_hits,
                     FEEDBACK_HEAD_CHARS, FEEDBACK_TAIL_CHARS, CONTEXT_BUDGET_FLOOR_CHARS)


class ContextBudgetTests(unittest.TestCase):
    def test_small_windows_keep_the_historical_budget(self):
        # Unchanged for small residents: max(8000, 2*ctx-10000).
        self.assertEqual(context_char_budget({'OLLAMA_NUM_CTX': '8192'}), 8000)
        self.assertEqual(context_char_budget({'OLLAMA_NUM_CTX': '9216'}), 8432)
        self.assertEqual(context_char_budget({'OLLAMA_NUM_CTX': '16384'}), 22768)

    def test_large_windows_use_the_measured_default_ceiling(self):
        # P135: prompt evaluation grows linearly with the snapshot (measured
        # 98 s -> 213 s), so large windows get a default ceiling above the
        # old 24k cap; operators can lift it explicitly.
        from runtime import CONTEXT_DEFAULT_MAX_CHARS
        budget = context_char_budget({'OLLAMA_NUM_CTX': '262144', 'OLLAMA_NUM_PREDICT': '8192'})
        self.assertEqual(budget, CONTEXT_DEFAULT_MAX_CHARS)
        self.assertGreater(budget, 24000)
        lifted = context_char_budget({'OLLAMA_NUM_CTX': '262144', 'OLLAMA_NUM_PREDICT': '8192',
                                      'VILLAGE_CONTEXT_MAX_CHARS': '500000'})
        self.assertGreater(lifted, 24000 * 10)

    def test_budget_never_exceeds_the_window_left_after_output_and_system(self):
        env = {'OLLAMA_NUM_CTX': '32768', 'OLLAMA_NUM_PREDICT': '12288', 'VILLAGE_CONTEXT_MAX_CHARS': '100000'}
        # 2*ctx-10000 = 55536, but only (32768-12288-4096)*3 = 49152 fit.
        self.assertEqual(context_char_budget(env), 49152)

    def test_operator_ceiling_is_respected_but_never_below_floor(self):
        env = {'OLLAMA_NUM_CTX': '262144', 'VILLAGE_CONTEXT_MAX_CHARS': '60000'}
        self.assertEqual(context_char_budget(env), 60000)
        env['VILLAGE_CONTEXT_MAX_CHARS'] = '10'
        self.assertEqual(context_char_budget(env), CONTEXT_BUDGET_FLOOR_CHARS)


class FeedbackClippingTests(unittest.TestCase):
    def test_short_results_are_untouched(self):
        self.assertEqual(clip_head_tail('abc'), 'abc')

    def test_long_results_keep_head_and_tail(self):
        text = 'HEAD' + 'x' * 50000 + 'TAIL'
        out = clip_head_tail(text)
        self.assertTrue(out.startswith('HEAD'))
        self.assertTrue(out.endswith('TAIL'))
        self.assertIn('chars omitted', out)
        self.assertLess(len(out), FEEDBACK_HEAD_CHARS + FEEDBACK_TAIL_CHARS + 100)

    def test_memory_hits_keep_every_ranked_item(self):
        items = [{'id': f'm{i}', 'agent': '02-b', 'scope': 'shared', 'kind': 'lesson',
                  'content': f'hit-{i} ' + 'y' * 3000} for i in range(8)]
        out = compact_memory_hits({'query': 'q', 'items': items, 'backend': 'lexical'})
        self.assertEqual(out['count'], 8)
        self.assertEqual([x['id'] for x in out['items']], [f'm{i}' for i in range(8)])
        self.assertTrue(out['items'][0]['content'].startswith('hit-0'))
        # Clipped enough that the whole set fits the stored head+tail bound.
        self.assertLessEqual(len(json.dumps(out)), FEEDBACK_HEAD_CHARS + FEEDBACK_TAIL_CHARS)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        self.env = dict(os.environ, AGENT_ID='01-a', AGENT_NAME='a', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), VILLAGE_MAX_OUTPUT_BYTES='4096',
                        VILLAGE_COMMAND_TIMEOUT_SECONDS='2')
        for target in ('runtime.calendar_is_planning_day', 'runtime.gazette_deadline_passed'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False)
        p.start(); self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_best_memory_search_hit_reaches_the_next_prompt(self):
        agent = Resident(dict(self.env, OLLAMA_NUM_CTX='16384'))
        items = [{'id': f'm{i}', 'agent': '02-b', 'scope': 'shared', 'kind': 'lesson',
                  'content': f'ranked-{i} ' + 'z' * 2000} for i in range(8)]
        with patch.object(agent, 'memory', return_value={'query': 'q', 'items': items}):
            agent.execute({'tool_call': {'name': 'memory_search', 'arguments': {'query': 'q'}}})
            ctx = json.loads(agent.snapshot())
        shown = ctx['last_action_feedback']['result']
        self.assertIn('ranked-0', shown)
        self.assertIn('ranked-7', shown)

    def test_memory_is_trimmed_after_projects_and_from_the_tail(self):
        agent = Resident(dict(self.env, OLLAMA_NUM_CTX='9216'))
        big_projects = [{'id': str(i), 'title': 'p' * 700, 'owner': '02-b', 'status': 'open'} for i in range(20)]
        agent.tasks.path.write_text(json.dumps(big_projects))
        hits = [{'id': f'm{i}', 'agent': '02-b', 'scope': 'shared', 'content': f'mem-{i} ' + 'k' * 300}
                for i in range(3)]
        with patch.object(agent, 'memory', return_value={'items': hits}):
            ctx = json.loads(agent.snapshot())
        self.assertLess(len(ctx.get('projects', [])), 20)  # projects absorbed the squeeze
        kept = [x['id'] for x in ctx.get('retrieved_memory_untrusted', [])]
        self.assertTrue(kept, 'memory hits must not be the first casualty')
        self.assertEqual(kept[0], 'm0')  # best-ranked hit is the last to go

    def test_retrieval_limit_and_recent_results_widened(self):
        agent = Resident(dict(self.env, OLLAMA_NUM_CTX='262144'))
        with patch.object(agent, 'memory', return_value={'items': []}) as mem:
            agent.snapshot()
        self.assertEqual(mem.call_args[0][1]['limit'], 8)
        tuned = Resident(dict(self.env, OLLAMA_NUM_CTX='262144', VILLAGE_RETRIEVAL_LIMIT='12'))
        with patch.object(tuned, 'memory', return_value={'items': []}) as mem:
            tuned.snapshot()
        self.assertEqual(mem.call_args[0][1]['limit'], 12)
        events = self.root / 'board' / 'events.jsonl'
        rows = [json.dumps({'timestamp': f'2026-10-10T10:00:{i:02d}+00:00', 'agent': '01-a',
                            'event': 'command_result', 'detail': f'r{i}'}) for i in range(12)]
        events.write_text('\n'.join(rows) + '\n')
        with patch.object(agent, 'memory', return_value={'items': []}):
            ctx = json.loads(agent.snapshot())
        self.assertEqual([x['detail'] for x in ctx['own_recent_results']], [f'r{i}' for i in range(4, 12)])


if __name__ == '__main__':
    unittest.main()

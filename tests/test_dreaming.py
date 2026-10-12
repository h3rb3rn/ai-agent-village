"""P147/P148: residents plan all 24 hours and dream once a day. Local, synthetic, no network."""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'web'))
from runtime import Resident
from village.dreaming import (DREAM_ACTIONS, archive_forgotten, default_window, duplicate_pairs, in_window,
                              planned_window, trace_summary)


class WindowTests(unittest.TestCase):
    def test_default_windows_are_staggered_across_the_whole_day(self):
        starts = [default_window(f'0{i}-x')[0] for i in range(1, 10)]
        self.assertEqual(len(set(starts)), 9)
        self.assertEqual(starts[0], 0)
        self.assertEqual(starts[-1], 8 * 160)
        self.assertEqual(default_window('05-x', 90), (640, 730))

    def test_window_may_cross_midnight(self):
        self.assertTrue(in_window(1430, (1400, 1490)))
        self.assertTrue(in_window(20, (1400, 1490)))
        self.assertFalse(in_window(60, (1400, 1490)))
        self.assertTrue(in_window(100, (100, 190)))
        self.assertFalse(in_window(190, (100, 190)))

    def test_own_planned_dream_event_wins(self):
        events = [{'kind': 'focus', 'start_time': '01:00', 'duration_minutes': 60, 'status': 'planned'},
                  {'kind': 'dream', 'start_time': '03:30', 'duration_minutes': 120, 'status': 'cancelled'},
                  {'kind': 'dream', 'start_time': '04:00', 'duration_minutes': 60, 'status': 'planned'}]
        self.assertEqual(planned_window(events), (240, 300))
        self.assertIsNone(planned_window(events[:2]))


class HelperTests(unittest.TestCase):
    def test_duplicate_pairs_find_overlapping_wording(self):
        items = [{'id': 'a', 'content': 'disk usage on /var is 41 percent measured with df -h'},
                 {'id': 'b', 'content': 'measured with df -h: disk usage on /var is 41 percent'},
                 {'id': 'c', 'content': 'podman build works with the sm_50 base image'}]
        self.assertEqual(duplicate_pairs(items), [['a', 'b']])

    def test_trace_summary_counts_labels_and_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / 'training' / 'traces'; d.mkdir(parents=True)
            rows = [{'label': 'ok'}, {'label': 'failed', 'result': 'No such file'},
                    {'label': 'failed', 'result': 'No such file'}, {'label': 'invalid_format', 'fallback_reason': 'bad'}]
            (d / '2026-10-12.jsonl').write_text('\n'.join(json.dumps(r) for r in rows))
            out = trace_summary(Path(tmp), '2026-10-12')
            self.assertEqual(out['cycles'], 4)
            self.assertEqual(out['labels'], {'ok': 1, 'failed': 2, 'invalid_format': 1})
            self.assertEqual(out['top_failures'][0], {'reason': 'No such file', 'count': 2})
            self.assertEqual(trace_summary(Path(tmp), '2026-10-13'), {})

    def test_forgotten_records_are_archived_privately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = archive_forgotten(Path(tmp), {'id': 'm1', 'content': 'old'}, 'duplicate of m2', 't')
            archive_forgotten(Path(tmp), {'id': 'm3', 'content': 'older'}, 'refuted', 't')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            rows = [json.loads(l) for l in path.read_text().splitlines()]
            self.assertEqual([r['record']['id'] for r in rows], ['m1', 'm3'])
            self.assertEqual(rows[0]['reason'], 'duplicate of m2')


class FakeGateway:
    """In-memory stand-in for the memory gateway (GET/PATCH/DELETE, ?mine=1 paging)."""

    def __init__(self, agent, n=20):
        self.records = {f'm{i:02d}': dict(id=f'm{i:02d}', agent=agent, kind='observation', scope='private',
                                          confidence=0.8, created_at=f'2026-10-0{1 + i // 10}T00:00:{i:02d}Z',
                                          content=f'note {i}') for i in range(n)}
        self.records['foreign'] = dict(id='foreign', agent='03-b', created_at='2026-10-01T00:00:00Z', content='x')
        self.calls = []

    def __call__(self, endpoint, value=None, method=None):
        self.calls.append((method or ('POST' if value is not None else 'GET'), endpoint, value))
        if endpoint.startswith('/v1/memories?mine=1'):
            from urllib.parse import parse_qs, urlsplit, unquote
            q = parse_qs(urlsplit(endpoint).query)
            after = unquote(q.get('after', [''])[0])
            limit = int(q['limit'][0])
            own = sorted((r for r in self.records.values() if r['agent'] == '02-a'),
                         key=lambda r: f"{r['created_at']}|{r['id']}")
            items = [r for r in own if f"{r['created_at']}|{r['id']}" > after][:limit]
            return {'items': items, 'total_own': len(own)}
        if endpoint.startswith('/v1/memories/'):
            mem_id = endpoint.rsplit('/', 1)[1]
            if method == 'DELETE':
                self.records.pop(mem_id)
                return {'ok': True, 'deleted_id': mem_id}
            if method == 'PATCH':
                self.records[mem_id].update(value)
                return self.records[mem_id]
            return dict(self.records[mem_id])
        return {'items': []}


class ResidentDreamTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        self.env = dict(os.environ, AGENT_ID='02-a', AGENT_NAME='a', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), VILLAGE_MAX_OUTPUT_BYTES='4096',
                        VILLAGE_COMMAND_TIMEOUT_SECONDS='2', OLLAMA_MODEL='qwen3.5:4b')
        for target in ('runtime.calendar_is_planning_day', 'runtime.gazette_deadline_passed'):
            p = patch(target, return_value=False); p.start(); self.addCleanup(p.stop)
        p = patch.object(Resident, 'calendar_pending_gazette_writing_slot', return_value=False)
        p.start(); self.addCleanup(p.stop)
        self.agent = Resident(self.env)
        self.gateway = FakeGateway('02-a')
        p = patch.object(self.agent, 'memory', side_effect=self.gateway); p.start(); self.addCleanup(p.stop)
        self.window = default_window('02-a')  # 02:40-04:10 UTC

    def tearDown(self):
        self.tmp.cleanup()

    def dream(self, on=True):
        self.agent._dreaming = on

    def act(self, name, **args):
        self.agent.execute({'tool_call': {'name': name, 'arguments': args}})
        return self.agent.state['last_result']

    def test_dreaming_follows_the_window_and_yields_to_an_open_meeting(self):
        self.assertTrue(self.agent.is_dreaming(self.window[0] + 10))
        self.assertFalse(self.agent.is_dreaming(self.window[1] + 10))
        self.agent.meetings.schedule('standup', 'status', '2026-10-12T02:50:00Z', meeting_id='m1')
        self.assertFalse(self.agent.is_dreaming(self.window[0] + 10))
        self.agent.env['VILLAGE_DREAM'] = '0'
        self.assertIsNone(self.agent.dream_window())

    def test_planned_dream_event_moves_the_window(self):
        from village.calendar import today
        self.agent.calendar.create_event('02-a', 'Dream', 'dream', today(), '12:00', 60)
        self.assertEqual(self.agent.dream_window(), (720, 780))

    def test_dream_restricts_actions_and_hides_forget_while_awake(self):
        self.dream(True)
        self.assertEqual(set(self.agent.effective_allowed_actions()) - set(DREAM_ACTIONS), set())
        self.assertIn('memory_forget', self.agent.effective_allowed_actions())
        self.dream(False)
        awake = self.agent.effective_allowed_actions()
        self.assertNotIn('memory_forget', awake)
        self.assertNotIn('memory_update', awake)
        self.assertIn('execute_bash', awake)

    def test_dream_snapshot_reviews_own_memories_in_batches(self):
        self.dream(True)
        ctx = json.loads(self.agent.dream_snapshot())
        self.assertEqual(ctx['cycle_mode'], 'dream')
        self.assertEqual([m['id'] for m in ctx['memories_to_review']], [f'm{i:02d}' for i in range(8)])
        self.assertEqual(ctx['own_memories_total'], 20)
        self.assertNotIn('projects', ctx)
        self.assertIn('memory_forget', ctx['dream_actions'])
        # Same batch for up to three turns (merge = update + forget), then the next one.
        ids = lambda: [m['id'] for m in json.loads(self.agent.dream_snapshot())['memories_to_review']]
        self.assertEqual(ids()[0], 'm00')
        self.assertEqual(ids()[0], 'm00')
        self.assertEqual(ids()[0], 'm08')

    def test_idle_moves_on_to_the_next_batch_and_the_end_wraps(self):
        self.dream(True)
        self.agent.dream_snapshot()
        self.act('idle')
        self.assertEqual(json.loads(self.agent.dream_snapshot())['memories_to_review'][0]['id'], 'm08')
        self.act('idle')
        self.assertEqual(json.loads(self.agent.dream_snapshot())['memories_to_review'][0]['id'], 'm16')
        self.act('idle')
        self.assertEqual(json.loads(self.agent.dream_snapshot())['memories_to_review'][0]['id'], 'm00')

    def test_dream_delivers_no_messages(self):
        self.agent.state['seen_board_epoch'] = 5
        self.agent.tasks.store.post_inbox_message(source='direct', sender='03-b', recipient='02-a', content='hi')
        self.dream(True)
        self.agent.dream_snapshot()
        self.assertEqual(self.agent.delivered_inbox_ids, [])
        self.assertEqual(self.agent.pending_cursor, 5)

    def test_forget_archives_then_deletes_only_own_records_and_only_while_dreaming(self):
        self.assertFalse(self.act('memory_forget', id='m01', reason='duplicate of m02')['ok'])  # awake
        self.dream(True)
        self.assertTrue(self.act('memory_forget', id='m01', reason='duplicate of m02')['ok'])
        self.assertNotIn('m01', self.gateway.records)
        archived = (self.agent.home / 'training' / 'forgotten.jsonl').read_text()
        self.assertIn('"id": "m01"', archived)
        self.assertIn('duplicate of m02', archived)
        res = self.act('memory_forget', id='foreign', reason='not mine')
        self.assertFalse(res['ok'])
        self.assertIn('own memories', res['result'])
        self.assertIn('foreign', self.gateway.records)

    def test_update_patches_own_record(self):
        self.dream(True)
        self.assertTrue(self.act('memory_update', id='m02', content='note 1 and note 2 merged', confidence=0.6)['ok'])
        self.assertEqual(self.gateway.records['m02']['content'], 'note 1 and note 2 merged')
        self.assertIn(('PATCH', '/v1/memories/m02', {'content': 'note 1 and note 2 merged', 'confidence': 0.6}),
                      self.gateway.calls)
        self.assertFalse(self.act('memory_update', id='m02')['ok'])  # nothing to change

    def test_full_cycle_dreams_and_records_mode(self):
        import io
        from decision import decision
        self.agent.env.update(OLLAMA_URL='http://localhost:1234', AGENT_IDENTITY_PROMPT='/test/identity',
                              VILLAGE_ACTION_FORMAT='schema')
        self.agent.pending_cursor = 1; self.agent.pending_organic_cursor = 2
        answer = {'done_reason': 'stop', 'message': {'content': json.dumps(
            {'name': 'memory_forget', 'arguments': {'id': 'm00', 'reason': 'duplicate'}})}}
        with patch('runtime.minute_of_day', return_value=self.window[0] + 5), \
                patch('runtime.Path.read_text', return_value='Test identity'), \
                patch('runtime.urllib.request.urlopen', return_value=io.BytesIO(json.dumps(answer).encode())) as req:
            self.agent.cycle()
        payload = json.loads(req.call_args.args[0].data)
        user = payload['messages'][1]['content']
        self.assertIn('"cycle_mode": "dream"', user)
        self.assertTrue(self.agent.state['dreaming'])
        self.assertTrue(self.agent.state['last_result']['ok'], self.agent.state['last_result'])
        self.assertNotIn('m00', self.gateway.records)
        events = (self.root / 'telemetry' / 'agent-events.jsonl').read_text()
        self.assertIn('dream_started', events)
        self.assertIn('mode=dream', events)
        # Awake actions are not accepted while dreaming.
        self.assertIn('not available', decision({'message': {'content': json.dumps(
            {'name': 'execute_bash', 'arguments': {'command': 'ls'}})}}, self.agent.effective_allowed_actions())['fallback_reason'])


class ParserTests(unittest.TestCase):
    def test_forget_needs_a_reason(self):
        from decision import decision
        parse = lambda args: decision({'message': {'content': json.dumps({'name': 'memory_forget', 'arguments': args})}})
        self.assertEqual(parse({'id': 'm1'})['fallback_reason'], 'missing action argument')
        self.assertFalse(parse({'id': 'm1', 'reason': 'duplicate'}).get('fallback_reason'))


if __name__ == '__main__':
    unittest.main()

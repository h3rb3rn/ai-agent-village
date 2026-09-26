"""P21.11: runtime reliability (wakeups, receipts, rejected output, prose routing)."""
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from village.collaboration import CooperationCheckpoint, assess
from village.coordinator import CoordinationStore
from web.decision import decision
from web.runtime import Resident, tail


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='village-rel-'))
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        (self.root / 'identity.txt').write_text('identity')
        self.env = dict(AGENT_ID='01-a', AGENT_NAME='a', AGENT_ROLE='resident', VILLAGE_ROOT=str(self.root),
                        AGENT_IDENTITY_PROMPT=str(self.root / 'identity.txt'), OLLAMA_MODEL='m')
        self.agent = Resident(self.env)
        self.store = self.agent.tasks.store

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # --- wakeups -------------------------------------------------------
    def test_broadcast_never_wakes_agent(self):
        self.store.post_inbox_message('board', '02-b', 'hello all')
        self.assertEqual(self.store.fetch_undelivered_wakeups('01-a'), [])
        self.assertEqual(self.store.fetch_undelivered_wakeups('02-b'), [])

    def test_direct_message_wakes_until_delivered_not_until_acked(self):
        m = self.store.post_inbox_message('direct', '02-b', 'question', recipient='01-a')
        self.assertEqual(len(self.store.fetch_undelivered_wakeups('01-a')), 1)
        self.store.mark_messages_delivered('01-a', [m['id']])
        # Delivered but not acknowledged (e.g. rejected model reply): no wake loop.
        self.assertEqual(self.store.fetch_undelivered_wakeups('01-a'), [])
        self.assertEqual(len(self.store.fetch_unacknowledged_messages('01-a')), 1)

    def test_own_direct_message_does_not_wake_sender(self):
        self.store.post_inbox_message('direct', '01-a', 'note', recipient='01-a')
        self.assertEqual(self.store.fetch_undelivered_wakeups('01-a'), [])

    def test_run_loop_sleeps_full_delay_despite_broadcast(self):
        self.store.post_inbox_message('board', '02-b', 'hello all')
        count = 0
        def fake_cycle():
            nonlocal count
            count += 1
            if count >= 2: self.agent.stopping = True
        self.agent.cycle = fake_cycle
        self.agent.compute_cycle_delay = lambda: 3
        start = time.monotonic(); self.agent.run(); elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 2.9)

    def test_invalid_streak_backoff_not_defeated_by_direct_message(self):
        self.agent.state['invalid_streak'] = 3
        self.store.post_inbox_message('direct', '02-b', 'ping', recipient='01-a')
        count = 0
        def fake_cycle():
            nonlocal count
            count += 1
            if count >= 2: self.agent.stopping = True
        self.agent.cycle = fake_cycle
        self.agent.compute_cycle_delay = lambda: 3
        start = time.monotonic(); self.agent.run(); self.assertGreaterEqual(time.monotonic() - start, 2.9)

    # --- receipts / foreign keys --------------------------------------
    def test_receipts_for_unknown_ids_are_ignored(self):
        self.store.mark_messages_delivered('01-a', ['msg_deadbeef'])
        self.assertEqual(self.store.acknowledge_messages('01-a', ['msg_deadbeef']), 0)

    def test_snapshot_survives_synthetic_message_id(self):
        events = self.root / 'board' / 'events.jsonl'
        events.write_text(json.dumps({'timestamp': '2999-01-01T00:00:00+00:00', 'agent': '02-b', 'event': 'board_message',
                                      'detail': 'to=01-a; message=legacy without id'}) + '\n')
        snap = json.loads(self.agent.snapshot())
        self.assertTrue(snap['untrusted_direct_messages'])

    def test_execute_reports_database_errors_instead_of_raising(self):
        def boom(*a, **k): raise sqlite3.IntegrityError('FOREIGN KEY constraint failed')
        self.agent.tasks.operate = boom
        self.agent.execute({'tool_call': {'name': 'task_operation', 'arguments': {'action': 'claim', 'task_id': 'x'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('IntegrityError', self.agent.state['last_result']['result'])

    # --- rejected output ----------------------------------------------
    def test_rejected_output_is_not_published(self):
        (self.agent.home / 'last-response.json').write_text(json.dumps({'content': 'I will run ```village-action {"a"'}))
        self.agent.execute(decision({'message': {'content': '```village-action\n{"name":"idle"}\n```\n```village-action\n{"name":"idle"}\n```'}}))
        text = (self.root / 'board' / 'events.jsonl').read_text()
        self.assertNotIn('unexecuted proposal', text)
        self.assertNotIn('board_message', text)
        self.assertEqual(self.agent.state['invalid_streak'], 1)

    # --- consult routing and visibility --------------------------------
    def test_prose_is_routed_to_selected_peer_at_consult(self):
        (self.root / 'runtime-peers.json').write_text('[]')
        self.agent.current_collaboration_checkpoint = CooperationCheckpoint('consult', 'board_message', 'ask', peer_id='02-b')
        parsed = decision({'message': {'content': 'Can you reproduce my measurement of the GPU idle load?'}})
        self.assertTrue(parsed['prose'])
        with unittest_patch_peers(['02-b']):
            self.agent.execute(parsed)
        self.assertTrue(self.agent.state['last_result']['ok'], self.agent.state['last_result'])
        msgs = self.store.fetch_unacknowledged_messages('02-b', source='direct')
        self.assertEqual(len(msgs), 1)

    def test_explicit_broadcast_still_rejected_at_consult(self):
        self.agent.current_collaboration_checkpoint = CooperationCheckpoint('consult', 'board_message', 'ask', peer_id='02-b')
        self.agent.execute({'tool_call': {'name': 'board_message', 'arguments': {'recipient': 'ALL', 'message': 'x'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])

    def test_direct_message_is_visible_as_metadata_and_satisfies_consult(self):
        self.store.post_inbox_message('direct', '01-a', 'secret question text', recipient='02-b')
        raw = (self.root / 'board' / 'events.jsonl').read_text()
        self.assertIn('direct_message', raw)
        self.assertNotIn('secret question text', raw)
        events = tail(self.root / 'board' / 'events.jsonl')
        # orient comes first without a memory search; add one and re-check.
        events.append({'agent': '01-a', 'event': 'memory_result', 'detail': 'result=success; action=memory_search; matches=1',
                       'timestamp': events[0]['timestamp']})
        self.assertNotEqual(assess(events, '01-a', has_active_task=True, peer_id='02-b').stage, 'consult')


def unittest_patch_peers(ids):
    from unittest.mock import patch
    import web.runtime as rt
    real = rt.read_json
    def fake(path, default):
        if str(path).endswith('runtime-peers.json'): return [{'id': i} for i in ids]
        return real(path, default)
    return patch.object(rt, 'read_json', fake)


if __name__ == '__main__':
    unittest.main()

"""P21.11: runtime reliability (wakeups, receipts, rejected output, prose routing)."""
import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import web.runtime as rt
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


class TolerantParsingTests(unittest.TestCase):
    def content(self, text):
        return decision({'message': {'content': text}})

    def test_trailing_prose_after_complete_envelope_is_accepted(self):
        parsed = self.content('{"name":"board_message","arguments":{"message":"hi"}}\nHope that helps!')
        self.assertNotIn('fallback_reason', parsed)
        self.assertEqual(parsed['tool_call']['name'], 'board_message')

    def test_trailing_prose_inside_block_is_accepted(self):
        parsed = self.content('```village-action\n{"name":"idle","arguments":{}} done\n```')
        self.assertEqual(parsed['tool_call']['name'], 'idle')

    def test_second_object_after_first_is_still_rejected(self):
        parsed = self.content('{"name":"idle","arguments":{}} then {"name":"execute_bash","arguments":{"command":"ls"}}')
        self.assertEqual(parsed['fallback_reason'], 'incomplete legacy action object')

    def test_truncated_object_is_still_rejected(self):
        self.assertIn('fallback_reason', self.content('{"name":"board_message","arguments":{"message":"hi'))


class HostObservedAliasTests(unittest.TestCase):
    """Regression fixtures captured verbatim from N06-M10 on 2026-09-27 during the M1
    measurement window. Three model families (llama3.2, granite4.2, qwen3.5) used
    "content" instead of "message" for board_message; nemotron-3-nano used the
    action name "board_operation". These were the dominant cause of invalid_decision
    in that window and must be aliased, never silently dropped."""

    def test_chronicler_content_field_is_accepted_as_message(self):
        content = '{"name":"board_message","arguments":{"recipient":"03-librarian","content":"Librarian, I\'d like to start by asking about the host memory reserve and possible solutions to ensure the recommended reserve of 4096 MiB is met."}}'
        parsed = decision({'message': {'content': content}})
        self.assertNotIn('fallback_reason', parsed, parsed)
        self.assertEqual(parsed['tool_call']['name'], 'board_message')
        self.assertIn('memory reserve', parsed['tool_call']['arguments']['message'])
        self.assertNotIn('content', parsed['tool_call']['arguments'])

    def test_librarian_pretty_printed_content_field_is_accepted(self):
        content = '{\n  "name": "board_message",\n  "arguments": {\n    "recipient": "05-interpreter",\n    "content": "Please provide the exact ChromaDB version requirement."\n  }\n}'
        parsed = decision({'message': {'content': content}})
        self.assertNotIn('fallback_reason', parsed, parsed)
        self.assertEqual(parsed['tool_call']['arguments']['message'], 'Please provide the exact ChromaDB version requirement.')

    def test_explorer_content_field_inside_action_block_is_accepted(self):
        content = '```village-action\n{"name":"board_message","arguments":{"recipient":"07-methodologist","content":"Methodologist: proceeding with install.","reply_to":"msg_1"}}\n```'
        parsed = decision({'message': {'content': content}})
        self.assertNotIn('fallback_reason', parsed, parsed)
        self.assertEqual(parsed['tool_call']['arguments']['message'], 'Methodologist: proceeding with install.')
        self.assertEqual(parsed['tool_call']['arguments']['reply_to'], 'msg_1')

    def test_operator_board_operation_name_is_aliased(self):
        content = '{\n  "name": "board_operation",\n  "arguments": {\n    "recipient": "05-interpreter",\n    "content": "Could you please provide the exact ChromaDB version requirement?"\n  }\n}'
        parsed = decision({'message': {'content': content}})
        self.assertNotIn('fallback_reason', parsed, parsed)
        self.assertEqual(parsed['tool_call']['name'], 'board_message')
        self.assertEqual(parsed['tool_call']['arguments']['recipient'], '05-interpreter')

    def test_real_message_field_still_wins_over_content_if_both_present(self):
        parsed = decision({'message': {'content': '{"name":"board_message","arguments":{"message":"real","content":"decoy"}}'}})
        self.assertEqual(parsed['tool_call']['arguments']['message'], 'real')

    def test_empty_content_does_not_fabricate_a_message(self):
        parsed = decision({'message': {'content': '{"name":"board_message","arguments":{"content":""}}'}})
        self.assertEqual(parsed['fallback_reason'], 'missing action argument')

    def test_unrelated_unknown_action_name_is_not_silently_aliased(self):
        parsed = decision({'message': {'content': '{"name":"send_message","arguments":{"content":"x"}}'}})
        self.assertIn('unknown action', parsed['fallback_reason'])


class CalcOperationRuntimeTests(unittest.TestCase):
    """P30: calc_operation wires village/tools.py into the resident action contract."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='village-calc-'))
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        (self.root / 'identity.txt').write_text('identity')
        self.env = dict(AGENT_ID='08-logician', AGENT_NAME='logician', AGENT_ROLE='resident', VILLAGE_ROOT=str(self.root),
                        AGENT_IDENTITY_PROMPT=str(self.root / 'identity.txt'), OLLAMA_MODEL='m')
        self.agent = Resident(self.env)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_subnet_info_via_action_contract(self):
        self.agent.execute({'tool_call': {'name': 'calc_operation', 'arguments': {'tool': 'subnet_info', 'cidr': '10.10.10.0/28'}}})
        self.assertTrue(self.agent.state['last_result']['ok'], self.agent.state['last_result'])
        self.assertIn('"usable_hosts": 14', self.agent.state['last_result']['result'])

    def test_invalid_tool_argument_is_reported_not_crashed(self):
        self.agent.execute({'tool_call': {'name': 'calc_operation', 'arguments': {'tool': 'subnet_info', 'cidr': 'garbage'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('invalid CIDR', self.agent.state['last_result']['result'])

    def test_unexpected_argument_name_is_reported_not_crashed(self):
        self.agent.execute({'tool_call': {'name': 'calc_operation', 'arguments': {'tool': 'calc', 'expr': '1+1'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('invalid arguments', self.agent.state['last_result']['result'])

    def test_calc_operation_parses_through_decision(self):
        content = '{"name":"calc_operation","arguments":{"tool":"unit_convert","value":13216,"from_unit":"mib","to_unit":"gib"}}'
        parsed = decision({'message': {'content': content}})
        self.assertNotIn('fallback_reason', parsed, parsed)
        self.agent.execute(parsed)
        self.assertTrue(self.agent.state['last_result']['ok'])
        self.assertIn('12.906', self.agent.state['last_result']['result'])


class CapabilitySummaryTests(unittest.TestCase):
    """P38: village/containers.py::describe_agent_capabilities() (P19) existed
    but was never surfaced to a resident's own prompt - an agent cannot use a
    habitat it does not know it has. capability_summary() closes that gap."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='village-caps-'))
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        (self.root / 'identity.txt').write_text('identity')
        (self.root / 'system-prompt.txt').write_text('FULL')
        self.env = dict(AGENT_ID='02-explorer', AGENT_NAME='explorer', AGENT_ROLE='resident', VILLAGE_ROOT=str(self.root),
                        AGENT_IDENTITY_PROMPT=str(self.root / 'identity.txt'), OLLAMA_MODEL='m',
                        OLLAMA_URL='http://127.0.0.1:1', VILLAGE_SHARE_DIR=str(self.root))
        self.agent = Resident(self.env)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _fake_desc(self, **overrides):
        desc = {
            'agent': '02-explorer', 'user': 'village_explorer',
            'storage': {'private_directory': '/priv', 'shared_directory': '/shared', 'shared_access': 'read-write'},
            'tools': {'curl': '/usr/bin/curl', 'python3': None},
            'containers': {'rootless_supported': True},
            'gpu': {'gpu_compute_ready': False},
            'authority': {'request_channel': "village-authority request <capability> --reason '<reason>'"},
        }
        desc.update(overrides)
        return desc

    def test_summary_reports_storage_tools_and_readiness(self):
        with patch('village.containers.audit_host_environment', return_value=object()), \
             patch('village.containers.describe_agent_capabilities', return_value=self._fake_desc()):
            summary = self.agent.capability_summary()
        self.assertIn('/priv', summary)
        self.assertIn('/shared', summary)
        self.assertIn('curl', summary)
        self.assertNotIn('python3', summary)  # tools with no resolved path are excluded
        self.assertIn('Rootless containers: ready', summary)
        self.assertIn('GPU compute: not ready yet', summary)
        self.assertIn('village-authority request', summary)

    def test_summary_is_cached_within_the_ttl(self):
        with patch('village.containers.audit_host_environment', return_value=object()) as audit, \
             patch('village.containers.describe_agent_capabilities', return_value=self._fake_desc()):
            first = self.agent.capability_summary(ttl_seconds=1800)
            second = self.agent.capability_summary(ttl_seconds=1800)
        self.assertEqual(first, second)
        audit.assert_called_once()

    def test_audit_failure_degrades_to_an_honest_unknown_not_a_crash(self):
        with patch('village.containers.audit_host_environment', side_effect=OSError('nvidia-smi missing')):
            summary = self.agent.capability_summary()
        self.assertIn('unknown this cycle', summary)
        self.assertIn('OSError', summary)

    def test_summary_reaches_the_actual_outgoing_prompt(self):
        captured = {}

        def fake_urlopen(request, timeout=0):
            captured['request'] = request
            raise OSError('blocked for test - only the outgoing request matters here')

        with patch('village.containers.audit_host_environment', return_value=object()), \
             patch('village.containers.describe_agent_capabilities', return_value=self._fake_desc()), \
             patch.object(self.agent, 'memory', return_value={'items': [], 'id': 'mem1'}), \
             patch('urllib.request.urlopen', side_effect=fake_urlopen):
            self.agent.cycle()
        prompt = json.loads(captured['request'].data)['messages'][0]['content']
        self.assertIn('/priv', prompt)
        self.assertIn('village-authority request', prompt)


class MandatoryKnowledgebaseGateTests(unittest.TestCase):
    """P31: knowledgebase_gate=mandatory is an explicit, documented operator
    intervention (docs/evidence/P31.md), not a silent default. Default policy
    (advisory) must be byte-for-byte unaffected."""

    def make(self, gate):
        root = Path(tempfile.mkdtemp(prefix='village-kb-'))
        (root / 'board').mkdir(); (root / 'telemetry').mkdir()
        (root / 'identity.txt').write_text('identity')
        (root / 'policy.json').write_text(json.dumps({'defaults': {'knowledgebase_gate': gate}}))
        env = dict(AGENT_ID='02-explorer', AGENT_NAME='explorer', AGENT_ROLE='resident', VILLAGE_ROOT=str(root),
                   AGENT_IDENTITY_PROMPT=str(root / 'identity.txt'), OLLAMA_MODEL='m',
                   VILLAGE_POLICY_FILE='/nonexistent', VILLAGE_POLICY_LOCAL_FILE=str(root / 'policy.json'))
        agent = Resident(env)
        patcher = patch.object(agent, 'memory', return_value={'items': [], 'id': 'mem1'})
        patcher.start(); self.addCleanup(patcher.stop)
        agent.tasks.operate('02-explorer', dict(action='create', title='t', success_criterion='c'))
        task_id = json.loads((root / 'board/work-items.json').read_text())[0]['id']
        agent.tasks.operate('02-explorer', dict(action='claim', task_id=task_id))
        return agent, root

    def step(self, agent, name, **args):
        agent.snapshot()  # recompute current_collaboration_checkpoint from real board events, like cycle() does
        agent.execute({'tool_call': {'name': name, 'arguments': args}})

    def test_advisory_default_still_allows_three_attempts_before_blocking(self):
        agent, root = self.make('advisory')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for i in range(2):
            self.step(agent, 'execute_bash', command=f'echo {i}')
            self.assertTrue(agent.state['last_result']['ok'], agent.state['last_result'])
        self.step(agent, 'execute_bash', command='echo third')
        self.assertFalse(agent.state['last_result']['ok'])

    def test_mandatory_blocks_on_first_attempt_without_orient(self):
        agent, root = self.make('mandatory')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.step(agent, 'execute_bash', command='echo hi')
        self.assertFalse(agent.state['last_result']['ok'], agent.state['last_result'])
        self.assertIn('memory_search', agent.state['last_result']['result'])

    def test_mandatory_record_requires_shared_scope_not_private(self):
        agent, root = self.make('mandatory')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.step(agent, 'memory_search', query='x')
        self.step(agent, 'execute_bash', command='echo done')
        self.assertTrue(agent.state['last_result']['ok'], agent.state['last_result'])
        # Private record does not satisfy the mandatory (shared) record checkpoint.
        self.step(agent, 'memory_remember', content='x', scope='private')
        self.step(agent, 'execute_bash', command='echo again')
        self.assertFalse(agent.state['last_result']['ok'], agent.state['last_result'])
        self.assertIn('share', agent.state['last_result']['result'].lower())
        # A shared record does satisfy it.
        self.step(agent, 'memory_remember', content='x', scope='shared')
        self.step(agent, 'execute_bash', command='echo third')
        self.assertTrue(agent.state['last_result']['ok'], agent.state['last_result'])

    def test_invalid_gate_value_falls_back_to_advisory(self):
        agent, root = self.make('bogus-value')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.assertEqual(agent.policy.knowledgebase_gate, 'advisory')


class ForeignHomeGuardTests(unittest.TestCase):
    """Board review 2026-09-27: King/Operator/Methodologist tried to fix 08-logician's
    venv by running commands against his private path from their OWN account, and
    03-librarian repeatedly announced writing to .../users/operator/schema.md. Both
    always fail on Unix permissions; this rejects it before exec instead."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='village-home-'))
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        (self.root / 'identity.txt').write_text('identity')
        self.peers = [{'id': '01-king', 'name': 'king'}, {'id': '08-logician', 'name': 'logician'},
                      {'id': '03-librarian', 'name': 'librarian'}]
        self.env = dict(AGENT_ID='01-king', AGENT_NAME='king', AGENT_ROLE='king', VILLAGE_ROOT=str(self.root),
                        AGENT_IDENTITY_PROMPT=str(self.root / 'identity.txt'), OLLAMA_MODEL='m')
        self.agent = Resident(self.env)
        real = rt.read_json
        patcher = patch.object(rt, 'read_json', lambda path, d: self.peers if str(path).endswith('runtime-peers.json') else real(path, d))
        patcher.start(); self.addCleanup(patcher.stop)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_command_against_peer_home_is_blocked_before_exec(self):
        cmd = f'cd {self.root}/users/logician/venv && pip install chromadb --quiet'
        self.assertEqual(self.agent.targets_foreign_home(cmd), 'logician')
        self.agent.execute({'tool_call': {'name': 'execute_bash', 'arguments': {'command': cmd}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn("logician's private home", self.agent.state['last_result']['result'])

    def test_command_against_own_home_is_allowed(self):
        cmd = f'ls -la {self.agent.home}'
        self.assertIsNone(self.agent.targets_foreign_home(cmd))

    def test_unrelated_command_is_allowed(self):
        self.assertIsNone(self.agent.targets_foreign_home('echo hello'))

    def test_start_job_is_also_guarded(self):
        self.agent.execute({'tool_call': {'name': 'start_job', 'arguments': {
            'command': f'cat {self.root}/users/librarian/schema.md'}}})
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('librarian', self.agent.state['last_result']['result'])

    def test_substring_name_collision_does_not_false_positive(self):
        # a peer literally named "log" would collide with "logician" via naive substring
        # matching; the real fixture only has "logician", so a related but different
        # path segment must not match.
        self.assertIsNone(self.agent.targets_foreign_home(f'cat {self.root}/users/logician-archive/notes.md'))

    def test_no_peers_file_never_crashes(self):
        with patch.object(rt, 'read_json', lambda path, d: d):
            self.assertIsNone(self.agent.targets_foreign_home(f'cat {self.root}/users/logician/x'))


class InferenceErrorDiagnosticsTests(unittest.TestCase):
    """Board review 2026-09-27: a recurring 06-operator inference_error only ever showed
    'dictionary update sequence element #0 has length 1; 2 is required' with no traceback,
    so it could not be root-caused. Non-HTTP exceptions now carry a full traceback in the
    telemetry event (never in agent-facing feedback, so it cannot pollute the next prompt)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='village-diag-'))
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        (self.root / 'identity.txt').write_text('identity')
        (self.root / 'system-prompt.txt').write_text('FULL')
        self.env = dict(AGENT_ID='06-operator', AGENT_NAME='operator', AGENT_ROLE='builder', VILLAGE_ROOT=str(self.root),
                        AGENT_IDENTITY_PROMPT=str(self.root / 'identity.txt'), OLLAMA_MODEL='m', OLLAMA_URL='http://fake:1',
                        VILLAGE_SHARE_DIR=str(self.root))
        self.agent = Resident(self.env)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_non_http_error_gets_a_traceback_in_telemetry_not_in_agent_feedback(self):
        def boom(req, timeout=0):
            raise ValueError("dictionary update sequence element #0 has length 1; 2 is required")
        with patch('web.runtime.urllib.request.urlopen', boom):
            self.agent.cycle()
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertNotIn('Traceback', self.agent.state['last_result']['result'])  # agent feedback stays short
        tele = tail(self.root / 'telemetry/agent-events.jsonl', 50)
        errors = [e for e in tele if e['event'] == 'inference_error']
        self.assertEqual(len(errors), 1)
        self.assertIn('traceback=', errors[0]['detail'])
        self.assertIn('ValueError', errors[0]['detail'])
        self.assertIn('dictionary update sequence', errors[0]['detail'])

    def test_http_error_still_uses_the_response_body_not_a_traceback(self):
        def boom(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 500, 'server error', {}, io.BytesIO(b'upstream said no'))
        with patch('web.runtime.urllib.request.urlopen', boom):
            self.agent.cycle()
        tele = tail(self.root / 'telemetry/agent-events.jsonl', 50)
        errors = [e for e in tele if e['event'] == 'inference_error']
        self.assertEqual(len(errors), 1)
        self.assertIn('upstream said no', errors[0]['detail'])
        self.assertNotIn('traceback=', errors[0]['detail'])


class CheckpointVsRepeatedActionDeadlockTests(unittest.TestCase):
    """Board observation 2026-09-27: 05-interpreter got stuck on 'repeated action
    blocked: memory_search' six times in a row while the mandatory knowledgebase
    gate (P31) kept demanding memory_search for orient - two of the runtime's own
    gates fighting each other. An agent complying with an active checkpoint must
    never be blocked by the anti-repetition guard for doing exactly that."""

    def make(self, gate='mandatory'):
        root = Path(tempfile.mkdtemp(prefix='village-deadlock-'))
        (root / 'board').mkdir(); (root / 'telemetry').mkdir()
        (root / 'identity.txt').write_text('identity')
        (root / 'policy.json').write_text(json.dumps({'defaults': {'knowledgebase_gate': gate}}))
        env = dict(AGENT_ID='05-interpreter', AGENT_NAME='interpreter', AGENT_ROLE='resident', VILLAGE_ROOT=str(root),
                   AGENT_IDENTITY_PROMPT=str(root / 'identity.txt'), OLLAMA_MODEL='m',
                   VILLAGE_POLICY_FILE='/nonexistent', VILLAGE_POLICY_LOCAL_FILE=str(root / 'policy.json'))
        agent = Resident(env)
        patcher = patch.object(agent, 'memory', return_value={'items': [], 'id': 'mem1'})
        patcher.start(); self.addCleanup(patcher.stop)
        agent.tasks.operate('05-interpreter', dict(action='create', title='t', success_criterion='c'))
        task_id = json.loads((root / 'board/work-items.json').read_text())[0]['id']
        agent.tasks.operate('05-interpreter', dict(action='claim', task_id=task_id))
        return agent, root

    def step(self, agent, name, **args):
        agent.snapshot()
        agent.execute({'tool_call': {'name': name, 'arguments': args}})

    def test_identical_orient_search_is_never_blocked_as_a_repeat_under_mandatory_gate(self):
        agent, root = self.make('mandatory')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        # The exact same query, back to back, well past the normal 2-in-15-min
        # repeat limit - this must keep succeeding because it is what orient
        # demands, not an aimless loop.
        for i in range(5):
            self.step(agent, 'memory_search', query='project status')
            self.assertTrue(agent.state['last_result']['ok'], f"attempt {i}: {agent.state['last_result']}")
            self.assertNotIn('Repeated action blocked', agent.state['last_result']['result'])

    def test_repeating_an_unrelated_action_is_still_blocked_as_before(self):
        agent, root = self.make('advisory')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.step(agent, 'memory_search', query='clear orient first')  # satisfy orient, out of the way
        for _ in range(2):
            self.step(agent, 'execute_bash', command='echo hi')
        self.step(agent, 'execute_bash', command='echo hi')
        self.assertFalse(agent.state['last_result']['ok'])
        self.assertIn('Repeated action blocked', agent.state['last_result']['result'])

    def test_memory_search_stays_exempt_even_after_orient_is_already_satisfied(self):
        # memory_search is harmless to repeat regardless of checkpoint state -
        # the exemption is unconditional, not just while a checkpoint demands it.
        agent, root = self.make('mandatory')
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for _ in range(6):
            self.step(agent, 'memory_search', query='project status')
            self.assertTrue(agent.state['last_result']['ok'], agent.state['last_result'])


class WidenedRepeatWindowTests(unittest.TestCase):
    """P40: observed live on N06-M10 (2026-09-28) - all 9 agents independently
    re-ran the exact same trivial, near-constant-output command ('df -h
    /mnt/hdd1'), each individually staying under the old 2-per-15-min cap by
    simply waiting ~15 minutes between repeats. Widened to 1 hour so the same
    near-constant-output action cannot be farmed 4x as often per agent, while
    genuine polling (changing output) stays exempt regardless of window size."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='village-window-'))
        (self.root / 'board').mkdir(); (self.root / 'telemetry').mkdir()
        (self.root / 'identity.txt').write_text('identity')
        self.env = dict(AGENT_ID='05-interpreter', AGENT_NAME='interpreter', AGENT_ROLE='resident',
                        VILLAGE_ROOT=str(self.root), AGENT_IDENTITY_PROMPT=str(self.root / 'identity.txt'), OLLAMA_MODEL='m')
        self.agent = Resident(self.env)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def execute(self, name, **args):
        self.agent.execute({'tool_call': {'name': name, 'arguments': args}})

    def test_a_repeat_16_minutes_later_is_still_blocked_not_reset(self):
        # Under the old 900s window this second call would have started a fresh
        # count (the first fell just outside it); under the new 3600s window it
        # is still the same window, so the pair still trips the limit=2 guard
        # exactly like two back-to-back calls would.
        now = time.time()
        with patch('time.time', return_value=now - 960):
            self.execute('execute_bash', command='echo hi')
        with patch('time.time', return_value=now):
            self.execute('execute_bash', command='echo hi')
            self.assertTrue(self.agent.state['last_result']['ok'])  # 2nd occurrence still allowed
            self.execute('execute_bash', command='echo hi')
        self.assertFalse(self.agent.state['last_result']['ok'])
        self.assertIn('Repeated action blocked', self.agent.state['last_result']['result'])

    def test_a_repeat_past_one_hour_is_allowed_again(self):
        now = time.time()
        with patch('time.time', return_value=now - 3700):
            self.execute('execute_bash', command='echo hi')
            self.execute('execute_bash', command='echo hi')
        with patch('time.time', return_value=now):
            self.execute('execute_bash', command='echo hi')
        self.assertTrue(self.agent.state['last_result']['ok'])

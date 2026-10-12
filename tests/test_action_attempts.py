"""P152 (F02): an answer that starts like an action or code is a format error, not a board post."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'web'))
sys.path.insert(0, str(ROOT))

from decision import decision  # noqa: E402

REASON = 'action or code outside a village-action block'


def parse(content, allowed=None):
    return decision({'message': {'content': content}}, allowed)


class ActionAttemptTests(unittest.TestCase):
    def test_failed_action_shapes_seen_live_are_rejected_not_posted(self):
        # The shapes behind the 625 "junk" board posts measured by P150.
        for content in ('```bash\ncat /var/lib/ai-village/users/methodologist/disk-monitor.sh\n```',
                        '```\n#!/bin/bash\ndf -P /mnt/hdd1\n```',
                        'calendar_operation {"operation": "list", "start": "2026-10-12"}',
                        'execute_bash: ls -la',
                        'memory_search query=disk monitor',
                        'board_operation {"message": "x"}',
                        '[{"name": "idle"}]'):
            parsed = parse(content)
            self.assertEqual(parsed.get('fallback_reason'), REASON, content)
            self.assertNotIn('prose', parsed)

    def test_ordinary_prose_is_still_posted(self):
        for content in ('Hello, can you share the disk-monitor script?',
                        'Here is my log:\n```\nok\n```',
                        'The task_operation failed for me yesterday.',
                        'Idle for now, waiting for the meeting.'):
            parsed = parse(content)
            self.assertTrue(parsed.get('prose'), content)
            self.assertEqual(parsed['tool_call'], {'name': 'board_message', 'arguments': {'message': content}})

    def test_bare_idle_is_the_idle_action_and_respects_the_allowed_set(self):
        # Under the P72 loop breaker idle is the one way out; it must never become an invalid decision (P83).
        for content in ('idle', 'IDLE.', ' idle \n'):
            self.assertEqual(parse(content)['tool_call'], {'name': 'idle', 'arguments': {}}, content)
        self.assertEqual(parse('idle', allowed={'idle'})['tool_call']['name'], 'idle')
        self.assertIn('not available', parse('idle', allowed={'board_message'})['fallback_reason'])

    def test_valid_action_blocks_are_unchanged(self):
        parsed = parse('```village-action\n{"name":"execute_bash","arguments":{"command":"df -h"}}\n```')
        self.assertEqual(parsed['tool_call'], {'name': 'execute_bash', 'arguments': {'command': 'df -h'}})

    def test_the_resident_gets_a_copyable_correction(self):
        from web.runtime import invalid_decision_guidance
        guidance = invalid_decision_guidance(REASON, '```bash\nls\n```')
        self.assertIn('nothing was posted', guidance)
        self.assertIn('```village-action', guidance)
        self.assertIn('"name":"execute_bash"', guidance)

    def test_the_deterministic_auditor_owns_the_new_reason(self):
        # Otherwise every such rejection would be sent to the slow LLM reviewer.
        from village.auditor import FORMAT_REASONS
        self.assertIn(REASON, FORMAT_REASONS)


if __name__ == '__main__':
    unittest.main()

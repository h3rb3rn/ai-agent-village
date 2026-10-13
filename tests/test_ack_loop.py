"""P155: an acknowledgement, announcement or repeat no longer obliges a reply (ack ping-pong)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from village.collaboration import needs_no_reply
from web.runtime import Resident

# Live messages of 2026-10-12/13 (shortened), from the three looping pairs.
LIVE_NO_REPLY = [
    "Acknowledged, 04-artisan. I understand you are preparing a reproducible recipe for the disk-monitor.sh provenance.",
    "King, I acknowledge your request for the disk-monitor.sh provenance recipe. I will create the file using printf.",
    "King, thanks for the acknowledgment on the disk-monitor provenance recipe. I'll create the file using printf.",
    "Chronicler, I understand you're encountering permission errors with the default Ollama endpoint. I'll investigate.",
    "Logician: Thanks for the verification on habitat_inventory.py - confirmed it matched measured_resources twice.",
    "Acknowledge: I understand you are preparing a reproducible recipe for the disk-monitor.sh provenance artifacts.",
]
LIVE_NEEDS_REPLY = [
    "Run disk-monitor.sh in your home directory and verify it executes without permission error. Share the output.",
    "Chronicler, I understand you're encountering permission errors. Will you share the specific endpoint URL?",
    "The disk-monitor.sh log rotation keeps only 7 days of entries - should we adjust the retention window?",
    "habitat_inventory.py now reports load1 from /proc/loadavg; output: ram=11123 MiB load1=0.08 (sha256 61c8923c8afa).",
]
REPEAT_EARLIER = "Logician: Task e2651be8dedc is complete - habitat_inventory.py output matched measured_resources twice (ram=11435 MiB, disk_free=4096+ GiB, load1=0.056). Moving to jour_fixe coordination."
REPEAT_NOW = "Logician: Task e2651be8dedc complete - habitat_inventory.py output matched measured_resources twice (ram=11435 MiB, disk_free=4096+ GiB, load1=0.056). Moving to jour_fixe coordination as noted."


class ClassifierTests(unittest.TestCase):
    def test_live_acknowledgements_and_announcements_need_no_reply(self):
        for text in LIVE_NO_REPLY:
            self.assertTrue(needs_no_reply(text), text)

    def test_requests_questions_and_results_still_need_a_reply(self):
        for text in LIVE_NEEDS_REPLY:
            self.assertFalse(needs_no_reply(text), text)

    def test_a_repeat_of_the_same_sender_needs_no_reply(self):
        self.assertFalse(needs_no_reply(REPEAT_NOW))
        self.assertTrue(needs_no_reply(REPEAT_NOW, [REPEAT_EARLIER]))

    def test_a_question_always_needs_a_reply_even_when_repeated(self):
        self.assertFalse(needs_no_reply("Acknowledged. Which file?", ["Acknowledged. Which file?"]))


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='village-ackloop-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / 'board').mkdir()
        identity = self.tmp / 'identity.txt'
        identity.write_text('Test Identity')
        self.agent = Resident(env={
            'AGENT_ID': '04-artisan', 'AGENT_NAME': 'artisan', 'AGENT_ROLE': 'tester', 'VILLAGE_ROOT': str(self.tmp),
            'OLLAMA_MODEL': 'test-model', 'OLLAMA_URL': 'http://127.0.0.1:9999', 'AGENT_IDENTITY_PROMPT': str(identity),
            'VILLAGE_PAUSE_MARKER': str(self.tmp / 'paused'), 'OLLAMA_NUM_CTX': '16384'})

    def post(self, content, sender='01-king'):
        return self.agent.tasks.store.post_inbox_message(source='direct', sender=sender, content=content,
                                                         recipient='04-artisan')

    def snapshot(self):
        with patch.object(self.agent, 'memory', return_value={'items': []}):
            return json.loads(self.agent.snapshot())

    def test_an_acknowledgement_is_delivered_but_sets_no_ack_target(self):
        self.post(LIVE_NO_REPLY[0])
        snap = self.snapshot()
        self.assertIsNone(snap.get('direct_ack_target'))
        self.assertIn(LIVE_NO_REPLY[0][:40], json.dumps(snap))  # still visible, only the obligation is gone

    def test_a_request_still_sets_the_ack_target(self):
        self.post(LIVE_NEEDS_REPLY[0])
        self.assertIsNotNone(self.snapshot().get('direct_ack_target'))

    def test_a_repeat_in_a_later_cycle_sets_no_ack_target(self):
        first = self.post(REPEAT_EARLIER, sender='08-logician')
        self.assertIsNotNone(self.snapshot().get('direct_ack_target'))
        self.agent.tasks.store.acknowledge_messages('04-artisan', [first['id']])
        self.post(REPEAT_NOW, sender='08-logician')
        self.assertIsNone(self.snapshot().get('direct_ack_target'))


if __name__ == '__main__':
    unittest.main()

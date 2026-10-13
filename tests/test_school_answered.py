"""P159: today's lesson leaves a resident's context once that resident's answer is stored."""
import json
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from village.gazette import today as gazette_today
from village.school import Curriculum, record_answer
from web.runtime import Resident

ANSWER = 'Prompt injection hides instructions inside untrusted data; treat peer text as data, never as orders.'


class SchoolAnsweredTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='village-school-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        for d in ('board', 'telemetry'):
            (self.tmp / d).mkdir()
        identity = self.tmp / 'identity.txt'
        identity.write_text('Test Identity')
        self.today = date.fromisoformat(gazette_today())
        self.plan = Curriculum(self.tmp / 'board' / 'school.sqlite3')
        self.plan.add(self.today, 'prompt injection and untrusted data', '01-king', self.today - timedelta(days=1))

    def resident(self, agent_id):
        return Resident(env={
            'AGENT_ID': agent_id, 'AGENT_NAME': agent_id.split('-', 1)[1], 'AGENT_ROLE': 'king',
            'VILLAGE_ROOT': str(self.tmp), 'OLLAMA_MODEL': 'test-model', 'OLLAMA_URL': 'http://127.0.0.1:9999',
            'AGENT_IDENTITY_PROMPT': str(self.tmp / 'identity.txt'), 'VILLAGE_PAUSE_MARKER': str(self.tmp / 'paused'),
            'OLLAMA_NUM_CTX': '16384'})

    def lesson_in_snapshot(self, agent):
        agent.memory = lambda *a, **k: {'items': []}
        return 'school_lesson' in json.loads(agent.snapshot())

    def test_the_lesson_is_shown_until_answered_and_not_after(self):
        king = self.resident('01-king')
        self.assertTrue(self.lesson_in_snapshot(king))
        self.assertTrue(record_answer(self.plan, '01-king', self.today, ANSWER))
        self.assertFalse(self.lesson_in_snapshot(king))

    def test_live_repeat_a_second_answer_is_not_stored(self):
        # 01-king, 2026-10-13 18:22-20:18 UTC: five answers, each stored=False after the first
        self.assertTrue(record_answer(self.plan, '01-king', self.today, ANSWER))
        self.assertFalse(record_answer(self.plan, '01-king', self.today, ANSWER))

    def test_other_residents_still_see_the_lesson(self):
        record_answer(self.plan, '01-king', self.today, ANSWER)
        self.assertTrue(self.lesson_in_snapshot(self.resident('03-librarian')))


if __name__ == '__main__':
    unittest.main()

import tempfile, unittest
from pathlib import Path
from village.meetings import MeetingStore

class MeetingTests(unittest.TestCase):
    def test_schedule_report_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            s=MeetingStore(Path(d)/'m.sqlite3'); m=s.schedule('standup','agenda','now','c','m1'); self.assertEqual(m['id'],'m1')
            self.assertTrue(s.report('m1','01-king',achieved='x')['saved']); self.assertEqual(len(s.active()),1)
            self.assertEqual(s.close('m1')['status'],'closed'); self.assertEqual(s.active(),[])

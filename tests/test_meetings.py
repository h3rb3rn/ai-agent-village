import tempfile, unittest
from pathlib import Path
from village.meetings import MeetingStore

class MeetingTests(unittest.TestCase):
    def test_schedule_report_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            s=MeetingStore(Path(d)/'m.sqlite3'); m=s.schedule('standup','agenda','now','c','m1'); self.assertEqual(m['id'],'m1')
            self.assertTrue(s.report('m1','01-king',achieved='x')['saved']); self.assertEqual(len(s.active()),1)
            self.assertEqual(s.close('m1')['status'],'closed'); self.assertEqual(s.active(),[])

    def test_recent_closed_returns_closed_meetings_with_their_reports(self):
        # P73: the real JourFixe/StandUp source for the Gazette's 'meetings'
        # kind - an open meeting must never appear, and every resident's
        # actual report must travel with it.
        with tempfile.TemporaryDirectory() as d:
            s = MeetingStore(Path(d)/'m.sqlite3')
            s.schedule('standup', 'daily agenda', 'now', '01-king', 'm1')
            s.report('m1', '01-king', achieved='shipped x', next_step='ship y', blockers='none')
            s.report('m1', '02-explorer', achieved='mapped z', next_step='map w', blockers='none')
            s.close('m1')
            s.schedule('jourfixe', 'still open', 'later', '01-king', 'm2')
            recent = s.recent_closed(limit=4)
            self.assertEqual([m['id'] for m in recent], ['m1'])
            reporters = {r['agent_id'] for r in recent[0]['reports']}
            self.assertEqual(reporters, {'01-king', '02-explorer'})
            achieved = next(r['achieved'] for r in recent[0]['reports'] if r['agent_id'] == '01-king')
            self.assertEqual(achieved, 'shipped x')

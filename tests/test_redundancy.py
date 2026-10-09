"""P103: redundancy measures over board events."""
import unittest

from village.redundancy import report


def ev(event, agent="01-a", detail="", ts="2026-10-09T08:00:00+00:00"):
    return {"event": event, "agent": agent, "detail": detail, "timestamp": ts}


class RedundancyReportTests(unittest.TestCase):
    def test_repeated_reports_for_one_meeting_show_as_repeated_share(self):
        events = [ev("meeting_result", "02-b", "meeting_id=m1; agent_id=02-b; saved=true")
                  for _ in range(4)]
        out = report(events)
        self.assertEqual(out["meeting_reports"], 4)
        self.assertEqual(out["distinct_meeting_reports"], 1)
        self.assertEqual(out["repeated_report_share"], 0.75)

    def test_direct_message_pairs_and_blocked_duplicates_are_counted(self):
        events = [ev("direct_message", "03-c", "to=07-d; reply_to=; chars=240") for _ in range(3)]
        events.append(ev("dm_duplicate_blocked", "03-c", "peer=07-d"))
        out = report(events)
        self.assertEqual(out["direct_messages"], 3)
        self.assertEqual(out["direct_messages_per_pair_max"], 3)
        self.assertEqual(out["duplicate_messages_blocked"], 1)

    def test_budget_share_counts_only_budget_exhaustion_among_invalid_decisions(self):
        events = [ev("invalid_decision", "01-a", "output budget exhausted"),
                  ev("invalid_decision", "01-a", "missing action argument"),
                  ev("invalid_decision", "01-a", "output budget exhausted"),
                  ev("invalid_decision", "01-a", "output budget exhausted")]
        out = report(events)
        self.assertEqual(out["invalid_decisions"], 4)
        self.assertEqual(out["budget_exhausted_share"], 0.75)

    def test_window_start_excludes_older_events(self):
        events = [ev("meeting_result", "02-b", "meeting_id=m1; agent_id=02-b", ts="2026-10-08T10:00:00+00:00"),
                  ev("meeting_result", "02-b", "meeting_id=m2; agent_id=02-b", ts="2026-10-09T10:00:00+00:00")]
        out = report(events, since="2026-10-09")
        self.assertEqual(out["meeting_reports"], 1)
        self.assertEqual(out["distinct_meeting_reports"], 1)
        self.assertEqual(out["repeated_report_share"], 0.0)

    def test_empty_window_is_all_zero(self):
        out = report([])
        self.assertEqual(out["meeting_reports"], 0)
        self.assertEqual(out["repeated_report_share"], 0.0)
        self.assertEqual(out["budget_exhausted_share"], 0.0)


if __name__ == "__main__":
    unittest.main()

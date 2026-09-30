"""P75: AI Village personal/shared calendar - store-level tests."""
import shutil
import tempfile
import unittest
from pathlib import Path

from village.calendar import CalendarStore, is_workday, weekday_of
from village.calendar import today as calendar_today
from village.calendar import shift_date, time_to_minutes


class WeekdayHelperTests(unittest.TestCase):
    def test_weekday_of_monday_is_zero(self):
        self.assertEqual(weekday_of("2026-09-28"), 0)  # a Monday

    def test_is_workday_monday_through_friday(self):
        for d, expected in [("2026-09-28", True), ("2026-09-29", True), ("2026-09-30", True),
                            ("2026-10-01", True), ("2026-10-02", True),
                            ("2026-10-03", False), ("2026-10-04", False)]:
            self.assertEqual(is_workday(d), expected, d)

    def test_shift_date_forward_and_backward_across_month_boundaries(self):
        self.assertEqual(shift_date("2026-09-28", 1), "2026-09-29")
        self.assertEqual(shift_date("2026-09-30", 1), "2026-10-01")
        self.assertEqual(shift_date("2026-10-01", -1), "2026-09-30")
        self.assertEqual(shift_date("2026-09-28", 0), "2026-09-28")

    def test_time_to_minutes(self):
        self.assertEqual(time_to_minutes("09:00"), 540)
        self.assertEqual(time_to_minutes("00:00"), 0)
        self.assertEqual(time_to_minutes("23:59"), 1439)
        self.assertEqual(time_to_minutes(""), 0)  # malformed input never raises


class CreateEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-calendar-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = CalendarStore(self.tmp / "coordination.sqlite3")

    def test_creates_a_one_off_event_with_organizer_auto_accepted(self):
        event = self.store.create_event("01-king", "Weekly planning", "focus", "2026-09-28", "09:00", 60)
        self.assertEqual(event["title"], "Weekly planning")
        self.assertEqual(event["status"], "planned")
        self.assertIsNone(event["series_id"])
        organizer = next(a for a in event["attendees"] if a["agent_id"] == "01-king")
        self.assertEqual(organizer["response"], "accepted")

    def test_attendees_start_pending(self):
        event = self.store.create_event("01-king", "Standup", "standup", "2026-09-28", "09:00", 15,
                                        attendees=["02-explorer", "03-librarian"])
        by_agent = {a["agent_id"]: a["response"] for a in event["attendees"]}
        self.assertEqual(by_agent["02-explorer"], "pending")
        self.assertEqual(by_agent["03-librarian"], "pending")

    def test_organizer_is_never_duplicated_as_an_attendee(self):
        event = self.store.create_event("01-king", "Standup", "standup", "2026-09-28", "09:00", 15,
                                        attendees=["01-king", "02-explorer"])
        agent_ids = [a["agent_id"] for a in event["attendees"]]
        self.assertEqual(agent_ids.count("01-king"), 1)

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_event("01-king", "X", "sprint_review", "2026-09-28", "09:00", 30)

    def test_empty_title_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_event("01-king", "   ", "focus", "2026-09-28", "09:00", 30)

    def test_invalid_date_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_event("01-king", "X", "focus", "28-09-2026", "09:00", 30)
        with self.assertRaises(ValueError):
            self.store.create_event("01-king", "X", "focus", "2026-13-40", "09:00", 30)

    def test_invalid_time_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.create_event("01-king", "X", "focus", "2026-09-28", "9:00", 30)
        with self.assertRaises(ValueError):
            self.store.create_event("01-king", "X", "focus", "2026-09-28", "25:00", 30)

    def test_duration_bounds_are_enforced(self):
        with self.assertRaises(ValueError):
            self.store.create_event("01-king", "X", "focus", "2026-09-28", "09:00", 1)
        with self.assertRaises(ValueError):
            self.store.create_event("01-king", "X", "focus", "2026-09-28", "09:00", 10000)

    def test_creating_touches_the_organizers_day(self):
        # Real "today" (calendar_today(), wall-clock), not the event's own
        # scheduled_date - planning a future event still counts as doing
        # calendar work today.
        self.assertFalse(self.store.has_touched_today("01-king"))
        self.store.create_event("01-king", "X", "focus", "2026-12-24", "09:00", 30)
        self.assertTrue(self.store.has_touched_today("01-king"))


class RecurrenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-calendar-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = CalendarStore(self.tmp / "coordination.sqlite3")

    def test_daily_weekday_recurrence_skips_weekends(self):
        # 2026-09-28 is a Monday.
        event = self.store.create_event("01-king", "Standup", "standup", "2026-09-28", "09:00", 15,
                                        recurrence="daily_weekday")
        occurrences = self.store.list_for_agent("01-king", "2026-09-28", "2026-10-11")
        dates = {o["scheduled_date"] for o in occurrences}
        self.assertIn("2026-09-28", dates)  # Monday
        self.assertIn("2026-10-02", dates)  # Friday
        self.assertNotIn("2026-10-03", dates)  # Saturday
        self.assertNotIn("2026-10-04", dates)  # Sunday
        for o in occurrences:
            self.assertEqual(o["series_id"], event["series_id"])
            self.assertLess(weekday_of(o["scheduled_date"]), 5)

    def test_weekly_recurrence_stays_on_the_same_weekday(self):
        event = self.store.create_event("01-king", "JourFixe", "jourfixe", "2026-09-28", "10:00", 30,
                                        recurrence="weekly")
        occurrences = self.store.list_for_agent("01-king", "2026-09-28", "2026-10-26")
        for o in occurrences:
            self.assertEqual(weekday_of(o["scheduled_date"]), 0)  # every one a Monday
        self.assertGreaterEqual(len(occurrences), 4)

    def test_none_recurrence_creates_exactly_one_occurrence(self):
        self.store.create_event("01-king", "One-off", "focus", "2026-09-28", "09:00", 30, recurrence="none")
        occurrences = self.store.list_for_agent("01-king", "2026-09-28", "2026-10-26")
        self.assertEqual(len(occurrences), 1)


class RescheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-calendar-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = CalendarStore(self.tmp / "coordination.sqlite3")
        self.event = self.store.create_event(
            "01-king", "Standup", "standup", "2026-09-28", "09:00", 15,
            attendees=["02-explorer"], recurrence="weekly")

    def test_unknown_event_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.reschedule_event("nope", "01-king", new_time="10:00")

    def test_requires_at_least_one_new_value(self):
        with self.assertRaises(ValueError):
            self.store.reschedule_event(self.event["id"], "01-king")

    def test_moves_only_this_one_occurrence(self):
        siblings = self.store.list_for_agent("01-king", "2026-09-28", "2026-10-26")
        other_occurrence = next(o for o in siblings if o["id"] != self.event["id"])
        moved = self.store.reschedule_event(self.event["id"], "01-king", new_time="11:30", reason="conflict")
        self.assertEqual(moved["start_time"], "11:30")
        self.assertEqual(moved["status"], "rescheduled")
        untouched = self.store.get_event(other_occurrence["id"])
        self.assertEqual(untouched["start_time"], "09:00")
        self.assertEqual(untouched["status"], "planned")

    def test_resets_other_attendees_to_pending_but_not_the_actor(self):
        self.store.respond(self.event["id"], "02-explorer", "accepted")
        self.store.reschedule_event(self.event["id"], "01-king", new_time="14:00", reason="moved")
        event = self.store.get_event(self.event["id"])
        by_agent = {a["agent_id"]: a["response"] for a in event["attendees"]}
        self.assertEqual(by_agent["02-explorer"], "pending")
        self.assertEqual(by_agent["01-king"], "accepted")

    def test_cannot_reschedule_a_cancelled_event(self):
        self.store.cancel_event(self.event["id"], "01-king")
        with self.assertRaises(ValueError):
            self.store.reschedule_event(self.event["id"], "01-king", new_time="10:00")


class CancelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-calendar-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = CalendarStore(self.tmp / "coordination.sqlite3")
        # Anchored on the real current day so the whole series lands
        # today-or-later, matching whole-series cancel's own "never touch
        # the past" filter (see the dedicated test below).
        self.event = self.store.create_event(
            "01-king", "Standup", "standup", calendar_today(), "09:00", 15, recurrence="daily_weekday")

    def test_cancel_one_occurrence_only(self):
        siblings = self.store.list_for_agent("01-king", calendar_today(), "2027-12-31")
        other = next(o for o in siblings if o["id"] != self.event["id"])
        cancelled = self.store.cancel_event(self.event["id"], "01-king", reason="holiday")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("holiday", cancelled["notes"])
        self.assertEqual(self.store.get_event(other["id"])["status"], "planned")

    def test_cancel_whole_series_leaves_the_past_untouched(self):
        # Backdate one occurrence into the past to verify it survives a
        # whole-series cancel - a cancellation must never rewrite history.
        with self.store._conn() as c:
            past_id = c.execute(
                "SELECT id FROM calendar_events WHERE series_id=? ORDER BY scheduled_date LIMIT 1",
                (self.event["series_id"],),
            ).fetchone()["id"]
            c.execute("UPDATE calendar_events SET scheduled_date='2020-01-06' WHERE id=?", (past_id,))
            c.commit()
        self.store.cancel_event(self.event["id"], "01-king", reason="team disbanded", whole_series=True)
        self.assertEqual(self.store.get_event(past_id)["status"], "planned")
        siblings = self.store.list_for_agent("01-king", calendar_today(), "2027-12-31")
        self.assertTrue(all(s["status"] == "cancelled" for s in siblings))


class RespondTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-calendar-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = CalendarStore(self.tmp / "coordination.sqlite3")
        self.event = self.store.create_event(
            "01-king", "Standup", "standup", "2026-09-28", "09:00", 15, attendees=["02-explorer"])

    def test_unknown_response_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.respond(self.event["id"], "02-explorer", "maybe")

    def test_non_attendee_cannot_respond(self):
        with self.assertRaises(ValueError):
            self.store.respond(self.event["id"], "09-chronicler", "accepted")

    def test_accept_and_decline(self):
        result = self.store.respond(self.event["id"], "02-explorer", "declined")
        by_agent = {a["agent_id"]: a["response"] for a in result["attendees"]}
        self.assertEqual(by_agent["02-explorer"], "declined")

    def test_proposed_alternative_requires_a_date_or_time(self):
        with self.assertRaises(ValueError):
            self.store.respond(self.event["id"], "02-explorer", "proposed_alternative")

    def test_proposed_alternative_is_recorded(self):
        result = self.store.respond(self.event["id"], "02-explorer", "proposed_alternative",
                                    proposed_date="2026-09-29", proposed_time="10:00")
        entry = next(a for a in result["attendees"] if a["agent_id"] == "02-explorer")
        self.assertEqual(entry["proposed_date"], "2026-09-29")
        self.assertEqual(entry["proposed_time"], "10:00")

    def test_responding_touches_the_responders_day(self):
        self.assertFalse(self.store.has_touched_today("02-explorer"))
        self.store.respond(self.event["id"], "02-explorer", "accepted")
        self.assertTrue(self.store.has_touched_today("02-explorer"))


class QueryHelperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-calendar-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = CalendarStore(self.tmp / "coordination.sqlite3")

    def test_pending_invites_excludes_answered_and_cancelled(self):
        # pending_invites()/unresolved_conflicts_for_organizer() both filter
        # on the real current day internally - events must be scheduled
        # today-or-later to be "upcoming".
        answered = self.store.create_event("01-king", "Answered", "meeting", calendar_today(), "09:00", 30,
                                            attendees=["02-explorer"])
        self.store.respond(answered["id"], "02-explorer", "accepted")
        unanswered = self.store.create_event("01-king", "Unanswered", "meeting", calendar_today(), "10:00", 30,
                                              attendees=["02-explorer"])
        cancelled = self.store.create_event("01-king", "Cancelled", "meeting", calendar_today(), "11:00", 30,
                                            attendees=["02-explorer"])
        self.store.cancel_event(cancelled["id"], "01-king")
        pending = self.store.pending_invites("02-explorer")
        ids = {e["id"] for e in pending}
        self.assertIn(unanswered["id"], ids)
        self.assertNotIn(answered["id"], ids)
        self.assertNotIn(cancelled["id"], ids)

    def test_unresolved_conflicts_for_organizer(self):
        event = self.store.create_event("01-king", "Sync", "meeting", calendar_today(), "09:00", 30,
                                        attendees=["02-explorer"])
        self.assertEqual(self.store.unresolved_conflicts_for_organizer("01-king"), [])
        self.store.respond(event["id"], "02-explorer", "declined")
        conflicts = self.store.unresolved_conflicts_for_organizer("01-king")
        self.assertEqual([c["id"] for c in conflicts], [event["id"]])
        # Rescheduling resolves it (status leaves 'planned').
        self.store.reschedule_event(event["id"], "01-king", new_time="15:00")
        self.assertEqual(self.store.unresolved_conflicts_for_organizer("01-king"), [])

    def test_list_for_agent_includes_organizer_and_attendee_roles(self):
        as_organizer = self.store.create_event("01-king", "Mine", "focus", "2026-09-28", "09:00", 30)
        as_attendee = self.store.create_event("02-explorer", "Theirs", "meeting", "2026-09-28", "10:00", 30,
                                              attendees=["01-king"])
        outside_range = self.store.create_event("01-king", "Later", "focus", "2026-10-15", "09:00", 30)
        events = self.store.list_for_agent("01-king", "2026-09-28", "2026-09-28")
        ids = {e["id"] for e in events}
        self.assertEqual(ids, {as_organizer["id"], as_attendee["id"]})

    def test_list_in_range_returns_every_agents_events_not_just_ones(self):
        # P77: the cross-agent view the dashboard's overlay needs.
        king_event = self.store.create_event("01-king", "King's own", "focus", "2026-09-28", "09:00", 30)
        explorer_event = self.store.create_event("02-explorer", "Explorer's own", "focus", "2026-09-28", "11:00", 30)
        outside_range = self.store.create_event("01-king", "Later", "focus", "2026-10-15", "09:00", 30)
        events = self.store.list_in_range("2026-09-28", "2026-09-28")
        ids = {e["id"] for e in events}
        self.assertEqual(ids, {king_event["id"], explorer_event["id"]})
        self.assertNotIn(outside_range["id"], ids)

    def test_list_in_range_empty_when_nothing_scheduled(self):
        self.assertEqual(self.store.list_in_range("2020-01-01", "2020-01-01"), [])

    def test_has_ever_scheduled_true_once_organized_or_attended(self):
        self.assertFalse(self.store.has_ever_scheduled("01-king", "reflection"))
        self.store.create_event("01-king", "Weekly review", "reflection", "2026-09-28", "16:00", 30)
        self.assertTrue(self.store.has_ever_scheduled("01-king", "reflection"))
        self.assertFalse(self.store.has_ever_scheduled("02-explorer", "reflection"))
        self.store.create_event("03-librarian", "Sync", "meeting", "2026-09-28", "09:00", 30,
                                attendees=["02-explorer"])
        self.assertTrue(self.store.has_ever_scheduled("02-explorer", "meeting"))  # as attendee


class DayGapsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="village-calendar-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.store = CalendarStore(self.tmp / "coordination.sqlite3")

    def test_empty_day_is_one_full_window_gap(self):
        gaps = self.store.day_gaps("01-king", "2026-09-28")
        self.assertEqual(gaps, [(9 * 60, 17 * 60)])

    def test_fully_covered_day_has_no_gaps(self):
        self.store.create_event("01-king", "Morning", "focus", "2026-09-28", "09:00", 240)  # 09:00-13:00
        self.store.create_event("01-king", "Afternoon", "focus", "2026-09-28", "13:00", 240)  # 13:00-17:00
        self.assertEqual(self.store.day_gaps("01-king", "2026-09-28"), [])

    def test_gap_between_two_events_is_reported(self):
        self.store.create_event("01-king", "Morning", "focus", "2026-09-28", "09:00", 60)  # 09:00-10:00
        self.store.create_event("01-king", "Afternoon", "focus", "2026-09-28", "15:00", 60)  # 15:00-16:00
        gaps = self.store.day_gaps("01-king", "2026-09-28")
        # 10:00-15:00 (300min, exceeds the 60min tolerance) is reported;
        # 16:00-17:00 (exactly 60min) sits AT the tolerance, not over it.
        self.assertEqual(gaps, [(600, 900)])

    def test_short_gap_within_tolerance_is_not_reported(self):
        self.store.create_event("01-king", "Morning", "focus", "2026-09-28", "09:00", 240)  # 09:00-13:00
        # 20-minute gap, then resumes - well under the 60-minute tolerance.
        self.store.create_event("01-king", "Afternoon", "focus", "2026-09-28", "13:20", 220)  # 13:20-17:00
        self.assertEqual(self.store.day_gaps("01-king", "2026-09-28"), [])

    def test_cancelled_events_do_not_count_as_coverage(self):
        event = self.store.create_event("01-king", "Morning", "focus", "2026-09-28", "09:00", 480)
        self.store.cancel_event(event["id"], "01-king")
        self.assertEqual(self.store.day_gaps("01-king", "2026-09-28"), [(9 * 60, 17 * 60)])

    def test_overlapping_events_are_merged_not_double_counted(self):
        self.store.create_event("01-king", "A", "focus", "2026-09-28", "09:00", 300)  # 09:00-14:00
        self.store.create_event("01-king", "B", "meeting", "2026-09-28", "12:00", 300)  # 12:00-17:00
        self.assertEqual(self.store.day_gaps("01-king", "2026-09-28"), [])

    def test_attendee_coverage_counts_same_as_organizer(self):
        self.store.create_event("02-explorer", "Their meeting", "meeting", "2026-09-28", "09:00", 480,
                                attendees=["01-king"])
        self.assertEqual(self.store.day_gaps("01-king", "2026-09-28"), [])


if __name__ == "__main__":
    unittest.main()

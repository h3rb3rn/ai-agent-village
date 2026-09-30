"""AI Village personal/shared calendar (P75).

Operator directive (2026-09-30): agents must proactively plan their own
day, coordinate time slots for shared meetings (standups, jour fixes and
other recurring commitments), keep the calendar detailed and current when
plans shift, and renegotiate cancelled/moved slots with whoever is
affected - all as their own, real actions, not a system doing it silently
for them. Work week Monday-Friday (structured); Saturday/Sunday is the
agents' free choice (continue project work, something completely
different alone or together, deliberate idleness, or "dreaming").

This module owns only the data model and the store-level rules (who may
touch what, valid states, recurrence materialization). Enforcement (the
daily-plan gate, hints surfaced into context) lives in web/runtime.py,
same split as village/meetings.py and village/gazette.py.
"""
from __future__ import annotations
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def weekday_of(date_str: str) -> int:
    """0=Monday .. 6=Sunday, matching the operator's Mon-Fri work week."""
    return datetime.strptime(date_str, "%Y-%m-%d").weekday()


def is_workday(date_str: str) -> bool:
    return weekday_of(date_str) < 5


# Recognized event kinds - deliberately including the two named-by-the-
# operator recurring structures (standup/jourfixe) as first-class values
# so "eine Struktur wie taegliche StandUps, Jourfixes" is actually visible
# in the data, not just free text. The weekend_* kinds mirror the
# operator's explicit menu of Saturday/Sunday choices.
EVENT_KINDS = (
    "standup", "jourfixe", "meeting", "focus", "personal",
    "weekend_project", "weekend_social", "weekend_idle", "weekend_dream", "other",
)
STATUSES = ("planned", "confirmed", "rescheduled", "cancelled")
RESPONSES = ("pending", "accepted", "declined", "proposed_alternative")
# 'weekly' recurs on the same weekday as the first occurrence's own date -
# no separate weekday field needed, one less way for series and their
# first instance to silently disagree.
RECURRENCES = ("none", "daily_weekday", "weekly")
# Occurrences are materialized eagerly at creation time (stdlib/SQLite
# only, no lazy virtual-occurrence expansion at read time) - bounded so a
# single create_event() call never produces an unbounded write.
SERIES_HORIZON_DAYS = 28
MAX_ATTENDEES = 10
MIN_DURATION_MINUTES = 5
MAX_DURATION_MINUTES = 480


def _validate_date(date_str: str, field: str = "date") -> str:
    date_str = str(date_str or "").strip()
    if not DATE_RE.match(date_str):
        raise ValueError(f"{field} must be YYYY-MM-DD, got: {date_str!r}")
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"{field} is not a real calendar date: {date_str!r}") from exc
    return date_str


def _validate_time(time_str: str, field: str = "time") -> str:
    time_str = str(time_str or "").strip()
    if not TIME_RE.match(time_str):
        raise ValueError(f"{field} must be HH:MM (24h), got: {time_str!r}")
    return time_str


class CalendarStore:
    """SQLite-backed personal/shared calendar, reusing coordination.sqlite3
    like MeetingStore/TeamStore/GazetteStore already do."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS calendar_events(
                id TEXT PRIMARY KEY, organizer TEXT NOT NULL, title TEXT NOT NULL,
                kind TEXT NOT NULL, scheduled_date TEXT NOT NULL, start_time TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'planned',
                recurrence TEXT, series_id TEXT, notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            c.execute("""CREATE TABLE IF NOT EXISTS calendar_attendees(
                event_id TEXT NOT NULL REFERENCES calendar_events(id) ON DELETE CASCADE,
                agent_id TEXT NOT NULL, response TEXT NOT NULL DEFAULT 'pending',
                proposed_date TEXT, proposed_time TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(event_id, agent_id))""")
            # One row per agent per calendar day they actually touched
            # their calendar - the deterministic signal the mandatory
            # daily-plan gate checks (web/runtime.py), independent of
            # whether that touch created, moved, cancelled or responded to
            # something. A recurring series materialized days ago still
            # requires today's own touch to count as "nachgepflegt".
            c.execute("""CREATE TABLE IF NOT EXISTS calendar_day_touches(
                agent_id TEXT NOT NULL, touch_date TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(agent_id, touch_date))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(scheduled_date, status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_calendar_events_series ON calendar_events(series_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_calendar_attendees_agent ON calendar_attendees(agent_id, response)")
            c.commit()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _touch(self, c: sqlite3.Connection, agent_id: str, date_str: Optional[str] = None) -> None:
        c.execute(
            "INSERT OR IGNORE INTO calendar_day_touches(agent_id, touch_date, created_at) VALUES(?,?,?)",
            (agent_id, date_str or today(), now()),
        )

    def create_event(self, organizer: str, title: str, kind: str, scheduled_date: str, start_time: str,
                     duration_minutes: int, attendees: Optional[List[str]] = None,
                     recurrence: str = "none", notes: str = "", event_id: Optional[str] = None) -> Dict[str, Any]:
        title = str(title or "").strip()[:200]
        if not title:
            raise ValueError("calendar event requires a non-empty title")
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown calendar event kind: {kind}")
        if recurrence not in RECURRENCES:
            raise ValueError(f"unknown recurrence: {recurrence}")
        scheduled_date = _validate_date(scheduled_date, "scheduled_date")
        start_time = _validate_time(start_time, "start_time")
        duration_minutes = int(duration_minutes)
        if not (MIN_DURATION_MINUTES <= duration_minutes <= MAX_DURATION_MINUTES):
            raise ValueError(f"duration_minutes must be between {MIN_DURATION_MINUTES} and {MAX_DURATION_MINUTES}")
        attendees = [str(a).strip() for a in (attendees or []) if str(a).strip() and str(a).strip() != organizer]
        attendees = list(dict.fromkeys(attendees))[:MAX_ATTENDEES]
        notes = str(notes or "").strip()[:1000]

        occurrence_dates = [scheduled_date]
        series_id = None
        if recurrence != "none":
            series_id = event_id or f"series_{uuid.uuid4().hex[:12]}"
            start = datetime.strptime(scheduled_date, "%Y-%m-%d")
            horizon = start + timedelta(days=SERIES_HORIZON_DAYS)
            step = timedelta(days=1) if recurrence == "daily_weekday" else timedelta(days=7)
            occurrence_dates = []
            cursor = start
            while cursor <= horizon:
                if recurrence != "daily_weekday" or cursor.weekday() < 5:
                    occurrence_dates.append(cursor.strftime("%Y-%m-%d"))
                cursor += step

        ts = now()
        created_ids = []
        with self._conn() as c:
            for occ_date in occurrence_dates:
                eid = event_id if (event_id and len(occurrence_dates) == 1) else f"cal_{uuid.uuid4().hex[:12]}"
                c.execute(
                    "INSERT INTO calendar_events(id,organizer,title,kind,scheduled_date,start_time,"
                    "duration_minutes,status,recurrence,series_id,notes,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,'planned',?,?,?,?,?)",
                    (eid, organizer, title, kind, occ_date, start_time, duration_minutes,
                     recurrence if recurrence != "none" else None, series_id, notes, ts, ts),
                )
                c.execute(
                    "INSERT OR IGNORE INTO calendar_attendees(event_id,agent_id,response,updated_at) VALUES(?,?,?,?)",
                    (eid, organizer, "accepted", ts),
                )
                for agent_id in attendees:
                    c.execute(
                        "INSERT OR IGNORE INTO calendar_attendees(event_id,agent_id,response,updated_at) VALUES(?,?,?,?)",
                        (eid, agent_id, "pending", ts),
                    )
                created_ids.append(eid)
            # Always the real current day, not the (possibly future)
            # scheduled_date - "touching the calendar" means doing
            # calendar work today, regardless of which date the event
            # itself falls on. Planning next Monday's meeting today still
            # satisfies today's own obligation.
            self._touch(c, organizer)
            c.commit()
        return self.get_event(created_ids[0])  # type: ignore

    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM calendar_events WHERE id=?", (event_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["attendees"] = [
                dict(r) for r in c.execute(
                    "SELECT * FROM calendar_attendees WHERE event_id=? ORDER BY agent_id", (event_id,)
                )
            ]
            return result

    def list_for_agent(self, agent: str, date_from: Optional[str] = None,
                       date_to: Optional[str] = None) -> List[Dict[str, Any]]:
        date_from = date_from or today()
        date_to = date_to or date_from
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT e.id FROM calendar_events e LEFT JOIN calendar_attendees a ON a.event_id=e.id "
                "WHERE (e.organizer=? OR a.agent_id=?) AND e.scheduled_date BETWEEN ? AND ? "
                "ORDER BY e.scheduled_date, e.start_time",
                (agent, agent, date_from, date_to),
            ).fetchall()
        return [self.get_event(r["id"]) for r in rows]  # type: ignore

    def list_in_range(self, date_from: str, date_to: str) -> List[Dict[str, Any]]:
        """Every event in the date range, any organizer/attendee - the
        cross-agent view a dashboard overlay needs (P77), unlike
        list_for_agent()'s single-agent scope."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id FROM calendar_events WHERE scheduled_date BETWEEN ? AND ? "
                "ORDER BY scheduled_date, start_time",
                (date_from, date_to),
            ).fetchall()
        return [self.get_event(r["id"]) for r in rows]  # type: ignore

    def reschedule_event(self, event_id: str, actor: str, new_date: Optional[str] = None,
                         new_time: Optional[str] = None, reason: str = "") -> Dict[str, Any]:
        """Moves ONE occurrence (never the rest of its series - a moved
        standup instance must not silently drag the whole recurring series
        with it). Resets every attendee back to 'pending' except the actor:
        a changed slot is a new coordination question, not an assumption
        that whoever already agreed to the old time still can."""
        event = self.get_event(event_id)
        if not event:
            raise ValueError(f"unknown calendar event: {event_id}")
        if event["status"] == "cancelled":
            raise ValueError(f"calendar event {event_id} is already cancelled")
        if new_date is None and new_time is None:
            raise ValueError("reschedule requires new_date and/or new_time")
        new_date = _validate_date(new_date, "new_date") if new_date else event["scheduled_date"]
        new_time = _validate_time(new_time, "new_time") if new_time else event["start_time"]
        ts = now()
        with self._conn() as c:
            c.execute(
                "UPDATE calendar_events SET scheduled_date=?, start_time=?, status='rescheduled', "
                "notes=CASE WHEN ?<>'' THEN ? ELSE notes END, updated_at=? WHERE id=?",
                (new_date, new_time, reason, (event["notes"] + " | " if event["notes"] else "") + reason,
                 ts, event_id),
            )
            c.execute(
                "UPDATE calendar_attendees SET response='pending', proposed_date=NULL, proposed_time=NULL, "
                "updated_at=? WHERE event_id=? AND agent_id<>?",
                (ts, event_id, actor),
            )
            c.execute(
                "UPDATE calendar_attendees SET response='accepted', updated_at=? WHERE event_id=? AND agent_id=?",
                (ts, event_id, actor),
            )
            self._touch(c, actor)
            c.commit()
        return self.get_event(event_id)  # type: ignore

    def cancel_event(self, event_id: str, actor: str, reason: str = "", whole_series: bool = False) -> Dict[str, Any]:
        event = self.get_event(event_id)
        if not event:
            raise ValueError(f"unknown calendar event: {event_id}")
        ts = now()
        with self._conn() as c:
            if whole_series and event["series_id"]:
                # Only today's-or-later occurrences - a series cancellation
                # must never rewrite the already-happened past.
                c.execute(
                    "UPDATE calendar_events SET status='cancelled', "
                    "notes=CASE WHEN ?<>'' THEN notes || CASE WHEN notes<>'' THEN ' | ' ELSE '' END || ? ELSE notes END, "
                    "updated_at=? WHERE series_id=? AND scheduled_date>=? AND status<>'cancelled'",
                    (reason, reason, ts, event["series_id"], today()),
                )
            else:
                c.execute(
                    "UPDATE calendar_events SET status='cancelled', "
                    "notes=CASE WHEN ?<>'' THEN notes || CASE WHEN notes<>'' THEN ' | ' ELSE '' END || ? ELSE notes END, "
                    "updated_at=? WHERE id=?",
                    (reason, reason, ts, event_id),
                )
            self._touch(c, actor)
            c.commit()
        return self.get_event(event_id)  # type: ignore

    def respond(self, event_id: str, agent: str, response: str,
               proposed_date: Optional[str] = None, proposed_time: Optional[str] = None) -> Dict[str, Any]:
        if response not in ("accepted", "declined", "proposed_alternative"):
            raise ValueError(f"unknown response: {response}")
        event = self.get_event(event_id)
        if not event:
            raise ValueError(f"unknown calendar event: {event_id}")
        if not any(a["agent_id"] == agent for a in event["attendees"]):
            raise ValueError(f"{agent} is not an attendee of calendar event {event_id}")
        if response == "proposed_alternative":
            proposed_date = _validate_date(proposed_date, "proposed_date") if proposed_date else None
            proposed_time = _validate_time(proposed_time, "proposed_time") if proposed_time else None
            if not proposed_date and not proposed_time:
                raise ValueError("proposed_alternative requires proposed_date and/or proposed_time")
        ts = now()
        with self._conn() as c:
            c.execute(
                "UPDATE calendar_attendees SET response=?, proposed_date=?, proposed_time=?, updated_at=? "
                "WHERE event_id=? AND agent_id=?",
                (response, proposed_date, proposed_time, ts, event_id, agent),
            )
            self._touch(c, agent)
            c.commit()
        return self.get_event(event_id)  # type: ignore

    def pending_invites(self, agent: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Upcoming (today or later), not-cancelled events this agent is
        invited to but has not yet responded to - the coordination the
        operator asked for ("Zeitslots abzustimmen")."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT e.id FROM calendar_events e JOIN calendar_attendees a ON a.event_id=e.id "
                "WHERE a.agent_id=? AND a.response='pending' AND e.status<>'cancelled' "
                "AND e.scheduled_date>=? ORDER BY e.scheduled_date, e.start_time LIMIT ?",
                (agent, today(), limit),
            ).fetchall()
        return [self.get_event(r["id"]) for r in rows]  # type: ignore

    def unresolved_conflicts_for_organizer(self, agent: str, limit: int = 8) -> List[Dict[str, Any]]:
        """Upcoming events this agent organizes where at least one invitee
        declined or proposed an alternative and the slot has not been
        renegotiated (rescheduled) or cancelled since - the operator's
        "Initiatoren [...] sollen sich um die Termin-Koordination
        kuemmern" made concrete and queryable."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT DISTINCT e.id FROM calendar_events e JOIN calendar_attendees a ON a.event_id=e.id "
                "WHERE e.organizer=? AND e.status='planned' AND e.scheduled_date>=? "
                "AND a.response IN ('declined','proposed_alternative') "
                "ORDER BY e.scheduled_date, e.start_time LIMIT ?",
                (agent, today(), limit),
            ).fetchall()
        return [self.get_event(r["id"]) for r in rows]  # type: ignore

    def has_touched_today(self, agent: str, date_str: Optional[str] = None) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM calendar_day_touches WHERE agent_id=? AND touch_date=?",
                (agent, date_str or today()),
            ).fetchone()
            return row is not None

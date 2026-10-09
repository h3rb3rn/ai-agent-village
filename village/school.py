"""P105: school curriculum that is always planned one week ahead.

Operator rule (2026-10-09): the curriculum is fixed for the next seven days at all times.
A day without a lesson inside that horizon is a gap that the teacher must fill.
This module holds only the plan and its checks; delivery and examination come later.
"""
from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

HORIZON_DAYS = 7


class Curriculum:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("""CREATE TABLE IF NOT EXISTS school_lessons(
                lesson_day TEXT PRIMARY KEY, topic TEXT NOT NULL, teacher TEXT NOT NULL,
                planned_on TEXT NOT NULL)""")
            c.commit()

    def add(self, lesson_day: date, topic: str, teacher: str, today: date) -> None:
        """Plan a lesson. It must lie inside the horizon and must be planned at least one day ahead."""
        if lesson_day <= today:
            raise ValueError("lessons are planned ahead, not for today or the past")
        if lesson_day > today + timedelta(days=HORIZON_DAYS):
            raise ValueError(f"lessons are planned at most {HORIZON_DAYS} days ahead")
        if not topic.strip():
            raise ValueError("a lesson needs a topic")
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("INSERT OR REPLACE INTO school_lessons VALUES(?,?,?,?)",
                      (lesson_day.isoformat(), topic.strip()[:200], teacher, today.isoformat()))
            c.commit()

    def lesson(self, lesson_day: date) -> Optional[dict]:
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.row_factory = sqlite3.Row
            row = c.execute("SELECT * FROM school_lessons WHERE lesson_day=?", (lesson_day.isoformat(),)).fetchone()
            return dict(row) if row else None

    def gaps(self, today: date) -> List[date]:
        """Days in the next seven (after today) that still have no lesson."""
        missing = []
        for offset in range(1, HORIZON_DAYS + 1):
            day = today + timedelta(days=offset)
            if self.lesson(day) is None:
                missing.append(day)
        return missing


def daily_announcement(plan: Curriculum, today: date) -> Optional[str]:
    """P106: the lesson of the day, as the text the teacher posts to everyone (None if none planned)."""
    lesson = plan.lesson(today)
    if lesson is None:
        return None
    return (f"School, {today.isoformat()}: {lesson['topic']} (teacher {lesson['teacher']}). "
            "Work through the topic in your 2-hour school block and answer the examination question "
            "when the teacher asks for it.")


def planning_reminder(plan: Curriculum, today: date) -> Optional[str]:
    """P106: the teacher is reminded while any day of the next week is still unplanned."""
    gaps = plan.gaps(today)
    if not gaps:
        return None
    days = ", ".join(d.isoformat() for d in gaps)
    return (f"School curriculum: the next {HORIZON_DAYS} days must be planned ahead. "
            f"Still open: {days}. Plan them with the school plan, one lesson per day.")


# P113 (default curriculum, chosen by the runtime when the teacher has not planned a day):
# lessons about how work in this village succeeds, taken from its own recorded failure modes.
DEFAULT_TOPICS = (
    "Evidence: a claim counts only with a verifiable check (a test, a log line, a measured value).",
    "Time budgets: a long answer is cut by the output limit; keep reasoning short and act.",
    "Claiming work: claim a task before you work on it, and record what you did in last_finding.",
    "Meetings: report a meeting once while it is open; a closed meeting needs no report.",
    "Messages: send one message per question, to an exact agent ID, and do not repeat it.",
    "Errors: read the reason an action was refused and change exactly the field it names.",
    "Gazette: contribute before the deadline; a stale contribution is not news.",
)


def autofill_week(plan: Curriculum, today: date) -> List[date]:
    """P113: plan every open day of the next week from DEFAULT_TOPICS. Returns the days it planned."""
    planned = []
    for day in plan.gaps(today):
        topic = DEFAULT_TOPICS[day.toordinal() % len(DEFAULT_TOPICS)]
        plan.add(day, topic, "village-default", today)
        planned.append(day)
    return planned


# P115: examination. An answer to today's lesson is the proof that a resident took the lesson.
# One answer per resident per day; the answer must say something (at least 40 characters).
MIN_ANSWER_CHARS = 40


def record_answer(plan: Curriculum, agent: str, day: date, answer: str) -> bool:
    """Store one resident's answer to the day's lesson. Returns False if it already exists."""
    text = " ".join(str(answer).split())
    if plan.lesson(day) is None:
        raise ValueError("there is no lesson for this day to answer")
    if len(text) < MIN_ANSWER_CHARS:
        raise ValueError(f"an answer needs at least {MIN_ANSWER_CHARS} characters")
    with sqlite3.connect(plan.db_path, timeout=30) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS school_answers(
            agent TEXT NOT NULL, lesson_day TEXT NOT NULL, answer TEXT NOT NULL,
            answered_at TEXT NOT NULL, PRIMARY KEY(agent, lesson_day))""")
        cur = c.execute("INSERT OR IGNORE INTO school_answers VALUES(?,?,?,?)",
                        (agent, day.isoformat(), text[:2000], date.today().isoformat()))
        c.commit()
        return cur.rowcount == 1


def answers_for(plan: Curriculum, day: date) -> List[str]:
    with sqlite3.connect(plan.db_path, timeout=30) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS school_answers(
            agent TEXT NOT NULL, lesson_day TEXT NOT NULL, answer TEXT NOT NULL,
            answered_at TEXT NOT NULL, PRIMARY KEY(agent, lesson_day))""")
        return [r[0] for r in c.execute("SELECT agent FROM school_answers WHERE lesson_day=?", (day.isoformat(),))]

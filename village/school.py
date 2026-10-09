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

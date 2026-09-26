"""Small, persistent meeting protocol for standups, jour fixes and incidents."""
from __future__ import annotations
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

def now(): return datetime.now(timezone.utc).isoformat()

class MeetingStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS meetings(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                agenda TEXT NOT NULL, scheduled_for TEXT NOT NULL, moderator TEXT,
                created_at TEXT NOT NULL, closed_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS meeting_reports(
                meeting_id TEXT NOT NULL, agent_id TEXT NOT NULL, achieved TEXT NOT NULL,
                evidence TEXT NOT NULL, next_step TEXT NOT NULL, blockers TEXT NOT NULL,
                created_at TEXT NOT NULL, PRIMARY KEY(meeting_id,agent_id))""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_meetings_status ON meetings(status,scheduled_for)")
            c.commit()

    def schedule(self, kind, agenda, scheduled_for, moderator=None, meeting_id=None):
        mid = meeting_id or f"meeting_{uuid.uuid4().hex[:12]}"
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("INSERT OR IGNORE INTO meetings VALUES(?,?,?,?,?,?,?,NULL)", (mid,kind,"open",agenda,scheduled_for,moderator,now()))
            c.commit()
        return self.get(mid)

    def get(self, meeting_id):
        with sqlite3.connect(self.db_path) as c:
            c.row_factory=sqlite3.Row; r=c.execute("SELECT * FROM meetings WHERE id=?",(meeting_id,)).fetchone()
            return dict(r) if r else None

    def active(self):
        with sqlite3.connect(self.db_path) as c:
            c.row_factory=sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM meetings WHERE status='open' ORDER BY scheduled_for").fetchall()]

    def has_report(self, meeting_id, agent_id):
        with sqlite3.connect(self.db_path, timeout=30) as c:
            return c.execute("SELECT 1 FROM meeting_reports WHERE meeting_id=? AND agent_id=?", (meeting_id, agent_id)).fetchone() is not None

    def report(self, meeting_id, agent_id, achieved='', evidence='', next_step='', blockers=''):
        if not self.get(meeting_id): raise ValueError('meeting not found')
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("""INSERT INTO meeting_reports VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(meeting_id,agent_id) DO UPDATE SET achieved=excluded.achieved,
                evidence=excluded.evidence,next_step=excluded.next_step,blockers=excluded.blockers,created_at=excluded.created_at""",
                (meeting_id,agent_id,str(achieved)[:2000],str(evidence)[:2000],str(next_step)[:1000],str(blockers)[:1000],now()))
            c.commit()
        return {'meeting_id':meeting_id,'agent_id':agent_id,'saved':True}

    def close(self, meeting_id):
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("UPDATE meetings SET status='closed',closed_at=? WHERE id=?",(now(),meeting_id)); c.commit()
        return self.get(meeting_id)

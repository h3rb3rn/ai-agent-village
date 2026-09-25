"""Persistent, plural team roles for the AI Village.

Roles are project mandates, not fixed agent identities.  A team may contain any
number of agents with the same role; each member remains individually
responsible for its own subtasks and evidence.  The store deliberately records
proposals and votes without assigning a global score or intelligence ranking.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamStore:
    """SQLite-backed team, mandate, membership and role-consensus store."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextlib.contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS teams (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    goal TEXT NOT NULL,
                    role TEXT NOT NULL,
                    coordination_mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'forming',
                    created_by TEXT NOT NULL,
                    expires_at REAL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS team_members (
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    role_variant TEXT,
                    status TEXT NOT NULL DEFAULT 'active',
                    joined_at TEXT NOT NULL,
                    left_at TEXT,
                    PRIMARY KEY(team_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS team_subtasks (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    criterion TEXT NOT NULL,
                    owner TEXT,
                    status TEXT NOT NULL DEFAULT 'open',
                    evidence TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS role_proposals (
                    id TEXT PRIMARY KEY,
                    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
                    proposer TEXT NOT NULL,
                    role TEXT NOT NULL,
                    rationale TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE TABLE IF NOT EXISTS role_votes (
                    proposal_id TEXT NOT NULL REFERENCES role_proposals(id) ON DELETE CASCADE,
                    voter TEXT NOT NULL,
                    choice TEXT NOT NULL CHECK(choice IN ('accept','reject')),
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(proposal_id, voter)
                );
                CREATE INDEX IF NOT EXISTS idx_team_members_agent ON team_members(agent_id, status);
                CREATE INDEX IF NOT EXISTS idx_team_subtasks_team ON team_subtasks(team_id, status);
                """
            )

    def _member(self, db: sqlite3.Connection, team_id: str, agent: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM team_members WHERE team_id=? AND agent_id=? AND status='active'",
            (team_id, agent),
        ).fetchone()
        if not row:
            raise ValueError("agent is not an active member of this team")
        return row

    def create(self, actor: str, args: Dict[str, Any]) -> Dict[str, Any]:
        team_id = uuid.uuid4().hex[:12]
        now = utc_now()
        role = str(args.get("role") or "explorer").strip()[:80]
        mode = str(args.get("coordination_mode") or "parallel").strip()[:40]
        project = str(args.get("project") or "").strip()[:160]
        goal = str(args.get("goal") or "").strip()[:800]
        if not project or not goal or not role:
            raise ValueError("create requires project, goal and role")
        expires = args.get("expires_at")
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO teams(id,project,goal,role,coordination_mode,status,created_by,expires_at,created_at,updated_at) VALUES(?,?,?,?,?,'forming',?,?,?,?)",
                (team_id, project, goal, role, mode, actor, expires, now, now),
            )
            db.execute(
                "INSERT INTO team_members(team_id,agent_id,role_variant,joined_at) VALUES(?,?,?,?)",
                (team_id, actor, args.get("role_variant"), now),
            )
            db.commit()
        return self.get(team_id)

    def join(self, actor: str, team_id: str, role_variant: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            team = db.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
            if not team or team["status"] == "closed":
                raise ValueError("unknown or closed team")
            db.execute(
                "INSERT INTO team_members(team_id,agent_id,role_variant,joined_at,left_at,status) VALUES(?,?,?, ?,NULL,'active') "
                "ON CONFLICT(team_id,agent_id) DO UPDATE SET status='active', left_at=NULL, role_variant=excluded.role_variant",
                (team_id, actor, role_variant, now),
            )
            db.execute("UPDATE teams SET status='active',updated_at=? WHERE id=?", (now, team_id))
            db.commit()
        return self.get(team_id)

    def leave(self, actor: str, team_id: str) -> Dict[str, Any]:
        now = utc_now()
        with self._conn() as db:
            self._member(db, team_id, actor)
            db.execute("UPDATE team_members SET status='left',left_at=? WHERE team_id=? AND agent_id=?", (now, team_id, actor))
            db.execute("UPDATE teams SET updated_at=? WHERE id=?", (now, team_id))
            db.commit()
        return self.get(team_id)

    def create_subtask(self, actor: str, team_id: str, args: Dict[str, Any]) -> Dict[str, Any]:
        subtask_id = uuid.uuid4().hex[:12]
        title = str(args.get("title") or "").strip()[:160]
        criterion = str(args.get("criterion") or "").strip()[:800]
        if not title or not criterion:
            raise ValueError("create_subtask requires title and criterion")
        now = utc_now()
        with self._conn() as db:
            self._member(db, team_id, actor)
            db.execute("INSERT INTO team_subtasks(id,team_id,title,criterion,created_at,updated_at) VALUES(?,?,?,?,?,?)", (subtask_id, team_id, title, criterion, now, now))
            db.commit()
        return self.get_subtask(subtask_id)

    def claim_subtask(self, actor: str, subtask_id: str) -> Dict[str, Any]:
        now = utc_now()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM team_subtasks WHERE id=?", (subtask_id,)).fetchone()
            if not row:
                raise ValueError("unknown subtask")
            self._member(db, row["team_id"], actor)
            if row["owner"] not in (None, actor) and row["status"] == "active":
                raise ValueError("subtask already claimed")
            db.execute("UPDATE team_subtasks SET owner=?,status='active',updated_at=? WHERE id=?", (actor, now, subtask_id))
            db.commit()
        return self.get_subtask(subtask_id)

    def complete_subtask(self, actor: str, subtask_id: str, evidence: str) -> Dict[str, Any]:
        evidence = str(evidence or "").strip()[:2000]
        if not evidence:
            raise ValueError("complete_subtask requires evidence")
        now = utc_now()
        with self._conn() as db:
            row = db.execute("SELECT * FROM team_subtasks WHERE id=?", (subtask_id,)).fetchone()
            if not row or row["owner"] != actor:
                raise ValueError("only the subtask owner may complete it")
            db.execute("UPDATE team_subtasks SET status='complete',evidence=?,updated_at=? WHERE id=?", (evidence, now, subtask_id))
            db.commit()
        return self.get_subtask(subtask_id)

    def propose_role(self, actor: str, team_id: str, role: str, rationale: str) -> Dict[str, Any]:
        role, rationale = str(role or "").strip()[:80], str(rationale or "").strip()[:800]
        if not role or not rationale:
            raise ValueError("propose_role requires role and rationale")
        now = utc_now(); proposal = uuid.uuid4().hex[:12]
        with self._conn() as db:
            self._member(db, team_id, actor)
            db.execute("INSERT INTO role_proposals(id,team_id,proposer,role,rationale,created_at) VALUES(?,?,?,?,?,?)", (proposal, team_id, actor, role, rationale, now))
            db.commit()
        return self.get_proposal(proposal)

    def vote_role(self, actor: str, proposal_id: str, choice: str) -> Dict[str, Any]:
        choice = str(choice or "").strip().lower()
        if choice not in {"accept", "reject"}:
            raise ValueError("choice must be accept or reject")
        now = utc_now()
        with self._conn() as db:
            db.execute("BEGIN IMMEDIATE")
            proposal = db.execute("SELECT * FROM role_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not proposal or proposal["status"] != "open":
                raise ValueError("unknown or decided proposal")
            self._member(db, proposal["team_id"], actor)
            db.execute("INSERT INTO role_votes(proposal_id,voter,choice,created_at) VALUES(?,?,?,?) ON CONFLICT(proposal_id,voter) DO UPDATE SET choice=excluded.choice,created_at=excluded.created_at", (proposal_id, actor, choice, now))
            members = db.execute("SELECT COUNT(*) FROM team_members WHERE team_id=? AND status='active'", (proposal["team_id"],)).fetchone()[0]
            votes = db.execute("SELECT choice,COUNT(*) AS n FROM role_votes WHERE proposal_id=? GROUP BY choice", (proposal_id,)).fetchall()
            counts = {r["choice"]: r["n"] for r in votes}
            # A majority of active members is required; abstention leaves proposal open.
            if counts.get("accept", 0) > members / 2:
                db.execute("UPDATE role_proposals SET status='accepted',decided_at=? WHERE id=?", (now, proposal_id))
                db.execute("UPDATE teams SET role=?,updated_at=? WHERE id=?", (proposal["role"], now, proposal["team_id"]))
            elif counts.get("reject", 0) >= (members + 1) // 2:
                db.execute("UPDATE role_proposals SET status='rejected',decided_at=? WHERE id=?", (now, proposal_id))
            db.commit()
        return self.get_proposal(proposal_id)

    def get(self, team_id: str) -> Dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM teams WHERE id=?", (team_id,)).fetchone()
            if not row:
                raise ValueError("unknown team")
            members = [dict(x) for x in db.execute("SELECT * FROM team_members WHERE team_id=? AND status='active' ORDER BY joined_at", (team_id,))]
            subtasks = [dict(x) for x in db.execute("SELECT * FROM team_subtasks WHERE team_id=? ORDER BY created_at", (team_id,))]
            result = dict(row); result["members"] = members; result["subtasks"] = subtasks
            return result

    def get_subtask(self, subtask_id: str) -> Dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM team_subtasks WHERE id=?", (subtask_id,)).fetchone()
            if not row: raise ValueError("unknown subtask")
            return dict(row)

    def get_proposal(self, proposal_id: str) -> Dict[str, Any]:
        with self._conn() as db:
            row = db.execute("SELECT * FROM role_proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row: raise ValueError("unknown proposal")
            result = dict(row)
            result["votes"] = [dict(x) for x in db.execute("SELECT * FROM role_votes WHERE proposal_id=?", (proposal_id,))]
            return result

    def list_for_agent(self, agent: str, limit: int = 12) -> List[Dict[str, Any]]:
        with self._conn() as db:
            rows = db.execute("SELECT t.id FROM teams t JOIN team_members m ON m.team_id=t.id WHERE m.agent_id=? AND m.status='active' AND t.status!='closed' ORDER BY t.updated_at DESC LIMIT ?", (agent, limit)).fetchall()
        return [self.get(r["id"]) for r in rows]

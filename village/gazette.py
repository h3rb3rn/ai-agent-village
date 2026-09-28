"""AI Village Gazette: a daily edition written by the residents themselves.

Operator directive (2026-09-28): a daily paper covering village events and
each resident's own activity, motivation and goals - built on the previous
day's edition, meant to later serve as a chronicle for "the historian"
(09-chronicler's own role). Short, factual, bounded contributions per agent,
not essays. A daily "game" instead of a sports section, drawn fresh each day
and coordinated by King, who assigns the pairing. Interviews cover state of
mind, wishes for the community, what blocked or moved them, and improvement
suggestions - plus two additions: a lesson learned (closes the loop with
village/auditor.py's failure tally, P46) and a short outlook for tomorrow.

This module only owns the data model and King's daily coordination
(opening an edition, drawing the game). Compiling the raw contributions into
a rendered HTML/PDF edition is a separate, later stage (see
docs/analysis/GAZETTE-PLAN-2026-09-28.md) - kept out of this package so the
foundation can be tested and deployed on its own.
"""
from __future__ import annotations
import random
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Bounded, fact-oriented interview categories - "nur ein kleiner Teil pro
# Agent", never a free essay. Two beyond the operator's own list: LEARNING
# closes the loop with the Auditor's failure tally (P46) into something the
# agent itself narrates rather than a raw count; OUTLOOK is the forward-
# looking planning piece from the operator's original "vorausschauend und
# langfristig" ambition.
CONTRIBUTION_KINDS = (
    "state",       # Verfassung: how the day went, factually
    "mood",        # Stimmungsbild
    "wishes",      # Wuensche an die Gemeinschaft / das Dorf
    "topics",      # Themen die bewegen, worueber gestolpert / was blockiert
    "suggestions", # Verbesserungsvorschlaege fuer eigene Arbeit und Gemeinschaft
    "learning",    # Erkenntnis des Tages - ties into the Auditor's failure tally
    "outlook",     # Ausblick auf morgen
    "game_result", # Ergebnis/Verlauf des Tagesspiels, nur fuer die geloosten Teilnehmer
    "village_news",# eine faktische Kurzmeldung zum Dorfgeschehen, jeder darf
)
MAX_CONTRIBUTION_CHARS = 400

# A small, low-format-risk pool of daily "games" in place of a sports
# section - each expressible in one or two short board messages, nothing
# that needs multi-turn state tracking a 3-9B model would struggle to hold.
GAME_POOL = (
    "Wissens-Ratequiz: einer stellt eine Frage aus der eigenen Knowledgebase, der andere raet",
    "Haiku-Duell zu einem Tagesthema, King kuert einen Favoriten",
    "Pro/Contra-Debatte: je ein kurzes Argument zu einem von King gestellten Streitpunkt",
    "Schaetzfrage zu einem Ressourcenmesswert, der der Wahrheit am naechsten kommt gewinnt",
    "Wortkette: abwechselnd ein Wort anhaengen, das mit dem letzten Buchstaben beginnt",
)


class GazetteStore:
    """SQLite-backed edition/contribution store, reusing coordination.sqlite3
    like TeamStore and MeetingStore already do."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("""CREATE TABLE IF NOT EXISTS gazette_editions(
                id TEXT PRIMARY KEY, status TEXT NOT NULL, opened_by TEXT NOT NULL,
                opened_at TEXT NOT NULL, game_name TEXT, game_pair TEXT,
                compiled_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS gazette_contributions(
                id INTEGER PRIMARY KEY AUTOINCREMENT, edition_id TEXT NOT NULL,
                agent TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(edition_id, agent, kind),
                FOREIGN KEY(edition_id) REFERENCES gazette_editions(id) ON DELETE CASCADE)""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_gazette_contrib_edition ON gazette_contributions(edition_id)")
            # P53: a generic "pick any kind" hint proved too weak to actually
            # produce contributions (live observation: 0 after ~30 min
            # across all 9 residents despite a confirmed-delivered hint -
            # docs/evidence/P53.md). Real per-agent delegation: each resident
            # gets one specific, deterministically assigned kind at open
            # time, same as the game pairing is already drawn - no reliance
            # on King separately messaging anyone (that already failed once).
            c.execute("""CREATE TABLE IF NOT EXISTS gazette_assignments(
                edition_id TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL,
                PRIMARY KEY(edition_id, agent),
                FOREIGN KEY(edition_id) REFERENCES gazette_editions(id) ON DELETE CASCADE)""")
            c.commit()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def open_edition(self, king_agent: str, peers: List[str], edition_id: Optional[str] = None,
                     rng: Optional[random.Random] = None) -> Dict[str, Any]:
        """Idempotent: opening today's edition twice returns the same one
        (same game/pairing) rather than re-drawing - King's own repeated
        action must not reshuffle an edition already announced to peers."""
        eid = edition_id or today()
        existing = self.get_edition(eid)
        if existing:
            return existing
        rng = rng or random.Random()
        game = rng.choice(GAME_POOL)
        pair = rng.sample(peers, 2) if len(peers) >= 2 else list(peers)
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO gazette_editions(id,status,opened_by,opened_at,game_name,game_pair,compiled_at) "
                "VALUES(?,?,?,?,?,?,NULL)",
                (eid, "open", king_agent, now(), game, ",".join(pair)),
            )
            c.commit()
        return self.get_edition(eid)  # type: ignore

    def assign_kinds(self, edition_id: str, king_agent: str, peers: List[str],
                      rng: Optional[random.Random] = None) -> Dict[str, str]:
        """King's own, real delegation act (P54): a generic "pick any kind"
        hint (P52) and even an automatic, silently-computed assignment
        attributed to King in text only (P53's first version) both proved
        insufficient/dishonest - this method is only ever invoked from
        King's own gazette_operation(operation='assign') call, so the
        resulting assignment is genuinely his action, not a background
        computation wearing his name. Idempotent per edition, like
        open_edition(): King re-running it never reshuffles an assignment
        peers may already be acting on.
        """
        existing = self.get_edition(edition_id)
        if not existing:
            raise ValueError(f"unknown gazette edition: {edition_id}")
        if existing["assignments"]:
            return existing["assignments"]
        rng = rng or random.Random()
        assignable_kinds = [k for k in CONTRIBUTION_KINDS if k != "game_result"]
        shuffled_kinds = list(assignable_kinds)
        rng.shuffle(shuffled_kinds)
        roster = [king_agent] + [p for p in peers if p != king_agent]
        rows = [(edition_id, agent, shuffled_kinds[i % len(shuffled_kinds)]) for i, agent in enumerate(roster)]
        with self._conn() as c:
            c.executemany(
                "INSERT OR IGNORE INTO gazette_assignments(edition_id,agent,kind) VALUES(?,?,?)",
                rows,
            )
            c.commit()
        return {agent: kind for _, agent, kind in rows}

    def get_assignment(self, edition_id: str, agent: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT kind FROM gazette_assignments WHERE edition_id=? AND agent=?", (edition_id, agent)
            ).fetchone()
            return row["kind"] if row else None

    def get_edition(self, edition_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM gazette_editions WHERE id=?", (edition_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["game_pair"] = result["game_pair"].split(",") if result["game_pair"] else []
            result["contributions"] = [
                dict(r) for r in c.execute(
                    "SELECT * FROM gazette_contributions WHERE edition_id=? ORDER BY created_at", (edition_id,)
                )
            ]
            result["assignments"] = {
                r["agent"]: r["kind"] for r in c.execute(
                    "SELECT agent, kind FROM gazette_assignments WHERE edition_id=?", (edition_id,)
                )
            }
            return result

    def list_editions(self, limit: int = 30) -> List[Dict[str, Any]]:
        with self._conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM gazette_editions ORDER BY id DESC LIMIT ?", (limit,)
            )]
        return [self.get_edition(i) for i in ids]  # type: ignore

    def submit_contribution(self, edition_id: str, agent: str, kind: str, content: str) -> Dict[str, Any]:
        if kind not in CONTRIBUTION_KINDS:
            raise ValueError(f"unknown gazette contribution kind: {kind}")
        content = str(content).strip()[:MAX_CONTRIBUTION_CHARS]
        if not content:
            raise ValueError("gazette contribution requires non-empty content")
        edition = self.get_edition(edition_id)
        if not edition:
            raise ValueError(f"unknown gazette edition: {edition_id}")
        if kind == "game_result" and agent not in edition["game_pair"]:
            raise ValueError(f"only today's drawn pair {edition['game_pair']} may submit a game_result")
        ts = now()
        with self._conn() as c:
            c.execute(
                "INSERT INTO gazette_contributions(edition_id,agent,kind,content,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(edition_id,agent,kind) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                (edition_id, agent, kind, content, ts, ts),
            )
            c.commit()
        return self.get_edition(edition_id)  # type: ignore

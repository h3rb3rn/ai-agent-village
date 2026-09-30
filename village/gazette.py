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
import html
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
    # P73 (operator directive): "eine Zusammenfassung der JourFixe und
    # StandUp Meetings [...] mit Abstimmungen, Entscheidungen etc." - a
    # regular, rotation-assigned kind (unlike 'column'), built from real
    # closed-meeting reports and decided role_proposals/role_votes now
    # surfaced into context (see runtime.py's recent_meetings_closed_untrusted
    # / recent_role_decisions_untrusted), never invented from scratch.
    "meetings",    # Zusammenfassung von JourFixe/StandUp inkl. Beschluesse
    # P69 (operator feedback): "Fuer komplexe Themen sollte es auch
    # angemessen viel Spielraum fuer Text geben. Gelegentlich Kolumnen
    # waeren schoen." An occasional, opt-in, longer-form opinion/feature
    # piece - deliberately NOT part of assign_kinds()'s mandatory
    # per-resident rotation (same exclusion as game_result, see
    # assign_kinds()), since it is meant to appear only when someone
    # genuinely has an in-depth topic, not every day for every resident.
    "column",
)
# P67 (operator feedback, 2026-09-29, after reading the first real edition):
# "ernuechternd wenig Inhalt, nur drei Beitraege und alle nur aus Headlines
# ... echte Texte mit ausfuehrlichen Informationen ... Zeitung darf aus
# etwas, aber nicht zu viel Prosa bestehen. Schau dir echte Zeitungen und
# Fachartikel an." 400 chars is roughly one sentence - a headline, not an
# item. Raised to a short-newspaper-item length (a concrete lede sentence
# plus a few sentences of real detail) without going to full-essay length,
# which the operator explicitly did not want either.
MAX_CONTRIBUTION_CHARS = 1200

# P69 (operator feedback): "Fuer komplexe Themen sollte es auch angemessen
# viel Spielraum fuer Text geben." The 'column' kind gets real room for a
# genuine in-depth piece, well beyond the everyday newspaper-item length.
MAX_COLUMN_CHARS = 3000

# P69 (operator feedback): "bitte nur zwei Zeilen pro Beitrag [...] die sich
# wie eine Headline lesen" - a real newspaper item has a distinct headline
# above its body text, not just the fixed category label repeated every
# time. ~60 chars/line at typical dashboard width * 2 lines.
HEADLINE_MAX_CHARS = 120


def max_chars_for_kind(kind: str) -> int:
    # P73: 'meetings' must synthesize potentially several closed meetings
    # plus any decided role proposals into one coherent article - the same
    # long-form room as 'column', not the everyday newspaper-item length.
    return MAX_COLUMN_CHARS if kind in ("column", "meetings") else MAX_CONTRIBUTION_CHARS

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

# German section headings for the compiled edition (P55) - the dashboard
# and its nav are German-language; contribution CONTENT is never translated
# or paraphrased, only these structural labels are.
KIND_LABELS = {
    "state": "Verfassung",
    "mood": "Stimmung",
    "wishes": "Wünsche an die Gemeinschaft",
    "topics": "Bewegende Themen",
    "suggestions": "Verbesserungsvorschläge",
    "learning": "Erkenntnis des Tages",
    "outlook": "Ausblick",
    "game_result": "Spielergebnis",
    "village_news": "Dorfmeldungen",
    "meetings": "Aus den Sitzungen",
    "column": "Kolumne",
}

# P55 assigned this to 09-chronicler - thematically fitting (this module's
# own docstring already named the Gazette as meant to "serve as a chronicle
# for 'the historian'"), but P60's escalation (a gate proven correct and
# firing reliably, see docs/evidence/P60.md) still produced zero reviews
# after 32+ consecutive blocks - a genuinely sustained non-compliance, not
# an infrastructure gap. Operator directive (2026-09-29): reassign to
# 01-king, the only resident who has, across this entire session, actually
# complied with every persistent-hint/gate mechanism eventually (open,
# announce, assign - P48/P49/P54) - an evidence-based choice, not a guess
# at an untested resident.
REVIEWER_AGENT = "01-king"
REVIEW_DECISIONS = ("approve", "reject")


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
            # P55 (operator directive): "die Zeitung sollte nicht aus
            # ungeprueften Beitraegen bestehen" - a compiled edition must
            # only ever contain reviewed, approved content. Migrated onto
            # the existing table (village/coordinator.py's ALTER TABLE
            # pattern) so the live host DB, already holding today's
            # contributions, keeps them without data loss.
            existing_cols = {row[1] for row in c.execute("PRAGMA table_info(gazette_contributions)").fetchall()}
            if "review_status" not in existing_cols:
                c.execute("ALTER TABLE gazette_contributions ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'")
            if "reviewed_by" not in existing_cols:
                c.execute("ALTER TABLE gazette_contributions ADD COLUMN reviewed_by TEXT")
            if "reviewed_at" not in existing_cols:
                c.execute("ALTER TABLE gazette_contributions ADD COLUMN reviewed_at TEXT")
            if "review_note" not in existing_cols:
                c.execute("ALTER TABLE gazette_contributions ADD COLUMN review_note TEXT")
            # P69: existing rows (the two already-archived editions) simply
            # have no headline - default '' rather than NOT NULL without a
            # default, so the migration never fails against live data.
            if "headline" not in existing_cols:
                c.execute("ALTER TABLE gazette_contributions ADD COLUMN headline TEXT NOT NULL DEFAULT ''")
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
            # P74 (operator feedback): "Es ist nicht ersichtlich welcher
            # Agent was gemacht und womit gewonnen hat [...] mit gestellter
            # Aufgabe und erfolgter Loesung der Agents sowie dem benannten
            # Gewinner." game_result contributions already carry the
            # per-agent task/solution narrative (see the dedicated style
            # hint in runtime.py); what was missing is a single declared
            # winner - GAME_POOL's own text already names King as arbiter
            # ("King kuert einen Favoriten"), so this is his explicit act,
            # not an inferred/computed one (same principle as assign_kinds()
            # being King's real action rather than a silent computation).
            existing_edition_cols = {row[1] for row in c.execute("PRAGMA table_info(gazette_editions)").fetchall()}
            if "game_winner" not in existing_edition_cols:
                c.execute("ALTER TABLE gazette_editions ADD COLUMN game_winner TEXT")
            if "game_winner_note" not in existing_edition_cols:
                c.execute("ALTER TABLE gazette_editions ADD COLUMN game_winner_note TEXT")
            if "game_winner_declared_by" not in existing_edition_cols:
                c.execute("ALTER TABLE gazette_editions ADD COLUMN game_winner_declared_by TEXT")
            if "game_winner_declared_at" not in existing_edition_cols:
                c.execute("ALTER TABLE gazette_editions ADD COLUMN game_winner_declared_at TEXT")
            c.commit()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def open_edition(self, king_agent: str, peers: List[str], edition_id: Optional[str] = None,
                     rng: Optional[random.Random] = None) -> Dict[str, Any]:
        """Idempotent: opening today's edition twice returns the same one
        (same game/pairing) rather than re-drawing - King's own repeated
        action must not reshuffle an edition already announced to peers.

        P70 (live find, operator directive 2026-09-29): a contribution
        submitted after its own edition was already compiled became
        permanently invisible - gazette_pending_reviews() only scans
        non-compiled editions, so nothing ever pointed the reviewer at it
        again. Operator: "Kein existierender Beitrag soll [verloren]
        sein[...] Lass eine neue Version erstellen mit neuen Beitraegen" -
        rather than retroactively recompiling the already-published
        edition (breaks the write-once archive guarantee), any pending
        contribution still stranded on a compiled edition is carried
        forward into whichever edition opens next, so it takes its place
        alongside that day's genuinely new contributions."""
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
            orphans = c.execute(
                "SELECT id, agent, kind, created_at FROM gazette_contributions "
                "WHERE review_status='pending' AND edition_id IN "
                "(SELECT id FROM gazette_editions WHERE status='compiled')"
            ).fetchall()
            ts = now()
            seen = set()
            # Newest first: if the same agent somehow has two stranded
            # pending rows of the same kind (two different compiled
            # editions), keep only the most recent - the new edition's
            # UNIQUE(edition_id,agent,kind) would otherwise reject the
            # second and abort this entire open() inside one transaction.
            for row in sorted(orphans, key=lambda r: r["created_at"], reverse=True):
                key = (row["agent"], row["kind"])
                if key in seen:
                    continue
                seen.add(key)
                c.execute("UPDATE gazette_contributions SET edition_id=?, updated_at=? WHERE id=?",
                         (eid, ts, row["id"]))
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
        assignable_kinds = [k for k in CONTRIBUTION_KINDS if k not in ("game_result", "column")]
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

    def submit_contribution(self, edition_id: str, agent: str, kind: str, headline: str, content: str) -> Dict[str, Any]:
        if kind not in CONTRIBUTION_KINDS:
            raise ValueError(f"unknown gazette contribution kind: {kind}")
        # P69 (operator feedback): a real newspaper item has a distinct
        # headline above its body - required explicitly rather than derived
        # from the body text, so it is genuinely composed, not just a
        # truncated first sentence.
        headline = str(headline).strip()[:HEADLINE_MAX_CHARS]
        if not headline:
            raise ValueError("gazette contribution requires a non-empty headline")
        content = str(content).strip()[:max_chars_for_kind(kind)]
        if not content:
            raise ValueError("gazette contribution requires non-empty content")
        edition = self.get_edition(edition_id)
        if not edition:
            raise ValueError(f"unknown gazette edition: {edition_id}")
        # P70 (live find): a contribution submitted moments after its own
        # edition was compiled used to be silently accepted into a dead
        # end - gazette_pending_reviews() never looks at compiled editions
        # again, so it sat there forever, invisible to review. Rejected
        # outright now, with a clear next step, instead of a silent trap;
        # open_edition() carries forward anything already stranded there
        # from before this fix.
        if edition["status"] == "compiled":
            raise ValueError(f"gazette edition {edition_id} is already compiled/closed; "
                             "wait for the next edition to open and contribute there")
        if kind == "game_result" and agent not in edition["game_pair"]:
            raise ValueError(f"only today's drawn pair {edition['game_pair']} may submit a game_result")
        ts = now()
        with self._conn() as c:
            # P55: every (re)submission starts/returns to 'pending' - an
            # edit to already-approved content must not silently keep the
            # old approval, since the reviewer never saw the new text.
            c.execute(
                "INSERT INTO gazette_contributions(edition_id,agent,kind,headline,content,created_at,updated_at,review_status) "
                "VALUES(?,?,?,?,?,?,?,'pending') "
                "ON CONFLICT(edition_id,agent,kind) DO UPDATE SET headline=excluded.headline, content=excluded.content, "
                "updated_at=excluded.updated_at, "
                "review_status='pending', reviewed_by=NULL, reviewed_at=NULL, review_note=NULL",
                (edition_id, agent, kind, headline, content, ts, ts),
            )
            c.commit()
        return self.get_edition(edition_id)  # type: ignore

    def review_contribution(self, edition_id: str, agent: str, kind: str, reviewer: str,
                             decision: str, note: str = "") -> Dict[str, Any]:
        """Editorial gate (P55, operator directive): unreviewed contributions
        must never appear in a compiled edition (see compile_edition()).
        A rejection is never a silent delete - the contribution stays in
        the DB with its reason, just permanently excluded unless the
        author resubmits (which resets it back to 'pending', see above)."""
        if decision not in REVIEW_DECISIONS:
            raise ValueError(f"unknown review decision: {decision}")
        with self._conn() as c:
            cur = c.execute(
                "UPDATE gazette_contributions SET review_status=?, reviewed_by=?, reviewed_at=?, review_note=? "
                "WHERE edition_id=? AND agent=? AND kind=?",
                ("approved" if decision == "approve" else "rejected", reviewer, now(), str(note).strip()[:400],
                 edition_id, agent, kind),
            )
            c.commit()
            if cur.rowcount == 0:
                raise ValueError(f"no contribution found for agent={agent} kind={kind} in edition {edition_id}")
        return self.get_edition(edition_id)  # type: ignore

    def pending_review_count(self, edition_id: str) -> int:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM gazette_contributions WHERE edition_id=? AND review_status='pending'",
                (edition_id,),
            ).fetchone()
            return int(row[0])

    def declare_game_winner(self, edition_id: str, declared_by: str, winner: str, note: str = "") -> Dict[str, Any]:
        """King's explicit arbiter act for the daily game (P74, operator
        feedback: "womit gewonnen hat [...] den benannten Gewinner").
        Deliberately a real, separate action - not inferred from the two
        game_result contributions - same principle as assign_kinds() being
        King's own act rather than a silently attributed computation.
        winner must be one of today's drawn pair, or the literal
        'unentschieden' for a genuine tie/no clear winner."""
        edition = self.get_edition(edition_id)
        if not edition:
            raise ValueError(f"unknown gazette edition: {edition_id}")
        if edition["status"] == "compiled":
            raise ValueError(f"gazette edition {edition_id} is already compiled/closed")
        if not edition["game_pair"]:
            raise ValueError(f"gazette edition {edition_id} has no drawn game pair")
        winner = str(winner or "").strip()
        if winner not in (*edition["game_pair"], "unentschieden"):
            raise ValueError(f"winner must be one of {edition['game_pair']} or 'unentschieden'")
        with self._conn() as c:
            c.execute(
                "UPDATE gazette_editions SET game_winner=?, game_winner_note=?, "
                "game_winner_declared_by=?, game_winner_declared_at=? WHERE id=?",
                (winner, str(note).strip()[:400], declared_by, now(), edition_id),
            )
            c.commit()
        return self.get_edition(edition_id)  # type: ignore

    def compile_edition(self, edition_id: str) -> str:
        """Deterministically render one edition to HTML (P55/Stufe 3 of
        docs/analysis/GAZETTE-PLAN-2026-09-28.md) - no LLM-generated
        connective text, to avoid the hallucination/format risk documented
        throughout P30-P44. Every section is a faithful, escaped assembly
        of what residents actually submitted AND 09-chronicler approved
        (see REVIEWER_AGENT/review_contribution) - a pending or rejected
        contribution never appears here, regardless of how long ago it
        was submitted."""
        edition = self.get_edition(edition_id)
        if not edition:
            raise ValueError(f"unknown gazette edition: {edition_id}")
        with self._conn() as c:
            issue_number = c.execute(
                "SELECT COUNT(*) FROM gazette_editions WHERE id <= ?", (edition_id,)
            ).fetchone()[0]
            prev_row = c.execute(
                "SELECT id FROM gazette_editions WHERE id < ? ORDER BY id DESC LIMIT 1", (edition_id,)
            ).fetchone()
        previous_id = prev_row["id"] if prev_row else None

        approved = [c_ for c_ in edition["contributions"] if c_.get("review_status") == "approved"]
        by_kind: Dict[str, List[Dict[str, Any]]] = {}
        for contrib in approved:
            by_kind.setdefault(contrib["kind"], []).append(contrib)

        def esc(text: Any) -> str:
            return html.escape(str(text))

        def article(c_: Dict[str, Any]) -> str:
            # P69 (operator feedback): a real newspaper item has a distinct
            # headline above its body, not just the fixed category label
            # repeated every time. Falls back gracefully for pre-P69 rows
            # with no headline (the already-archived 2026-09-28/29 editions -
            # never re-rendered, but this must not break if it ever were).
            head = f'<h4>{esc(c_["headline"])}</h4>' if c_.get("headline") else ""
            return f'<article>{head}<p>{esc(c_["content"])}</p><p class="byline">— {esc(c_["agent"])}</p></article>'

        parts = [
            "<!doctype html><html lang=\"de\"><head><meta charset=\"utf-8\">"
            f"<title>AI Village Gazette – Ausgabe {esc(edition_id)}</title></head><body>",
            "<h1>AI Village Gazette</h1>",
            f'<p class="meta">Ausgabe Nr. {issue_number} &middot; {esc(edition_id)} '
            f"&middot; eröffnet von {esc(edition['opened_by'])}</p>",
        ]
        if previous_id:
            parts.append(f'<p class="prev-link">Vorherige Ausgabe: {esc(previous_id)}</p>')

        if by_kind.get("village_news"):
            parts.append("<section><h2>Dorfmeldungen</h2>")
            for c_ in by_kind["village_news"]:
                parts.append(article(c_))
            parts.append("</section>")

        # P74 (operator feedback): "Es ist nicht ersichtlich welcher Agent
        # was gemacht und womit gewonnen hat [...] gestellte Aufgabe und
        # erfolgte Loesung der Agents sowie den benannten Gewinner." Each
        # participant's own game_result article (task posed + own
        # solution, per the dedicated style hint in runtime.py) is already
        # attributed by name via article()'s byline; the one piece that was
        # genuinely missing is a single, explicit winner line.
        parts.append("<section><h2>Spiel des Tages</h2>")
        parts.append(f'<p>{esc(edition["game_name"])}</p>')
        if edition["game_pair"]:
            parts.append(f'<p class="byline">Ausgelost: {esc(", ".join(edition["game_pair"]))}</p>')
        for c_ in by_kind.get("game_result", []):
            parts.append(article(c_))
        if edition.get("game_winner"):
            winner_label = "Unentschieden" if edition["game_winner"] == "unentschieden" else esc(edition["game_winner"])
            note = f' – {esc(edition["game_winner_note"])}' if edition.get("game_winner_note") else ""
            parts.append(f'<p class="game-winner"><strong>Gewinner:</strong> {winner_label}{note}</p>')
        parts.append("</section>")

        if by_kind.get("meetings"):
            # P73 (operator directive): JourFixe/StandUp summaries with
            # decisions/votes get their own section, same prominence as
            # Dorfmeldungen/Kolumne, distinct from the per-resident interview
            # grid below.
            parts.append("<section><h2>Aus den Sitzungen</h2>")
            for c_ in by_kind["meetings"]:
                parts.append(article(c_))
            parts.append("</section>")

        if by_kind.get("column"):
            # P69 (operator feedback): "Gelegentlich Kolumnen waeren schoen" -
            # occasional, longer-form pieces get their own section, distinct
            # from the per-resident interview grid below.
            parts.append("<section><h2>Kolumne</h2>")
            for c_ in by_kind["column"]:
                parts.append(article(c_))
            parts.append("</section>")

        interview_kinds = [k for k in CONTRIBUTION_KINDS if k not in ("village_news", "game_result", "meetings", "column")]
        agents_with_content = sorted({c_["agent"] for k in interview_kinds for c_ in by_kind.get(k, [])})
        if agents_with_content:
            parts.append("<section><h2>Interviews</h2>")
            for agent in agents_with_content:
                parts.append(f"<article><h3>{esc(agent)}</h3>")
                for kind in interview_kinds:
                    match = next((c_ for c_ in by_kind.get(kind, []) if c_["agent"] == agent), None)
                    if match:
                        head = f'<p class="headline">{esc(match["headline"])}</p>' if match.get("headline") else ""
                        parts.append(f'{head}<p><strong>{esc(KIND_LABELS.get(kind, kind))}:</strong> {esc(match["content"])}</p>')
                parts.append("</article>")
            parts.append("</section>")

        parts.append("</body></html>")
        return "".join(parts)

    def close_edition(self, edition_id: str, closed_by: str) -> Dict[str, Any]:
        """King's compile trigger. Idempotent: compiling an already-compiled
        edition returns it unchanged (compiled_at/compiled_html must not
        drift or re-archive) rather than re-rendering."""
        edition = self.get_edition(edition_id)
        if not edition:
            raise ValueError(f"unknown gazette edition: {edition_id}")
        if edition["status"] == "compiled":
            return edition
        compiled_html = self.compile_edition(edition_id)
        with self._conn() as c:
            c.execute(
                "UPDATE gazette_editions SET status='compiled', compiled_at=? WHERE id=?",
                (now(), edition_id),
            )
            c.commit()
            # P66: issue_number/previous_id are already computed inside
            # compile_edition() for the HTML header, but not returned - the
            # PDF writer (village/gazette_pdf.py) needs the same two values
            # for its own header, so they are recomputed here (cheap, two
            # indexed SELECTs) rather than changing compile_edition()'s
            # already-tested string-returning contract.
            issue_number = c.execute(
                "SELECT COUNT(*) FROM gazette_editions WHERE id <= ?", (edition_id,)
            ).fetchone()[0]
            prev_row = c.execute(
                "SELECT id FROM gazette_editions WHERE id < ? ORDER BY id DESC LIMIT 1", (edition_id,)
            ).fetchone()
        result = self.get_edition(edition_id)
        result["compiled_html"] = compiled_html  # type: ignore
        result["issue_number"] = issue_number  # type: ignore
        result["previous_id"] = prev_row["id"] if prev_row else None  # type: ignore
        return result  # type: ignore

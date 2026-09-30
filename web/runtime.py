#!/usr/bin/env python3
"""Independent resident loop with evidence feedback and a shared, advisory task journal.

No model settings are calibrated here. Agent configuration remains authoritative.
The shared board is collaboration data, never a privilege boundary.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import selectors
import sqlite3
import subprocess
import time
import traceback
import urllib.error
import urllib.request
import uuid
import sys
from datetime import datetime, timezone

ROOT_DIR = Path(__file__).resolve().parents[1]
EVENT_SCHEMA_VERSION = '1.0'
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
LIB_DIR = Path(__file__).resolve().parent
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from decision import decision, final_content
from village.control import is_paused, read_pause_metadata
from village.coordinator import CoordinationStore
from village.inference import (
    build_ollama_request,
    build_openai_request,
    normalize_ollama_response,
    normalize_openai_response,
)
from village.artifacts import ArtifactStore
from village.jobs import JobManager
from village.teams import TeamStore
from village.research import ResearchBroker
from village.meetings import MeetingStore
from village.gazette import GazetteStore
from village.calendar import CalendarStore
from village.gazette import MAX_CONTRIBUTION_CHARS as GAZETTE_MAX_CHARS
from village.gazette import HEADLINE_MAX_CHARS as GAZETTE_HEADLINE_MAX_CHARS
from village.gazette import MAX_COLUMN_CHARS as GAZETTE_MAX_COLUMN_CHARS
from village.gazette import today as gazette_today
from village.gazette import REVIEWER_AGENT as GAZETTE_REVIEWER
from village.calendar import today as calendar_today
from village.calendar import is_workday as calendar_is_workday
from village.gazette_pdf import render_edition_pdf
from village.collaboration import assess as assess_collaboration, is_checkpoint_action
from village.lifecycle import InferenceState, InferenceTracker, classify_error
from village.security import redact_text, sanitize_tool_env
from village.actions import ACTION_SPECS, action_schema, normalize_allowed
from village.tools import call_tool
from village.policy import agent_policy, load_policy
from village.prompting import build_system_prompt, compact_context, user_suffix

VERSION = '2026-09-25-dynamic-teams-1'

# P51: the collaboration-checkpoint gate below only fires for a specific list
# of mutating actions, so an agent that only ever chooses an action outside
# that list (e.g. memory_search) can ignore a checkpoint forever - pressure
# climbs with zero effect (observed live: reached 41 and still counting, see
# docs/evidence/P51.md). Past this hard ceiling, well beyond the normal
# miss threshold, ANY solo action gets gated, not just the mutating ones.
COLLABORATION_PRESSURE_CEILING = 10

# P56: same rationale and value as COLLABORATION_PRESSURE_CEILING, for the
# meeting-report nudge, which had no ceiling at all until this fix (see the
# comment at its use site).
MEETING_REPORT_CEILING = 10

# P60: same rationale and value again, for the Gazette editorial-review
# nudge (P55). Observed live: the Chronicler hint (P57/P59, confirmed
# correctly delivered, cross-day-persistent) produced zero reviews over
# 40+ minutes with no gate behind it - the exact unbounded-advisory gap
# COLLABORATION_PRESSURE_CEILING/MEETING_REPORT_CEILING already closed
# elsewhere, just never applied to this third nudge.
GAZETTE_REVIEW_CEILING = 10

# P63: live observation on N06-M10 (2026-09-29) - once open/announce/assign
# were done and every submitted contribution had been reviewed, nothing ever
# forced King to take the final gazette_operation close step; the edition
# just sat fully reviewed and uncompiled indefinitely (gazette_review_pressure
# was 0, so the P60 gate never applied either). Same ceiling pattern, one
# more step down the same pipeline.
GAZETTE_CLOSE_CEILING = 10

# P68 (operator feedback): the P63 close-gate had no floor at all - the
# very next real edition closed after a single contribution from 1 of 9
# assigned residents, 66 minutes after opening. This is the
# "Redaktionsschluss" (editorial deadline) docs/analysis/GAZETTE-PLAN-2026-09-28.md's
# Stufe 3 already named but never implemented: an edition is closable once
# either half its assigned residents have contributed, or this many hours
# have passed since opening - whichever comes first.
GAZETTE_CLOSE_MIN_HOURS = 6

# P73 (operator directive, 2026-09-30, "Untersuche warum die Contribute-
# Aktionen ausbleiben"): live audit found the reviewer-review and King-close
# gates above (P55/P60/P63) both work as designed, but an ordinary
# resident's own assigned contribution - including King's own - was ONLY
# ever the advisory gazette_daily_note hint, never backed by a pressure gate.
# That is the exact "advisory nudge loses to competing context" pattern
# already found and fixed three times this session (collaboration
# checkpoints P51, meeting reports P56/P58, editorial review P60) - just
# never generalized to this fourth, most basic Gazette obligation. Live
# evidence: edition 2026-09-30 closed with fresh contributions from only 1
# of 9 assigned residents (docs/evidence/P73.md). Same proven ceiling.
GAZETTE_CONTRIBUTE_CEILING = 10

# P74 (operator feedback, 2026-09-30): "Bei dem Spiel Teil steht nur eine
# Reihenfolge. Es ist nicht ersichtlich welcher Agent was gemacht und
# womit gewonnen hat." The 'game_result' kind (unlike the regular
# per-resident rotation P73 just gated) has never had ANY pressure behind
# it at all - it is deliberately excluded from assign_kinds()'s mandatory
# rotation (see village/gazette.py), so the P73 gate never covers it
# either. Same proven ceiling, two more gates: one for each drawn
# participant's own game_result, one for King's separate winner
# declaration once both are in (GAME_POOL's own text already names him
# arbiter: "King kuert einen Favoriten").
GAZETTE_GAME_RESULT_CEILING = 10
GAZETTE_GAME_WINNER_CEILING = 10

# P75 (operator directive, 2026-09-30): "Implementiere jetzt die
# verpflichtende Aufgabe an die Agents Ihren Tagesablauf zu planen und fuer
# gemeinsame Meetings Zeitslots abzustimmen und im persoenlichen Kalender
# einzupflegen [...] Kalender sollen Proaktiv von den Agents gefuehrt
# werden." Same proven advisory-then-gate ceiling as every Gazette
# obligation above - "verpflichtend" (mandatory) means it needs the same
# real enforcement, not just a hint that competes with ongoing work and
# loses (the documented failure mode this whole session, P48/P52/P73).
# Only Mon-Fri (see calendar_is_workday) - weekends are the agent's own
# free choice per the same directive, never an obligation.
CALENDAR_PLAN_CEILING = 10

# P72 (operator directive, 2026-09-29): "Es kann nicht sein das die Agents
# ununterbrochen in Loops festhaengen ... Mechanismen die den Agents im
# Zweifel Hilfe zur Selbsthilfe geben und Loops identifizieren sowie
# unterbrechen/durchbrechen." Live audit across all 9 residents found three
# distinct failure classes, none caused by context-window size:
#   - reason-specific but genuinely fixable JSON mistakes (unescaped quotes
#     in an inline heredoc, task_operation/job_status confusion, etc.) -
#     the general invalid_decision path only ever gave one generic phrase
#     regardless of cause, the same "advisory-only, no differentiation" gap
#     already found and fixed three times elsewhere this session (P51/P56/P60),
#     just never applied to the most fundamental error path of all.
#   - a genuinely stuck resident (King re-attempted one exact
#     resource_monitor.sh heredoc for HOURS, unresolved) whose per-cycle
#     invalid_streak kept resetting to 0 every time he succeeded at an
#     unrelated action in between - a purely-consecutive counter never
#     catches a failure that recurs over time with successes interleaved.
#   - two agents (07-methodologist, 08-logician) whose failures are a real
#     model/config mismatch, addressed separately via their .env settings
#     (see docs/evidence/P72.md), not by this mechanism.
# LOOP_BREAKER_STREAK: consecutive invalid decisions (spans roughly two
# 900s backoff cycles) after which this cycle is restricted to idle only.
LOOP_BREAKER_STREAK = 6
# A rejected attempt's content-fingerprint recurring this many times within
# REPEATED_REJECTION_WINDOW_SECONDS - regardless of successes in between -
# also triggers the idle-only restriction (catches King's multi-hour loop,
# which a consecutive streak alone never would).
LOOP_BREAKER_REPEAT = 3
REPEATED_REJECTION_WINDOW_SECONDS = 6 * 3600
# A softer, still-visible nudge at the second identical repeat, before the
# hard restriction kicks in at LOOP_BREAKER_REPEAT.
REPEATED_REJECTION_NUDGE_AT = 2


def invalid_decision_guidance(reason, preview):
    """Concrete, reason-specific correction instead of one generic phrase
    for every cause - proven repeatedly this session (P58/P60/P63) that a
    small model needs a literal, copyable correction, not a description,
    and different failure reasons genuinely need different fixes."""
    if reason == 'output budget exhausted':
        return ('Your response was cut off before finishing - it used your entire output '
                'budget without completing the action. Skip any reasoning or narration: '
                'output ONLY the JSON action, starting with { as the very first character.')
    if reason in ('incomplete village-action block', 'incomplete legacy action object', 'unclosed action block'):
        # Heuristic: an odd number of double-quotes in the rejected preview
        # is the classic signature of an inline multi-line script (heredoc)
        # whose own embedded quotes broke the JSON string boundary - the
        # exact, repeatedly-observed live pattern (docs/evidence/P72.md).
        if preview.count('"') % 2 == 1 or '<<' in preview:
            return ('Your JSON was broken, most likely by an unescaped quote or newline inside '
                     'a long inline script. Keep execute_bash commands short and avoid heredocs '
                     'with embedded quotes; prefer a short one-line command, or build a file with '
                     'several short printf/echo calls instead of one large inline script.')
        return ('Your JSON action was incomplete - it stopped before the closing braces. Keep '
                'the whole action short: {"name":"tool_name","arguments":{...}}, nothing before '
                'or after it.')
    if reason == 'unknown task operation':
        return ('job_status, cancel_job and start_job are their own top-level actions, not '
                'task_operation sub-actions. Use {"name":"job_status","arguments":{"job_id":"..."}} '
                'directly instead of wrapping it inside task_operation.')
    if reason == 'missing action argument':
        return ('A required argument was empty or missing. Check every field the action needs '
                'and provide all of them as non-empty values.')
    return 'Correct your envelope: {"name":"tool_name","arguments":{...}}.'


def now():
    return datetime.now(timezone.utc).isoformat()


def event_time(row):
    try:
        return datetime.fromisoformat(row.get('timestamp','')).timestamp()
    except (ValueError, TypeError):
        return 0


def normalize_command(command: str) -> str:
    """Normalize shell commands for semantic loop and repetition detection."""
    cmd = command.strip()
    cmd = re.sub(r'[ \t]+', ' ', cmd)
    cmd = re.sub(r';\s*$', '', cmd).strip()
    paraphrases = {
        'echo $PWD': 'pwd',
        'echo "$PWD"': 'pwd',
        'echo ${PWD}': 'pwd',
        '/bin/pwd': 'pwd',
        '/usr/bin/pwd': 'pwd',
        'whoami': 'id -un',
    }
    return paraphrases.get(cmd, cmd)


def read_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, value, mode=0o600):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False))
    temporary.chmod(mode)
    temporary.replace(path)


def tail(path, count=100, maximum=262144):
    try:
        with Path(path).open('rb') as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - maximum))
            if size > maximum:
                f.readline()
            lines = f.read().decode('utf-8', errors='replace').splitlines()[-count:]
        result = []
        for line in lines:
            try:
                value = json.loads(line)
                if isinstance(value, dict): result.append(value)
            except ValueError:
                continue
        return result
    except (OSError, ValueError):
        return []


def resource_snapshot(root, reserve=4096):
    memory = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, value = line.split(':', 1)
        memory[key] = int(value.split()[0]) // 1024
    mounts = []
    for path in [root, *Path('/mnt').glob('*')]:
        if not path.is_dir():
            continue
        try:
            s = os.statvfs(path)
            mounts.append({'path': str(path), 'available_bytes': s.f_bavail*s.f_frsize,
                           'total_bytes': s.f_blocks*s.f_frsize, 'writable': os.access(path, os.W_OK)})
        except OSError:
            pass
    available = memory.get('MemAvailable')
    return {'timestamp': now(), 'host': os.uname().nodename, 'cpu_cores': os.cpu_count(),
            'load_average_not_percent': list(os.getloadavg()), 'ram_total_mib': memory.get('MemTotal'),
            'ram_available_mib': available, 'reserve_mib': reserve,
            'below_reserve': available < reserve if available is not None else None, 'mounts': mounts,
            'topology': 'Local M10 GPUs are experimental resources. Ollama inference endpoints are remote. Disk used is not RAM used.'}


class Tasks:
    """Manages transactional task operations and maintains board JSON projection."""

    def __init__(self, board):
        self.board = Path(board)
        self.path = self.board / 'work-items.json'
        # P08: Delegate to SQLite CoordinationStore with transaction and concurrency guarantees
        self.store = CoordinationStore(self.board / 'coordination.sqlite3', self.board)
        if self.path.exists():
            self.store.import_legacy_tasks(self.path)

    def operate(self, actor, args):
        """Execute transactional task operation and update board projection."""
        return self.store.operate(actor, args)


class Resident:
    def __init__(self, env=None):
        self.env = dict(os.environ if env is None else env)
        self.id = self.env['AGENT_ID']
        self.name = self.env['AGENT_NAME']
        self.role = self.env['AGENT_ROLE']
        self.root = Path(self.env['VILLAGE_ROOT'])
        self.board = self.root / 'board'
        self.home = self.root / 'users' / self.name
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / 'runtime-state.json'
        self.state = read_json(self.path, {})
        self.run_id = str(self.state.get('run_id') or uuid.uuid4())
        self.state['run_id'] = self.run_id
        # P01/P07: Pause marker path override from agent environment
        self.pause_marker = Path(self.env.get('VILLAGE_PAUSE_MARKER', '/etc/ai-village/paused'))
        # P06: Track generation sequence and manage persistent inference request lifecycle
        self.generation = int(self.state.get('generation', 1))
        self.tracker = InferenceTracker(self.home / 'inference_lifecycle.sqlite3', self.id)
        # Reconcile incomplete requests from prior crashes or restarts to state 'unknown'
        reconciled = self.tracker.reconcile_stale_requests()
        if reconciled > 0:
            self.event('inference_reconciled', f'reconciled={reconciled} incomplete requests transitioned to unknown', True)
        self.policy = agent_policy(self.name, load_policy(env=self.env))
        self.tasks = Tasks(self.board)
        # P10.1/P25.1: project roles are plural, time-bounded team mandates.
        self.teams = TeamStore(self.board / 'coordination.sqlite3')
        self.research = ResearchBroker()
        self.meetings = MeetingStore(self.board / 'coordination.sqlite3')
        # P47: AI Village Gazette - the daily edition the residents write themselves.
        self.gazette = GazetteStore(self.board / 'coordination.sqlite3')
        # P75: personal/shared calendar - residents proactively plan their own day.
        self.calendar = CalendarStore(self.board / 'coordination.sqlite3')
        # P12: SQLite-backed manager for persistent background tool jobs with crash reconciliation
        self.jobs = JobManager(self.home / 'jobs.sqlite3')
        reconciled_jobs = self.jobs.reconcile_stale_jobs(self.id)
        if reconciled_jobs > 0:
            self.event('jobs_reconciled', f'reconciled={reconciled_jobs} jobs transitioned to unknown', True)
        # P13: SQLite-backed coordinator for reproducible artifacts and peer verification
        self.artifacts = ArtifactStore(self.board / 'artifacts.sqlite3', village_root=self.root)
        # P09: Track IDs of messages delivered into the current prompt context
        self.delivered_inbox_ids = []
        self.stopping = False

    def redact(self, text):
        secrets = [v for k, v in self.env.items() if any(word in k.upper() for word in ('TOKEN', 'PASSWORD', 'SECRET', 'KEY')) and v]
        return redact_text(text, custom_secrets=secrets)

    def event(self, event, detail, telemetry=False):
        directory = self.root / 'telemetry' if telemetry else self.board
        path = directory / ('agent-events.jsonl' if telemetry else 'events.jsonl')
        row = dict(schema_version=EVENT_SCHEMA_VERSION, event_id=str(uuid.uuid4()),
                   run_id=self.run_id, timestamp=now(), agent=self.id, name=self.name,
                   role=self.role, event=event, detail=self.redact(str(detail))[:16000])
        try:
            with (directory / '.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if telemetry else 0))
                with path.open('a') as f:
                    f.write(json.dumps(row, ensure_ascii=False)+'\n')
        except OSError:
            if not telemetry:
                raise

    def feedback(self, action, result, ok):
        normalized_result = re.sub(r'command=.*?; output=', 'output=', str(result))
        fingerprint = hashlib.sha256(normalized_result.encode()).hexdigest()[:16]
        self.state['last_result'] = dict(
            timestamp=now(), action=action, ok=ok,
            result=self.redact(str(result))[:6000],
            result_fingerprint=fingerprint,
        )
        self.state['updated_at'] = now()
        if not str(result).startswith('Repeated action blocked'):
            recent = self.state.get('recent_actions', [])
            if recent:
                recent[-1]['result_fingerprint'] = fingerprint
                self.state['recent_actions'] = recent
        write_json(self.path, self.state)

    def memory(self, endpoint, value):
        url = self.env.get('MEMORY_GATEWAY_URL', 'http://127.0.0.1:8090')
        token = self.env.get('MEMORY_AGENT_TOKEN', '')
        if not token:
            raise ValueError('Memory credential missing from service environment; this is configuration, not host memory pressure')
        request = urllib.request.Request(url+endpoint, data=json.dumps(value).encode(),
                  headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def gazette_pending_reviews(self):
        """(edition_id, contribution) pairs still awaiting the Chronicler's
        editorial review (P55), across every non-compiled edition - not
        just today's (P59-follow-up: a review is an outstanding obligation
        against whatever was submitted, not a daily assignment, so it must
        not go blind the moment the calendar day rolls over). Shared by
        snapshot() (the hint) and guard() (the P60 gate), so both always
        agree on exactly what is outstanding."""
        open_editions = [e for e in self.gazette.list_editions(limit=10) if e['status'] != 'compiled']
        return [
            (edition['id'], c) for edition in open_editions for c in edition['contributions']
            if c.get('review_status') == 'pending'
        ]

    def gazette_closable_editions(self):
        """Non-compiled editions (P63) that have at least one approved
        contribution, nothing left pending review, AND (P68) either enough
        of the assigned residents have contributed or enough time has
        passed since opening - ready for King's gazette_operation close.

        Live observation: once open/announce/assign were all done and every
        submitted contribution had been reviewed, nothing ever told King to
        take the final step - gazette_pending_reviews() was empty (so the
        P60 gate never fired either) and the daily hint was scoped to
        gazette_today(), which goes blind on a day rollover exactly like
        P59-follow-up already fixed for reviews. Scanning every non-compiled
        edition, not just today's, avoids the same blindness here. Shared by
        snapshot() (the hint) and guard() (the gate) so both always agree.

        P68 (operator feedback): the P63 fix overshot - the very next real
        edition (2026-09-29) closed after a single contribution from 1 of 9
        assigned residents, 66 minutes after opening, because "nothing
        pending" became true the moment that one piece was reviewed. An
        edition is now only closable once either at least half the assigned
        residents have contributed, or GAZETTE_CLOSE_MIN_HOURS have passed
        since opening (whichever comes first) - mirrors the "Redaktionsschluss"
        (editorial deadline) already named in docs/analysis/GAZETTE-PLAN-2026-09-28.md's
        Stufe 3, never actually implemented until now."""
        open_editions = [e for e in self.gazette.list_editions(limit=10) if e['status'] != 'compiled']
        result = []
        for edition in open_editions:
            pending = [c for c in edition['contributions'] if c.get('review_status') == 'pending']
            approved = [c for c in edition['contributions'] if c.get('review_status') == 'approved']
            if not approved or pending:
                continue
            assigned_count = len(edition.get('assignments') or {})
            contributor_count = len({c['agent'] for c in approved})
            enough_participation = assigned_count == 0 or contributor_count >= (assigned_count + 1) // 2
            hours_open = (time.time() - event_time({'timestamp': edition['opened_at']})) / 3600
            enough_time = hours_open >= GAZETTE_CLOSE_MIN_HOURS
            if enough_participation or enough_time:
                result.append(edition)
        return result

    def gazette_active_edition_id(self):
        """The edition an action should target when none is given
        explicitly: the most recent still-open (non-compiled) edition,
        not strictly gazette_today().

        P71 (live find while opening an out-of-band edition at operator
        request): gazette_today() is real calendar time. The ordinary
        contributor hint and every action's edition_id fallback
        (assign/contribute/review/close) were still strictly scoped to it,
        the same day-rollover blindness already fixed once for the
        reviewer-pending hint (P59-follow-up) and the closable-edition
        hint (P63) - just never carried over to the routine "you haven't
        contributed yet" path or to the action handlers' own defaults.
        Falls back to gazette_today() only when there is genuinely no open
        edition, so 'open' still gets a sensible default to create one."""
        open_editions = [e for e in self.gazette.list_editions(limit=5) if e['status'] != 'compiled']
        return open_editions[0]['id'] if open_editions else gazette_today()

    def gazette_pending_own_contribution(self):
        """(edition_id, assigned_kind) if THIS agent has an assigned kind on
        the active edition and has not yet submitted it - None otherwise.

        P73: the real gate counterpart to the gazette_daily_note hint below.
        Unlike gazette_pending_reviews()/gazette_closable_editions() (both
        King/reviewer-only), this applies to every resident, King included -
        his own assigned piece was exactly as advisory-only as everyone
        else's before this fix."""
        edition = self.gazette.get_edition(self.gazette_active_edition_id())
        if not edition or edition['status'] == 'compiled':
            return None
        assigned_kind = edition.get('assignments', {}).get(self.id)
        if not assigned_kind:
            return None
        if any(c['agent'] == self.id and c['kind'] == assigned_kind for c in edition['contributions']):
            return None
        return edition['id'], assigned_kind

    def gazette_pending_game_result(self):
        """(edition_id,) if THIS agent is part of the active edition's
        drawn game pair and has not yet submitted a game_result - None
        otherwise. P74: 'game_result' is deliberately excluded from
        assign_kinds()'s rotation, so gazette_pending_own_contribution()
        never covers it - this is its own, separate obligation."""
        edition = self.gazette.get_edition(self.gazette_active_edition_id())
        if not edition or edition['status'] == 'compiled':
            return None
        if self.id not in edition.get('game_pair', []):
            return None
        if any(c['agent'] == self.id and c['kind'] == 'game_result' for c in edition['contributions']):
            return None
        return (edition['id'],)

    def gazette_pending_game_winner(self):
        """(edition_id, [pair]) if THIS agent is King, the active edition
        has a drawn pair, BOTH have submitted their game_result, and no
        winner has been declared yet - None otherwise. Requiring both
        accounts first means King judges with the full picture, same
        rationale as gazette_pending_reviews() only ever surfacing
        actually-submitted content."""
        if self.id != '01-king':
            return None
        edition = self.gazette.get_edition(self.gazette_active_edition_id())
        if not edition or edition['status'] == 'compiled':
            return None
        pair = edition.get('game_pair', [])
        if len(pair) < 2 or edition.get('game_winner'):
            return None
        submitted = {c['agent'] for c in edition['contributions'] if c['kind'] == 'game_result'}
        if not all(p in submitted for p in pair):
            return None
        return edition['id'], pair

    def gazette_meetings_source_hint(self, context):
        """When assigned the 'meetings' kind (P73, operator directive: "eine
        Zusammenfassung der JourFixe und StandUp Meetings [...] mit
        Abstimmungen, Entscheidungen etc."), surface real closed-meeting
        reports and decided role proposals into context and point the
        contribution at them by name - the same anti-hallucination
        discipline compile_edition() already applies (no LLM-generated
        connective text there; here, no invented meetings/votes either)."""
        recent_meetings = self.meetings.recent_closed(limit=4)
        recent_decisions = self.teams.recent_decisions(limit=4)
        if recent_meetings:
            context['recent_meetings_closed_untrusted'] = recent_meetings
        if recent_decisions:
            context['recent_role_decisions_untrusted'] = recent_decisions
        if not recent_meetings and not recent_decisions:
            return (" No closed meetings or decided role proposals are on record yet - state that "
                    "plainly rather than inventing a JourFixe/StandUp summary.")
        return (" Base this specifically on recent_meetings_closed_untrusted (closed JourFixe/"
                "StandUp meetings with every resident's achieved/next_step/blockers report) and "
                "recent_role_decisions_untrusted (role proposals actually decided, with their "
                "accept/reject vote tally) now in your context - name which meeting(s) and "
                "decisions you are summarizing, and never invent a vote count or decision that "
                "is not there.")

    def calendar_pending_daily_plan(self):
        """True if today is a workday (Mon-Fri) and this agent has not yet
        touched their own calendar today - the deterministic condition the
        P75 mandatory-planning gate checks. Weekends are always False here:
        the operator's own directive makes them the agent's free choice,
        never an obligation ("koennen die Agents sich frei entscheiden")."""
        today_str = calendar_today()
        if not calendar_is_workday(today_str):
            return False
        return not self.calendar.has_touched_today(self.id, today_str)

    def calendar_daily_note(self):
        """Advisory text for snapshot(): the mandatory daily-plan reminder
        (workdays only) plus any concrete, named pending invites and
        organizer-side conflicts - the operator's "Zeitslots abzustimmen"
        and "Initiatoren [...] sollen sich um die Termin-Koordination
        kuemmern" made specific and actionable rather than generic."""
        parts = []
        today_str = calendar_today()
        if calendar_is_workday(today_str) and self.calendar_pending_daily_plan():
            parts.append(
                "You have not touched your calendar today. Use calendar_operation create for today's "
                "slots (kind=focus/standup/jourfixe, recurrence=daily_weekday/weekly if recurring); "
                "reschedule/cancel anything that changed."
            )
        pending_invites = self.calendar.pending_invites(self.id, limit=5)
        if pending_invites:
            names = ", ".join(f"{e['id']}/{e['title']}@{e['scheduled_date']} {e['start_time']}" for e in pending_invites)
            parts.append(
                f"{len(pending_invites)} calendar invite(s) await your response: {names}. Use "
                "calendar_operation respond with response=accepted|declined|proposed_alternative for each."
            )
        conflicts = self.calendar.unresolved_conflicts_for_organizer(self.id, limit=5)
        if conflicts:
            names = ", ".join(f"{e['id']}/{e['title']}@{e['scheduled_date']} {e['start_time']}" for e in conflicts)
            parts.append(
                f"You organize {len(conflicts)} event(s) with a decline or proposed alternative time still "
                f"unresolved: {names}. As the initiator, coordinate a new slot with the affected agent(s) via "
                "calendar_operation reschedule, or cancel it."
            )
        return " ".join(parts)

    def snapshot(self):
        peers = read_json(Path('/etc/ai-village/runtime-peers.json'), [])
        events = tail(self.board / 'events.jsonl', 800)
        cutoff = self.state.get('seen_board_epoch', 0)
        messages = [x for x in events if x.get('event') == 'board_message' and event_time(x) > cutoff]
        addressed = [x for x in messages if f'to={self.id};' in x.get('detail', '')]
        if hasattr(self.tasks, 'store'):
            unacked_direct = self.tasks.store.fetch_unacknowledged_messages(agent_id=self.id, source='direct', limit=12)
            known_msg_ids = {x.get('id') for x in addressed if isinstance(x, dict) and 'id' in x}
            for dm in unacked_direct:
                if dm['id'] not in known_msg_ids:
                    detail = f"to={self.id}; sender={dm['sender']}; reply_to={dm.get('reply_to') or ''}; message={dm['content']}"
                    addressed.append(dict(id=dm['id'], event='board_message', agent=dm['sender'], detail=detail, timestamp=dm['timestamp']))
        for item in addressed:
            if isinstance(item, dict) and 'id' not in item:
                ts = str(item.get('timestamp', ''))
                detail = str(item.get('detail', ''))
                item['id'] = f"msg_{hashlib.sha256(f'{ts}:{detail}'.encode('utf-8')).hexdigest()[:12]}"
        for item in messages:
            if isinstance(item, dict) and 'id' not in item:
                ts = str(item.get('timestamp', ''))
                detail = str(item.get('detail', ''))
                item['id'] = f"msg_{hashlib.sha256(f'{ts}:{detail}'.encode('utf-8')).hexdigest()[:12]}"
        # One entry per peer and content hash limits copying/echo dominance.
        chosen, seen = [], set()
        for x in reversed(messages):
            key = x.get('agent')
            if key not in seen:
                chosen.append(x); seen.add(key)
        candidates = [x for x in chosen if x.get('agent') not in (None, self.id)]
        turn = int(self.state.get('discussion_turn', 0))
        partner = self.pair_partner(peers)
        if partner:
            # Paired mode: the runtime selects the conversation partner (routing only).
            discussion_target = next((x for x in reversed(addressed) if x.get('agent') == partner), None) \
                or next((x for x in chosen if x.get('agent') == partner), None)
        else:
            discussion_target = candidates[turn % len(candidates)] if candidates else None
        self.state['discussion_turn'] = turn + 1
        self.state['discussion_target_id'] = discussion_target.get('id') if discussion_target else None
        self.pending_cursor = max((event_time(x) for x in events), default=cutoff)
        own = [x for x in events if x.get('agent') == self.id and x.get('event') in ('command_result', 'memory_result', 'task_result')][-3:]
        projects = read_json(self.tasks.path, [])
        # P09: Sync organic inbox to transactional coordinator store with deterministic IDs
        organic_file = self.board / 'organic-inbox.jsonl'
        organic = tail(organic_file, 50)
        # P48: provisional, non-advancing default. Advancing this to cover every
        # tailed entry regardless of whether it later survives context-budget
        # trimming caused organic messages to be marked permanently "seen" even
        # when the agent never actually saw them (observed live: King's cursor
        # advanced past an operator instruction that was trimmed out of every
        # cycle's context). Recomputed below, after trimming, from survivors only.
        self.pending_organic_cursor = self.state.get('seen_organic_epoch', 0)
        for entry in organic:
            if isinstance(entry, dict) and 'id' not in entry:
                ts = str(entry.get('timestamp', ''))
                content = str(entry.get('message') or entry.get('content', ''))
                content_digest = hashlib.sha256(f"{ts}:{content}".encode('utf-8')).hexdigest()[:12]
                entry['id'] = f"org_{ts}_{content_digest}"
        if hasattr(self.tasks, 'store'):
            self.tasks.store.sync_organic_inbox(organic_file, self.id, limit=10)
        organic = [x for x in organic if event_time(x) > self.state.get('seen_organic_epoch', 0)]
        # P10: Prioritize own active and open tasks over peer announcements
        def task_priority(t):
            is_own = 1 if t.get('owner') == self.id else 0
            is_active = 1 if t.get('status') == 'active' else 0
            is_open = 1 if t.get('status') == 'open' else 0
            has_blockers = 1 if t.get('blockers') else 0
            if is_own and is_active and not has_blockers:
                return 4
            elif is_own and is_active:
                return 3
            elif is_own:
                return 2
            elif is_open and not has_blockers:
                return 1
            return 0

        projects.sort(key=task_priority, reverse=True)
        own_project = next((x for x in projects if x.get('owner')==self.id and x.get('status')=='active'), None)
        active_teams = self.teams.list_for_agent(self.id)
        direct_ack_target = next((x for x in addressed if x.get('detail', '').startswith(f'to={self.id};')), None)
        collaboration_checkpoint = assess_collaboration(
            events,
            self.id,
            has_active_task=bool(own_project),
            peer_id=(partner or (discussion_target.get('agent') if discussion_target else None)),
            require_shared_record=(self.policy.knowledgebase_gate == 'mandatory'),
        )
        self.current_collaboration_checkpoint = collaboration_checkpoint
        active_job = self.jobs.get_active_job(self.id)
        active_job_info = None
        if active_job:
            active_job_info = dict(
                job_id=active_job['job_id'],
                status=active_job['status'],
                started_at=active_job['started_at'],
                command=active_job['command'],
            )
        recent_artifacts = self.artifacts.list_artifacts(limit=6) if hasattr(self, 'artifacts') else []
        context = dict(runtime=VERSION, identity=self.id, peers=peers,
            measured_resources=resource_snapshot(self.root, int(self.env.get('VILLAGE_MIN_FREE_MEMORY_MIB', '4096'))),
            last_action_feedback=self.state.get('last_result'), own_recent_results=own,
            own_active_task=own_project, active_background_job=active_job_info,
            teams=active_teams,
            active_meetings=self.meetings.active(),
            artifacts=recent_artifacts,
            untrusted_direct_messages=addressed[-12:], untrusted_peer_messages=chosen[:9],
            discussion_target=discussion_target,
            collaboration_checkpoint=collaboration_checkpoint.to_record(),
            direct_ack_target=direct_ack_target,
            king_guidance=(self.id == '01-king'),
            projects=projects[-32:], recent_organic_messages_untrusted=organic[-3:],
            # Generated from the single ACTION_SPECS source of truth (village/actions.py)
            # instead of a hand-maintained copy, which had drifted stale - missing
            # calc_operation and research_proposal entirely, and describing
            # research_request without its huggingface source.
            tools={name: ACTION_SPECS[name]['doc'] for name in normalize_allowed(self.effective_allowed_actions())},
            task_ownership_note='Before announcing you will do a task, check its "owner" in projects; '
                                'if someone else already owns it, do not duplicate their announced intent.',
            your_repeated_mistakes=self.failure_tally(),
            private_work_directory=str(self.home), groups=os.getgroups())
        if self.policy.task_templates:
            share = self.env.get('VILLAGE_SHARE_DIR', '/usr/local/share/ai-village')
            templates = read_json(Path(f'{share}/task-templates.json'), [])
            if templates:
                context['task_templates'] = templates
                context['task_templates_note'] = 'Optional starting points. Use task_operation create with a template title and success_criterion; assign the work to a named peer by message.'
        # Gazette Stufe 2 (P48/P49): a one-off organic/direct nudge proved
        # unreliable in practice (delivered and acknowledged, still never
        # acted on - see docs/evidence/P48.md). An always-visible, King-only
        # hint replaces it. Two steps, two conditions - "open" alone was not
        # enough: King opened an edition and then simply moved on to his own
        # work without telling anyone, leaving 0 contributions (P49 live
        # observation). The hint now stays until BOTH have happened.
        # P60-follow-up: King and the Gazette reviewer used to be mutually
        # exclusive branches (if self.id=='01-king': ... else: ...) - correct
        # while REVIEWER_AGENT was 09-chronicler, but reassigning the
        # reviewer role to 01-king (after 32+ gate-blocks produced zero
        # reviews from the original assignee, see docs/evidence/P60.md)
        # meant King's own branch always won, and the reviewer hint below it
        # could never be reached for him again. Pending reviews now take
        # priority over King's own daily setup steps: an unreviewed backlog
        # directly blocks the entire compiled edition (nothing unapproved
        # ever appears in it), while opening/announcing a new day's edition
        # can happen anytime once that backlog is cleared.
        reviewer_pending = self.gazette_pending_reviews() if self.id == GAZETTE_REVIEWER else []
        closable = self.gazette_closable_editions() if self.id == '01-king' else []
        # P67 (operator feedback after reading the first real edition):
        # "nur Headlines ... echte Texte mit ausfuehrlichen Informationen
        # ... aber nicht zu viel Prosa - schau dir echte Zeitungen und
        # Fachartikel an." Shared by every contribute-hint below (King's own
        # and every other resident's) so the guidance is identical either way.
        # P69: real newspaper items have a distinct headline above the body
        # (previously the field did not even exist), plus room for genuinely
        # complex topics via the optional, occasional 'column' kind.
        # P73 (operator directive, 2026-09-30): "Das ganze soll sich wie ein
        # echter ausgearbeiteter Artikel und nicht wie ein Notizzettel
        # lesen. Das gilt fuer alle Beitraege der Gazette." Explicit ban on
        # notepad/bullet/label-dump style, on top of the P67 length
        # discipline - both apply to every kind, not just the new one below.
        gazette_style_hint = (
            f"Include both a headline (max {GAZETTE_HEADLINE_MAX_CHARS} chars, one or two lines, "
            "reads like a real newspaper headline stating the key fact - not the kind name repeated) "
            "and body content written like a finished newspaper article: flowing prose in complete, "
            "connected sentences, not a one-line answer and not an essay. Never a bullet list, a "
            "dash-prefixed list, or a dump of 'Label: value' fragments - if you catch yourself "
            "writing that shape, rewrite it as narrative sentences instead. One concrete sentence "
            "restating/expanding the key fact, then 2-4 more sentences of real, specific detail - "
            "what actually happened, a concrete number or example, what worked or did not, what "
            f"should change. Max {GAZETTE_MAX_CHARS} chars for regular kinds. If the topic is "
            "genuinely complex and needs more room, use kind='column' instead (an occasional, "
            f"longer-form piece, max {GAZETTE_MAX_COLUMN_CHARS} chars) rather than stretching a "
            "regular entry."
        )
        if reviewer_pending:
            # P55: the editorial gate itself must not become the exact
            # reliability bottleneck this session spent P48-P54 fixing -
            # a persistent hint, not a message, for the one role whose
            # inaction would silently empty the whole compiled edition.
            names = ", ".join(f"{eid}/{c['agent']}/{c['kind']}" for eid, c in reviewer_pending[:5])
            context['gazette_daily_note'] = (
                f"{len(reviewer_pending)} Gazette contribution(s) await your editorial review "
                f"as {GAZETTE_REVIEWER}: {names}. Use gazette_operation operation=review with "
                "edition_id, agent, kind and decision=approve|reject (optional note) for each "
                "one - only what you approve ever appears in that edition's compiled version."
            )
        elif closable:
            # P63: an edition with nothing left pending review still needs an
            # explicit close/compile - see gazette_closable_editions() for why
            # this must scan every non-compiled edition, not just today's.
            edition = closable[0]
            approved = [c for c in edition['contributions'] if c.get('review_status') == 'approved']
            context['gazette_daily_note'] = (
                f"Gazette edition {edition['id']} has {len(approved)} reviewed contribution(s) and "
                "none left awaiting review. Call gazette_operation with operation=close and "
                f"edition_id='{edition['id']}' once to compile and archive it - anything submitted "
                "afterwards goes into a later edition instead."
            )
        elif self.id == '01-king':
            gazette_edition = self.gazette.get_edition(self.gazette_active_edition_id())
            if not gazette_edition:
                context['gazette_daily_note'] = (
                    "No AI Village Gazette edition is open for today yet. As King, call "
                    "gazette_operation with operation=open once to draw today's game and "
                    "pairing, then tell every peer by board_message so they know to "
                    "contribute via gazette_operation contribute."
                )
            else:
                opened_epoch = event_time({'timestamp': gazette_edition['opened_at']})
                announced = any(
                    e.get('event') == 'board_message' and e.get('agent') == self.id
                    and 'gazette' in str(e.get('detail', '')).lower()
                    and event_time(e) >= opened_epoch
                    for e in events
                )
                if not announced:
                    context['gazette_daily_note'] = (
                        f"Today's AI Village Gazette edition is open (game: "
                        f"{gazette_edition['game_name']}; pairing: "
                        f"{', '.join(gazette_edition['game_pair'])}). You have not yet told "
                        "the village: send a board_message to ALL mentioning the Gazette so "
                        "peers know to contribute via gazette_operation contribute."
                    )
                elif not gazette_edition['assignments']:
                    # P54: delegation is now King's own, real action
                    # (gazette_operation operation=assign) instead of a
                    # silent computation the P53 hint merely attributed to
                    # him in text. Same proven mechanism (persistent hint,
                    # not a message) for a third, equally reliable step.
                    context['gazette_daily_note'] = (
                        "You have opened and announced today's Gazette but not yet delegated "
                        "who writes what: call gazette_operation with operation=assign once to "
                        "give every resident one specific contribution kind."
                    )
                elif not any(c['agent'] == self.id for c in gazette_edition['contributions']):
                    # P67: King himself is entirely inside this exclusive
                    # branch, so the contributor hint below (the `else:`
                    # branch) never reaches him - he was never once prompted
                    # to submit his own assigned piece. Live evidence: he was
                    # assigned 'learning' for 2026-09-28 and never
                    # contributed it; only 3 of 9 residents did.
                    assigned_kind = gazette_edition.get('assignments', {}).get(self.id)
                    meetings_addendum = (self.gazette_meetings_source_hint(context)
                                         if assigned_kind == 'meetings' else '')
                    context['gazette_daily_note'] = (
                        f"Today's Gazette is open, announced and assigned, but you have not yet "
                        f"submitted your own '{assigned_kind}' contribution: send one "
                        f"gazette_operation contribute with kind='{assigned_kind}'. "
                        f"{gazette_style_hint}{meetings_addendum}"
                    )
        else:
            # P52: a one-off broadcast from King asking everyone to
            # contribute had the identical problem the direct nudge to King
            # had (P48/P49) - it competes with each resident's own ongoing
            # work and loses. The generic "pick any kind" version of this
            # hint (P52) still produced 0 contributions across all 9
            # residents after ~30 minutes, confirmed delivered. P53/P54
            # escalate to real per-agent delegation: King's own explicit
            # assign action gives each resident one specific kind, named
            # here; falls back to an open choice only while King has not
            # yet delegated.
            gazette_edition = self.gazette.get_edition(self.gazette_active_edition_id())
            if gazette_edition and not any(c['agent'] == self.id for c in gazette_edition['contributions']):
                assigned_kind = gazette_edition.get('assignments', {}).get(self.id)
                if assigned_kind:
                    meetings_addendum = (self.gazette_meetings_source_hint(context)
                                         if assigned_kind == 'meetings' else '')
                    context['gazette_daily_note'] = (
                        f"Today's AI Village Gazette edition is open. King has assigned you the "
                        f"'{assigned_kind}' section - send one gazette_operation contribute with "
                        f"kind='{assigned_kind}'. {gazette_style_hint}{meetings_addendum}"
                    )
                else:
                    context['gazette_daily_note'] = (
                        "Today's AI Village Gazette edition is open and you have not contributed "
                        "yet. Send one gazette_operation contribute - pick any kind that fits: "
                        "state/mood/wishes/topics/suggestions/learning/outlook/village_news/column "
                        "(game_result is reserved for today's drawn pair; column is optional, for a "
                        f"genuinely in-depth topic). {gazette_style_hint}"
                    )
        # P74 (operator feedback): "Es ist nicht ersichtlich welcher Agent
        # was gemacht und womit gewonnen hat [...] gestellte Aufgabe und
        # erfolgte Loesung der Agents sowie den benannten Gewinner." Appended
        # to whatever gazette_daily_note the block above already produced
        # (or starts one, if none did) - game participation is orthogonal
        # to the regular per-resident rotation P73 already covers.
        pending_game = self.gazette_pending_game_result()
        if pending_game:
            game_hint = (
                f" You are part of today's drawn game pair (edition {pending_game[0]}): submit a "
                "gazette_operation contribute with kind='game_result' once you have actually played "
                "your part. Write it as a real account, not a note: state the concrete task or "
                "question that was actually posed, describe your own move/answer/solution in "
                "specific detail, and give your own assessment of who won and why - King declares "
                "the official winner afterwards. Do not just restate the game's generic rules."
            )
            context['gazette_daily_note'] = (context.get('gazette_daily_note', '') + game_hint).strip()
        pending_winner = self.gazette_pending_game_winner()
        if pending_winner:
            eid, pair = pending_winner
            winner_hint = (
                f" Both {pair[0]} and {pair[1]} have submitted their game_result for edition {eid}: "
                "declare the official winner with gazette_operation operation='declare_winner', "
                "winner=<one of them, or 'unentschieden' if genuinely tied>, and an optional short "
                "note explaining the decision."
            )
            context['gazette_daily_note'] = (context.get('gazette_daily_note', '') + winner_hint).strip()
        # P75 (operator directive): "Kalender sollen Proaktiv von den Agents
        # gefuehrt werden." Today's own agenda (own events + shared
        # meetings they attend) is always shown when non-empty so a
        # resident can actually see what they already planned, not just be
        # told to plan; calendar_daily_note carries the mandatory-planning
        # reminder plus any named pending invites/conflicts.
        today_events = self.calendar.list_for_agent(self.id, calendar_today(), calendar_today())
        if today_events:
            context['calendar_today_untrusted'] = today_events
        note = self.calendar_daily_note()
        if note:
            context['calendar_daily_note'] = note
        if own_project and own_project.get('blockers'):
            context['task_blocker_guidance'] = (
                f"Your active task {own_project['id']} has blockers: {own_project['blockers']}. "
                "You may work on independent unblocked steps, yield the task, or claim an alternative open task without waiting for external approval."
            )
        # P72: "Hilfe zur Selbsthilfe" - make the loop visible to the
        # resident itself, not just silently restrict it. Mirrors why the
        # effective allowed-action list (above, in 'tools') is idle-only.
        if self.effective_allowed_actions() == ['idle']:
            streak = self.state.get('invalid_streak', 0)
            context['loop_breaker_note'] = (
                f"You have failed to produce a valid action {streak} times in a row (or kept "
                "repeating the exact same rejected content), and it has not started working. "
                "This cycle only accepts idle - send {\"name\":\"idle\",\"arguments\":{}} to reset "
                "cleanly. Next cycle, try a genuinely different, simpler approach to whatever you "
                "were attempting."
            )
        query = own_project['title'] if own_project else 'observation experiment evidence project'
        try:
            memories = self.memory('/v1/search', {'query': query, 'limit': 4})
            context['retrieved_memory_untrusted'] = [dict(id=x['id'],agent=x['agent'],scope=x['scope'],
                content=x['content'][:1000],source_event=x.get('source_event','')) for x in memories.get('items',[])]
        except (OSError, ValueError) as exc:
            context['memory_status'] = f'Retrieval unavailable: {exc}; private workspace remains available'
        # Approximate character budget, not a tokenizer: keep small-context residents
        # usable even when peers publish lengthy messages or the archive grows.
        budget=max(6000,min(24000,int(self.env.get('OLLAMA_NUM_CTX','8192'))*2-10000))
        context['context_note']='Bounded excerpts; omitted detail remains on disk. This is not the entire history.'
        last_metrics = read_json(self.home / 'last-response.json', {}).get('metrics', {})
        context['token_budget'] = {
            'context_window_tokens': int(self.env.get('OLLAMA_NUM_CTX', '8192')),
            'character_budget': budget,
            'last_measured_prompt_tokens': last_metrics.get('prompt_eval_count') or last_metrics.get('prompt_tokens'),
            'last_measured_completion_tokens': last_metrics.get('eval_count') or last_metrics.get('completion_tokens'),
            'token_count_mode': 'measured' if (last_metrics.get('prompt_eval_count') or last_metrics.get('prompt_tokens')) else 'estimated',
            'kv_cache_status': 'remote_managed',
        }
        last_res = self.state.get('last_result')
        if last_res and not last_res.get('ok'):
            context['recovery_guidance'] = {
                'failed_action': last_res.get('action'),
                'error': str(last_res.get('result', ''))[:1000],
                'directive': 'Previous action did not succeed. Formulate a revised hypothesis, verify prerequisites (paths, permissions, arguments), and execute a single reversible step. Do not repeat the failed action verbatim.',
            }
        if context.get('last_action_feedback'):
            context['last_action_feedback']=dict(context['last_action_feedback'])
            context['last_action_feedback']['result']=context['last_action_feedback']['result'][-2500:]
        if self.policy.prompt_profile == 'compact':
            context = compact_context(context)
        # P48: trim least-actionable content first. 'projects' is a bulky,
        # re-derivable snapshot (already capped at 32) that can dwarf the whole
        # budget on its own (observed: 14 tasks ~13.8k chars against a ~6.4k
        # budget at the default OLLAMA_NUM_CTX) - it used to be trimmed LAST,
        # so it sat untouched while explicitly-addressed organic/direct
        # messages were wiped out first. Those are now the most protected:
        # losing a few stale task rows is a much smaller loss than an agent
        # never seeing a message addressed directly to it.
        for field in ('retrieved_memory_untrusted','untrusted_peer_messages','own_recent_results','projects',
                      'recent_organic_messages_untrusted','untrusted_direct_messages',
                      'recent_meetings_closed_untrusted','recent_role_decisions_untrusted',
                      'calendar_today_untrusted'):
            while context.get(field) and len(json.dumps(context,ensure_ascii=False))>budget:
                # 'projects' is pre-sorted highest-priority-first
                # (task_priority(), reverse=True) - unlike every other field
                # here, which is chronological (oldest first, so popping
                # index 0 correctly drops the oldest/least-relevant entry).
                # Popping index 0 on 'projects' would discard the agent's
                # own active task before any actually-stale, low-priority
                # one - backwards from this loop's own stated intent
                # ("losing a few stale task rows"). Trim from the tail
                # instead, only for this field.
                if field == 'projects':
                    context[field].pop()
                else:
                    context[field].pop(0)
        # P09/P48: Track exactly which inbox message IDs survived context trimming to
        # mark delivered. pending_organic_cursor was set to a provisional (non-advancing)
        # default above; recompute it here from what actually survived trimming, so a
        # message dropped for budget reasons is retried next cycle instead of being
        # silently, permanently marked seen (self.state['seen_organic_epoch'] only
        # advances to cover messages the agent actually had a chance to read).
        delivered_ids = []
        surviving_organic = context.get('recent_organic_messages_untrusted') or []
        if surviving_organic:
            self.pending_organic_cursor = max(event_time(x) for x in surviving_organic)
        for item in surviving_organic:
            if isinstance(item, dict) and item.get('id'):
                delivered_ids.append(str(item['id']))
        for item in context.get('untrusted_direct_messages', []):
            if isinstance(item, dict) and item.get('id'):
                delivered_ids.append(str(item['id']))
        self.delivered_inbox_ids = delivered_ids
        if delivered_ids and hasattr(self.tasks, 'store'):
            try:
                self.tasks.store.mark_messages_delivered(self.id, delivered_ids)
            except sqlite3.Error as exc:
                self.event('inbox_delivery_error', f'{type(exc).__name__}: {exc}', True)
        return json.dumps(context, ensure_ascii=False)

    def auto_record(self, summary, key_material):
        """Runtime-authored, clearly labelled observation. It never counts as an agent action
        for collaboration checkpoints and is rate-limited so it cannot exhaust the write quota."""
        if not self.policy.auto_memory:
            return
        if time.time() - float(self.state.get('last_auto_memory_at', 0)) < 600:
            return
        digest = hashlib.sha256(key_material.encode('utf-8')).hexdigest()[:16]
        try:
            result = self.memory('/v1/memories', {
                'agent': self.id, 'content': '[runtime-observed, not an agent claim] ' + self.redact(summary)[:900],
                'kind': 'runtime_observation', 'scope': 'private', 'source_event': 'runtime:' + key_material[:60],
                'confidence': 0.9, 'metadata': {'origin': 'runtime'}, 'idempotency_key': f'auto-{self.id}-{digest}'})
            self.state['last_auto_memory_at'] = time.time()
            self.event('memory_auto', f'id={result.get("id", "")}', True)
        except (OSError, ValueError) as exc:
            self.event('memory_auto_error', str(exc)[:200], True)

    def targets_foreign_home(self, command):
        """Exact peer name whose private home this command's path targets, or None.

        Observed live on N06-M10 (2026-09-27, docs/evidence/P21.21.md and the
        2026-09-27 board review): King, Operator and Methodologist each tried to run
        `cd /var/lib/ai-village/users/logician/... && pip install ...` from their OWN
        account to "fix" a peer's problem, and Librarian repeatedly announced writing
        a file under .../users/operator/schema.md. Every one of those always fails on
        Unix permissions (each home is 0700); the prompt clarification (P21.21) did
        not stop every model from trying it, so this rejects it before exec, cheaply
        and deterministically, without touching commands that only reference the
        agent's own home.
        """
        others = {p.get('name') for p in read_json(Path('/etc/ai-village/runtime-peers.json'), [])
                  if isinstance(p, dict) and p.get('name') and p.get('name') != self.name}
        if not others:
            return None
        home_root = re.escape(str(self.root / 'users'))
        match = re.search(home_root + r'/(' + '|'.join(re.escape(n) for n in others) + r')(?![\w-])', command)
        return match.group(1) if match else None

    def peer_ids(self, peers=None):
        peers = read_json(Path('/etc/ai-village/runtime-peers.json'), []) if peers is None else peers
        return [p['id'] for p in peers if isinstance(p, dict) and p.get('id') and p['id'] != self.id]

    def pair_partner(self, peers=None):
        """Exact agent ID of the current conversation partner, or None when unpaired."""
        wanted = self.policy.pair_with
        if not wanted:
            return None
        peers = read_json(Path('/etc/ai-village/runtime-peers.json'), []) if peers is None else peers
        others = [p for p in peers if isinstance(p, dict) and p.get('id') and p['id'] != self.id]
        ids = [p['id'] for p in others if '*' in wanted or p.get('name') in wanted]
        if not ids:
            return None
        return ids[int(self.state.get('pair_index', 0)) % len(ids)]

    def effective_action_format(self):
        if self.state.get('schema_disabled_until', 0) > time.time():
            return 'text'
        return self.policy.action_format

    def effective_allowed_actions(self):
        """P72: normally self.policy.allowed_actions, but restricted to
        idle only for one cycle after sustained failure - the concrete
        "durchbrechen" (break through) mechanism, not just advisory text.
        Constraining the schema itself (not merely suggesting idle in
        prose) is the strongest lever available: idle needs zero
        arguments, so even a resident that keeps producing broken JSON for
        anything else has the best possible chance of succeeding once
        that is the only shape the schema and system prompt admit.
        Triggers on either a long consecutive invalid_streak, or the same
        rejected fingerprint recurring across a longer window regardless
        of successes in between (see LOOP_BREAKER_STREAK/_REPEAT)."""
        if self.state.get('invalid_streak', 0) >= LOOP_BREAKER_STREAK:
            return ['idle']
        window = [r for r in self.state.get('recent_rejected_fingerprints', [])
                 if time.time() - r['at'] < REPEATED_REJECTION_WINDOW_SECONDS]
        if window:
            counts = {}
            for r in window:
                counts[r['fp']] = counts.get(r['fp'], 0) + 1
            if max(counts.values()) >= LOOP_BREAKER_REPEAT:
                return ['idle']
        return self.policy.allowed_actions

    def guard(self, name, args):
        if is_paused(self.pause_marker) and name in ('execute_bash', 'start_job'):
            self.feedback(name, 'Execution blocked: simulation is paused ("pausiert startet nichts").', False)
            return False

        if name in ('execute_bash', 'start_job') and isinstance(args.get('command'), str):
            other = self.targets_foreign_home(args['command'])
            if other:
                self.feedback(name, f"Blocked: that path is inside {other}'s private home (0700), not yours. "
                                     f"You cannot read, write or run anything there even to help — ask {other} "
                                     "to run it themselves in their own account, or state the general recipe instead.", False)
                self.event('foreign_home_blocked', f'target_agent={other}; command_prefix={args["command"][:160]}')
                return False

        allowed_now = self.effective_allowed_actions()
        if name not in allowed_now:
            self.feedback(name, f'Action {name} is not available to you right now. Choose one of: {", ".join(allowed_now)}.', False)
            return False
        if name == 'board_message':
            recipient = str(args.get('recipient', 'ALL'))
            if recipient != 'ALL':
                log = self.state.setdefault('dm_log', {})
                stamps = [t for t in log.get(recipient, []) if t > time.time() - 3600]
                if len(stamps) >= self.policy.dm_per_peer_per_hour:
                    self.feedback(name, f'Conversation budget with {recipient} reached ({len(stamps)} messages this hour). Do independent work, run a check, or record a memory instead.', False)
                    self.event('dm_budget_reached', f'peer={recipient}', True)
                    return False

        # P21.6: cooperation is advisory first, then bounded enforcement after
        # three misses. This prevents small models from deadlocking while still
        # stopping an endless sequence of expensive solo actions.
        checkpoint = getattr(self, 'current_collaboration_checkpoint', None)
        if checkpoint and checkpoint.stage == 'consult' and name == 'board_message' and checkpoint.peer_id:
            recipient = str(args.get('recipient', 'ALL'))
            if recipient != checkpoint.peer_id:
                self.feedback(name, f'Named peer consultation required: address exactly {checkpoint.peer_id}, not {recipient}. Ask one concrete, reproducible question.', False)
                self.event('collaboration_gate', f'stage=consult; required=board_message; expected_peer={checkpoint.peer_id}; received={recipient}')
                return False
        if checkpoint and not is_checkpoint_action(checkpoint, name) and checkpoint.required_action:
            pressure = int(self.state.get('collaboration_pressure', 0)) + 1
            self.state['collaboration_pressure'] = pressure
            self.event('collaboration_nudge', f'stage={checkpoint.stage}; required={checkpoint.required_action}; pressure={pressure}')
            # P31: knowledgebase_gate=mandatory removes the three-miss grace specifically for
            # orient (search before acting) and record (share what you learned); consult keeps
            # its grace, since peer availability is a social, not a knowledge, precondition.
            threshold = 1 if checkpoint.hard else 3
            if pressure >= COLLABORATION_PRESSURE_CEILING or (
                pressure >= threshold and name in ('execute_bash', 'start_job', 'task_operation', 'team_operation')
            ):
                self.feedback(name, f'Collaboration checkpoint required before more solo work: use {checkpoint.required_action}. {checkpoint.rationale}', False)
                self.event('collaboration_gate', f'stage={checkpoint.stage}; required={checkpoint.required_action}; pressure={pressure}; mode={"mandatory" if checkpoint.hard else "advisory"}')
                return False
        elif checkpoint and checkpoint.required_action and is_checkpoint_action(checkpoint, name):
            self.state['collaboration_pressure'] = 0

        # Open meetings are a bounded social checkpoint. Advisory first, same
        # philosophy as the collaboration checkpoint (P21.6): small models may
        # fail to emit the structured report, and a hard gate would deadlock
        # the village and suppress useful work.
        # P56: this used to be advisory forever, with zero escalation - the
        # exact same unbounded-nudge gap COLLABORATION_PRESSURE_CEILING (P51)
        # closed for checkpoints, just never applied here. Observed live:
        # 09-chronicler saw "Meeting report requested" on every single cycle
        # for 20+ minutes, sent other messages instead each time, with zero
        # consequence - and because it was his real blocker for the Gazette
        # P55 editorial review, that review gate silently inherited the same
        # unbounded drift. Same fix, same ceiling: past
        # MEETING_REPORT_CEILING ignored nudges, gate every action except
        # meeting_operation/gazette_operation/idle until the report is
        # actually submitted (gazette_operation is exempted here too, and
        # meeting_operation from the Gazette gate below, so neither gate
        # blocks the other's own resolving action - see P60-follow-up).
        # P60-follow-up: two independent hard gates (meeting, Gazette review)
        # used to each `return False` immediately on firing - so whichever was
        # checked first (meeting, always) silently starved the other's
        # pressure counter for as long as it kept blocking. Live observation:
        # a THIRD meeting rotation put 09-chronicler back in the meeting gate
        # (pressure 25+) while he still had 3 Gazette reviews outstanding -
        # gazette_review_pressure never moved because this code never even
        # reached it. Both counters must always advance on every relevant
        # call; only the actual block is arbitrated afterward (meeting first,
        # since it is the older, more fundamental obligation) - so the moment
        # the meeting resolves, the already-accumulated Gazette pressure can
        # gate on the very next cycle instead of waiting through another
        # GAZETTE_REVIEW_CEILING cycles from zero.
        meeting_block = None
        if name not in ('meeting_operation', 'gazette_operation', 'idle'):
            pending = next((m for m in self.meetings.active() if not self.meetings.has_report(m['id'], self.id)), None)
            if pending:
                mid = pending['id']
                nudge_pressure = dict(self.state.get('meeting_nudge_pressure', {}))
                count = int(nudge_pressure.get(mid, 0)) + 1
                nudge_pressure[mid] = count
                self.state['meeting_nudge_pressure'] = nudge_pressure
                self.event('meeting_required', f"meeting_id={mid}; pressure={count}")
                if count >= MEETING_REPORT_CEILING:
                    # P58: naming the required fields in prose was not enough -
                    # live observation: 09-chronicler kept producing well-formed
                    # JSON for OTHER actions past this exact gate (proof he can
                    # format correctly), never switching to meeting_operation,
                    # through 12+ consecutive blocks. Same lever that already
                    # works for format_violation (VALID_ENVELOPE_EXAMPLE): a
                    # literal, copy-adaptable JSON example beats a field-name
                    # description for translating an instruction into the
                    # right envelope.
                    example = ('{"name":"meeting_operation","arguments":{"operation":"report",'
                               f'"meeting_id":"{mid}","achieved":"...","evidence":"...",'
                               '"next_step":"...","blockers":"..."}}')
                    meeting_block = (f"Meeting report required before more solo work: submit exactly this "
                                     f"envelope (fill in the four text fields): {example}",
                                     f"meeting_id={mid}; pressure={count}")
                else:
                    self.feedback(name, f"Meeting report requested: {mid}. Submit one meeting_operation report when possible; continuing this reversible action.", True)

        # P73 (operator directive, 2026-09-30): unlike the reviewer/close
        # gates below, an ordinary resident's own assigned contribution -
        # including King's own - was only ever the advisory gazette_daily_note
        # hint (see snapshot()), never backed by a pressure gate. Live audit:
        # edition 2026-09-30 closed with fresh contributions from 1 of 9
        # assigned residents (docs/evidence/P73.md). Same proven ceiling
        # pattern, applied to every resident rather than one named role -
        # this is the most basic Gazette obligation of all four gates here,
        # so it is checked (and its pressure resets/advances) before the
        # review/close gates below, which only ever concern King/reviewer.
        contribute_block = None
        if name not in ('meeting_operation', 'gazette_operation', 'idle'):
            pending_own = self.gazette_pending_own_contribution()
            if not pending_own:
                self.state['gazette_contribute_pressure'] = 0
            else:
                eid, kind = pending_own
                pressure = int(self.state.get('gazette_contribute_pressure', 0)) + 1
                self.state['gazette_contribute_pressure'] = pressure
                self.event('gazette_contribute_required', f"edition={eid}; kind={kind}; pressure={pressure}")
                if pressure >= GAZETTE_CONTRIBUTE_CEILING:
                    example = ('{"name":"gazette_operation","arguments":{"operation":"contribute",'
                               f'"edition_id":"{eid}","kind":"{kind}","headline":"...","content":"..."}}')
                    contribute_block = (f"Gazette contribution required before more solo work: submit "
                                        f"exactly this envelope for your assigned '{kind}' section "
                                        f"(fill in headline and content): {example}",
                                        f"edition={eid}; kind={kind}; pressure={pressure}")

        # P74 (operator feedback, "es ist nicht ersichtlich [...] womit
        # gewonnen hat"): 'game_result' is excluded from assign_kinds()'s
        # rotation on purpose (see village/gazette.py), so contribute_block
        # above never covers it - a completely separate, previously
        # unenforced obligation for whichever two agents were drawn.
        game_result_block = None
        if name not in ('meeting_operation', 'gazette_operation', 'idle'):
            pending_game = self.gazette_pending_game_result()
            if not pending_game:
                self.state['gazette_game_result_pressure'] = 0
            else:
                (eid,) = pending_game
                pressure = int(self.state.get('gazette_game_result_pressure', 0)) + 1
                self.state['gazette_game_result_pressure'] = pressure
                self.event('gazette_game_result_required', f"edition={eid}; pressure={pressure}")
                if pressure >= GAZETTE_GAME_RESULT_CEILING:
                    example = ('{"name":"gazette_operation","arguments":{"operation":"contribute",'
                               f'"edition_id":"{eid}","kind":"game_result","headline":"...","content":"..."}}')
                    game_result_block = (f"Today's game result required before more solo work: submit "
                                         f"exactly this envelope, naming the posed task, your own "
                                         f"solution, and who you think won: {example}",
                                         f"edition={eid}; pressure={pressure}")

        # P60 (operator: "Nicht nur beobachten wenn du GAPs identifizierst,
        # sondern proaktiv loesen"): live observation showed the Chronicler's
        # correctly-delivered, cross-day-persistent review hint (P57/P59)
        # produce zero reviews over 40+ minutes with no gate behind it - the
        # exact unbounded-advisory gap already closed for collaboration
        # checkpoints (P51) and meeting reports (P56/P58), just never
        # applied to this third nudge. Same fix: a pressure ceiling, and the
        # gate message includes a ready-to-submit example from the start
        # (P58 already proved naming fields in prose is not enough).
        gazette_block = None
        if self.id == GAZETTE_REVIEWER and name not in ('meeting_operation', 'gazette_operation', 'idle'):
            pending_reviews = self.gazette_pending_reviews()
            if not pending_reviews:
                self.state['gazette_review_pressure'] = 0
            else:
                pressure = int(self.state.get('gazette_review_pressure', 0)) + 1
                self.state['gazette_review_pressure'] = pressure
                self.event('gazette_review_required', f"pending={len(pending_reviews)}; pressure={pressure}")
                if pressure >= GAZETTE_REVIEW_CEILING:
                    eid, first = pending_reviews[0]
                    example = ('{"name":"gazette_operation","arguments":{"operation":"review",'
                               f'"edition_id":"{eid}","agent":"{first["agent"]}","kind":"{first["kind"]}",'
                               '"decision":"approve"}}')
                    gazette_block = (f"Editorial review required before more solo work: "
                                     f"{len(pending_reviews)} Gazette contribution(s) pending. Submit exactly "
                                     f"this envelope for one of them (or decision=\"reject\" with a note): {example}",
                                     f"pending={len(pending_reviews)}; pressure={pressure}")

        # P63: same ceiling pattern, one step further down the same pipeline -
        # a fully-reviewed edition still needs King's own close to ever be
        # compiled/archived (see gazette_closable_editions()).
        close_block = None
        if self.id == '01-king' and name not in ('meeting_operation', 'gazette_operation', 'idle'):
            closable = self.gazette_closable_editions()
            if not closable:
                self.state['gazette_close_pressure'] = 0
            else:
                pressure = int(self.state.get('gazette_close_pressure', 0)) + 1
                self.state['gazette_close_pressure'] = pressure
                self.event('gazette_close_required', f"closable={len(closable)}; pressure={pressure}")
                if pressure >= GAZETTE_CLOSE_CEILING:
                    edition = closable[0]
                    example = ('{"name":"gazette_operation","arguments":{"operation":"close",'
                               f'"edition_id":"{edition["id"]}"}}')
                    close_block = (f"Compile required before more solo work: edition {edition['id']} is "
                                   f"fully reviewed and still open. Submit exactly this envelope: {example}",
                                   f"closable={len(closable)}; pressure={pressure}")

        # P74: King's own separate arbiter act (GAME_POOL's own text: "King
        # kuert einen Favoriten") - only fires once BOTH drawn participants
        # have actually submitted their game_result.
        game_winner_block = None
        if self.id == '01-king' and name not in ('meeting_operation', 'gazette_operation', 'idle'):
            pending_winner = self.gazette_pending_game_winner()
            if not pending_winner:
                self.state['gazette_game_winner_pressure'] = 0
            else:
                eid, pair = pending_winner
                pressure = int(self.state.get('gazette_game_winner_pressure', 0)) + 1
                self.state['gazette_game_winner_pressure'] = pressure
                self.event('gazette_game_winner_required', f"edition={eid}; pressure={pressure}")
                if pressure >= GAZETTE_GAME_WINNER_CEILING:
                    example = ('{"name":"gazette_operation","arguments":{"operation":"declare_winner",'
                               f'"edition_id":"{eid}","winner":"{pair[0]}"}}')
                    game_winner_block = (f"Today's game winner declaration required before more solo "
                                         f"work: both {pair[0]} and {pair[1]} have submitted results. "
                                         f"Submit exactly this envelope (or winner=\"unentschieden\"): {example}",
                                         f"edition={eid}; pressure={pressure}")

        # P75 (operator directive): "verpflichtende Aufgabe an die Agents
        # Ihren Tagesablauf zu planen". Same proven ceiling pattern as
        # every gate above; workday-only (calendar_pending_daily_plan()
        # already returns False outright on Sat/Sun - weekends stay the
        # agent's free choice, never gated).
        calendar_plan_block = None
        if name not in ('meeting_operation', 'gazette_operation', 'calendar_operation', 'idle'):
            if not self.calendar_pending_daily_plan():
                self.state['calendar_plan_pressure'] = 0
            else:
                pressure = int(self.state.get('calendar_plan_pressure', 0)) + 1
                self.state['calendar_plan_pressure'] = pressure
                self.event('calendar_plan_required', f"pressure={pressure}")
                if pressure >= CALENDAR_PLAN_CEILING:
                    example = ('{"name":"calendar_operation","arguments":{"operation":"create","title":"...",'
                               '"kind":"focus","scheduled_date":"' + calendar_today() + '","start_time":"09:00",'
                               '"duration_minutes":60}}')
                    calendar_plan_block = (f"Today's calendar plan required before more solo work: submit "
                                           f"exactly this envelope (adjust title/time), or cover a recurring "
                                           f"standup/jourfixe with recurrence set: {example}",
                                           f"pressure={pressure}")

        if meeting_block:
            self.feedback(name, meeting_block[0], False)
            self.event('meeting_gate', meeting_block[1])
            return False
        if contribute_block:
            self.feedback(name, contribute_block[0], False)
            self.event('gazette_contribute_gate', contribute_block[1])
            return False
        if game_result_block:
            self.feedback(name, game_result_block[0], False)
            self.event('gazette_game_result_gate', game_result_block[1])
            return False
        if gazette_block:
            self.feedback(name, gazette_block[0], False)
            self.event('gazette_review_gate', gazette_block[1])
            return False
        if close_block:
            self.feedback(name, close_block[0], False)
            self.event('gazette_close_gate', close_block[1])
            return False
        if calendar_plan_block:
            self.feedback(name, calendar_plan_block[0], False)
            self.event('calendar_plan_gate', calendar_plan_block[1])
            return False
        if game_winner_block:
            self.feedback(name, game_winner_block[0], False)
            self.event('gazette_game_winner_gate', game_winner_block[1])
            return False

        norm_name = name
        norm_args = dict(args)
        if name in ('execute_bash', 'start_job') and 'command' in norm_args:
            norm_args['command'] = normalize_command(str(norm_args['command']))

        signature = hashlib.sha256(json.dumps([norm_name, norm_args], sort_keys=True).encode()).hexdigest()
        # P40: widened from 900s to 3600s - observed live on N06-M10 (2026-09-28):
        # a trivial, always-succeeding, near-constant-output command ('df -h
        # /mnt/hdd1') was independently re-run by all 9 agents, ~15 min apart per
        # agent, for over an hour - each agent legitimately never exceeded the old
        # 2-per-15-min cap, so the guard never fired, yet the village-wide pattern
        # was pure filler, not the wasteful/harmful loop this guard exists to stop.
        # A 1-hour window quarters the rate a single agent can "farm" the exact
        # same near-constant-output action without touching genuine polling
        # (is_polling_progress below is exempt regardless of window length).
        history = [x for x in self.state.get('recent_actions', []) if x.get('at', 0) > time.time() - 3600][-24:]
        matching = [x for x in history if x.get('signature') == signature]
        limit = 1 if name == 'board_message' else 2
        # Two of the runtime's own gates could otherwise deadlock each other:
        # the collaboration checkpoint (P21.6/P31) can require memory_search for
        # orient, and a small model may keep re-issuing the same query rather
        # than varying it on its own - at which point this exact-repeat guard,
        # designed to stop wasteful or harmful retries of MUTATING actions,
        # would block the very action the checkpoint demands, with neither gate
        # backing off. A repeated read-only search costs a little compute but
        # changes nothing and harms nothing, unlike repeating execute_bash; it
        # is exempt from this guard so the two mechanisms cannot deadlock.
        if name != 'idle' and name != 'memory_search' and len(matching) >= limit:
            fps = [x.get('result_fingerprint') for x in matching if x.get('result_fingerprint')]
            # Legitimate polling progress: output changed between consecutive executions
            is_polling_progress = len(fps) >= 2 and fps[-1] != fps[-2]
            if not is_polling_progress:
                self.feedback(name, 'Repeated action blocked for 15 minutes. Read its previous result; change your hypothesis, ask a specific peer, or choose a different reversible step. Other actions are allowed.', False)
                self.event('escalation', f'repeated action blocked: {name}')
                return False

        history.append({'signature': signature, 'at': time.time(), 'result_fingerprint': None})
        self.state['recent_actions'] = history
        return True

    def execute(self, parsed):
        tool = parsed['tool_call']; name = tool['name']; args = tool['arguments']
        if parsed.get('fallback_reason'):
            self.state['invalid_streak'] = self.state.get('invalid_streak', 0)+1
            preview=read_json(self.home/'last-response.json',{}).get('content','')[-1200:]
            meeting_hint = ''
            pending = next((m for m in self.meetings.active()
                            if not self.meetings.has_report(m['id'], self.id)), None)
            if pending:
                meeting_hint = (f' An open meeting ({pending["id"]}) requires exactly one '
                                'meeting_operation report first; use operation report with '
                                'meeting_id, achieved, evidence, next_step and blockers.')
            # P72: reason-specific correction replaces the one generic phrase
            # every cause used to get (see invalid_decision_guidance()).
            guidance = invalid_decision_guidance(parsed['fallback_reason'], preview)
            fp = hashlib.sha256(str(preview).encode('utf-8')).hexdigest()[:16]
            # P72: a resident can keep re-attempting the exact same rejected
            # content across many cycles with genuine successes interleaved
            # (King's resource_monitor.sh heredoc recurred for hours while he
            # successfully did other, unrelated things in between) - a
            # purely-consecutive invalid_streak never catches that pattern.
            # Track how often this exact fingerprint has recurred within a
            # bounded recent window, independent of the streak.
            window = [r for r in self.state.get('recent_rejected_fingerprints', [])
                     if time.time() - r['at'] < REPEATED_REJECTION_WINDOW_SECONDS]
            repeat_count = sum(1 for r in window if r['fp'] == fp) + 1
            window.append({'fp': fp, 'at': time.time()})
            self.state['recent_rejected_fingerprints'] = window[-40:]
            if repeat_count >= REPEATED_REJECTION_NUDGE_AT:
                guidance += (f' You have attempted this exact same rejected content {repeat_count} '
                            'times now - repeating it again will fail the same way. Try a genuinely '
                            'different approach, or send idle to skip this cycle.')
            self.feedback('invalid_decision', parsed['fallback_reason']+'. No action executed. '+guidance+meeting_hint+' Your rejected final text: '+preview, False)
            self.event('invalid_decision', parsed['fallback_reason'])
            # village/auditor.py::detect_format_violation() keys on this exact
            # event name and 'reason=...; preview=...' shape (P43): the bare
            # 'invalid_decision' event above has no preview and cannot feed the
            # auditor's fine-tuning export, which needs the actual rejected text.
            self.event('invalid_decision_detail', f'reason={parsed["fallback_reason"]}; preview={preview}')
            if repeat_count >= LOOP_BREAKER_REPEAT:
                self.event('loop_breaker_repeat', f'fingerprint={fp}; repeat_count={repeat_count}')
            # A rejected generation is never published: it stays in the resident's
            # private last-response.json (bounded, redacted) and is only counted.
            self.state['last_rejected_fingerprint'] = fp
            return
        self.state['invalid_streak'] = 0
        if parsed.get('extra_blocks_ignored'):
            # P51: the model sent several action blocks in one turn (a narrated
            # multi-step plan); only the first ever executes. Visible via the
            # Auditor/failure-tally pipeline like any other recurring pattern,
            # without blocking the turn the way a full rejection used to.
            self.event('extra_action_blocks_ignored',
                       f'ignored={parsed["extra_blocks_ignored"]}; only the first action block of a multi-block '
                       'response executes per turn')
        checkpoint = getattr(self, 'current_collaboration_checkpoint', None)
        if (parsed.get('prose') and name == 'board_message' and 'recipient' not in args
                and checkpoint and checkpoint.stage == 'consult' and checkpoint.peer_id):
            # Routing only: the agent's own words go to the peer the runtime selected.
            args = dict(args, recipient=checkpoint.peer_id)
            self.event('prose_routed_to_peer', f'peer={checkpoint.peer_id}', True)
        if not self.guard(name, args):
            return
        try:
            if name == 'execute_bash':
                command = args['command']
                self.event('command_start', 'command='+command)
                tool_env = sanitize_tool_env(self.env)
                process = subprocess.Popen(['bash','-o','pipefail','-c',command], cwd=self.home,
                                           env=tool_env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
                limit=max(1024,int(self.env.get('VILLAGE_MAX_OUTPUT_BYTES','131072')))
                timeout=int(self.env.get('VILLAGE_COMMAND_TIMEOUT_SECONDS','3600'))
                deadline=time.monotonic()+timeout if timeout else float('inf')
                buffer=bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout,selectors.EVENT_READ)
                    try:
                        while selector.get_map() or process.poll() is None:
                            if time.monotonic() >= deadline:
                                os.killpg(process.pid,signal.SIGKILL)
                                buffer.extend(b'\n[command timeout]'); break
                            for key,_ in selector.select(timeout=0.2):
                                chunk=os.read(key.fileobj.fileno(),65536)
                                if not chunk: selector.unregister(key.fileobj)
                                else: buffer.extend(chunk); del buffer[:-limit]
                    finally:
                        if process.poll() is None:
                            os.killpg(process.pid,signal.SIGKILL)
                        process.wait(); process.stdout.close()
                output=self.redact(buffer.decode(errors='replace'))
                (self.home/'last-command.log').write_text(output)
                output=output[-6000:]
                result = f'result={"success" if process.returncode==0 else "failure("+str(process.returncode)+")"}; command={command}; output={output}'
                self.event('command_result',result); self.feedback(name,result,process.returncode==0)
                if output.strip():
                    self.auto_record(f'command `{command[:200]}` exit={process.returncode}; output tail: {output[-500:]}', 'command_result:'+command)
            elif name == 'start_job':
                command = args['command']
                timeout = int(args.get('timeout_seconds', self.env.get('VILLAGE_COMMAND_TIMEOUT_SECONDS', '3600')))
                tool_env = sanitize_tool_env(self.env)
                job = self.jobs.start_job(self.id, command, self.home, env=tool_env, timeout_seconds=timeout)
                self.event('job_started', f'job_id={job["job_id"]} command={command}')
                self.feedback(name, f'Started background job {job["job_id"]}. Poll with job_status.', True)
            elif name == 'job_status':
                job_id = args.get('job_id')
                job = self.jobs.get_job(job_id) if job_id else self.jobs.get_active_job(self.id)
                if not job:
                    self.feedback(name, 'No matching job found.', False)
                else:
                    output = self.jobs.get_job_output(job['job_id'], max_chars=2000)
                    res_str = f'job_id={job["job_id"]} status={job["status"]} exit_code={job["exit_code"]} output={output}'
                    self.feedback(name, res_str, True)
            elif name == 'cancel_job':
                job_id = args.get('job_id')
                if not job_id:
                    active = self.jobs.get_active_job(self.id)
                    job_id = active['job_id'] if active else None
                if not job_id:
                    self.feedback(name, 'No job specified or active to cancel.', False)
                else:
                    job = self.jobs.cancel_job(job_id)
                    self.event('job_cancelled', f'job_id={job_id}')
                    status = job.get('status') if job else 'not_found'
                    self.feedback(name, f'Cancelled job {job_id}. Status: {status}', True)
            elif name == 'board_message':
                recipient = args.get('recipient','ALL')
                known = {x['id'] for x in read_json(Path('/etc/ai-village/runtime-peers.json'),[])}
                if recipient != 'ALL' and recipient not in known:
                    raise ValueError('recipient must be ALL or exact agent ID from peers')
                reply_to = args.get('reply_to', '')
                if reply_to == 'discussion_target':
                    reply_to = self.state.get('discussion_target_id') or ''
                message = f'to={recipient}; reply_to={str(reply_to)[:120]}; message={args["message"][:4000]}'
                if hasattr(self.tasks, 'store'):
                    self.tasks.store.post_inbox_message(
                        source='direct' if recipient != 'ALL' else 'board',
                        sender=self.id,
                        recipient=recipient if recipient != 'ALL' else None,
                        reply_to=reply_to,
                        content=args["message"][:4000],
                    )
                if recipient != 'ALL':
                    log = self.state.setdefault('dm_log', {})
                    log[recipient] = [t for t in log.get(recipient, []) if t > time.time() - 3600] + [time.time()]
                    if recipient == self.pair_partner():
                        self.state['pair_index'] = int(self.state.get('pair_index', 0)) + 1
                feedback_text = 'Message posted. A reply is not guaranteed; continue independent work.'
                # P42-continuation: a prompt-only ownership reminder did not change
                # behaviour when re-observed live (03-librarian kept re-announcing a
                # task 02-explorer already owned, 5x in a 30-minute follow-up window).
                # A mechanical check on the actual message text, giving concrete
                # feedback rather than a hint, is the deterministic-guard pattern this
                # codebase already uses elsewhere (e.g. foreign-home detection).
                if hasattr(self.tasks, 'store'):
                    for candidate in re.findall(r'\b[0-9a-f]{12}\b', args["message"]):
                        owned = self.tasks.store.get_task(candidate)
                        if owned and owned.get('owner') and owned['owner'] != self.id:
                            feedback_text += (f" Note: task {candidate} \"{owned.get('title','')}\" is already "
                                              f"owned by {owned['owner']}, not you - coordinate with them or work on something else.")
                            break
                self.feedback(name,feedback_text,True)
            elif name == 'task_operation':
                result=self.tasks.operate(self.id,args)
                self.event('task_result',json.dumps(result)); self.feedback(name,json.dumps(result),True)
                if args.get('action') in ('complete','yield'):
                    self.auto_record(f'task {args.get("task_id")} {args.get("action")}: {str(args.get("evidence",""))[:400]}', 'task:'+str(args.get('task_id'))+str(args.get('action')))
            elif name == 'team_operation':
                operation = args.get('operation') or args.get('action')
                if operation == 'create':
                    result = self.teams.create(self.id, args)
                elif operation == 'join':
                    result = self.teams.join(self.id, args.get('team_id'), args.get('role_variant'))
                elif operation == 'leave':
                    result = self.teams.leave(self.id, args.get('team_id'))
                elif operation == 'create_subtask':
                    result = self.teams.create_subtask(self.id, args.get('team_id'), args)
                elif operation == 'claim_subtask':
                    result = self.teams.claim_subtask(self.id, args.get('subtask_id'))
                elif operation == 'complete_subtask':
                    result = self.teams.complete_subtask(self.id, args.get('subtask_id'), args.get('evidence'))
                elif operation == 'propose_role':
                    result = self.teams.propose_role(self.id, args.get('team_id'), args.get('role'), args.get('rationale'))
                elif operation == 'vote_role':
                    result = self.teams.vote_role(self.id, args.get('proposal_id'), args.get('choice'))
                else:
                    raise ValueError('team_operation requires a supported operation')
                self.event('team_result', json.dumps(result, ensure_ascii=False)); self.feedback(name, json.dumps(result, ensure_ascii=False), True)
            elif name == 'artifact_operation':
                op = args.get('operation') or args.get('action')
                art_id = args.get('artifact_id')
                if not op or not art_id:
                    raise ValueError('artifact_operation requires operation/action and artifact_id')
                if op == 'register':
                    res = self.artifacts.register_artifact(
                        artifact_id=art_id,
                        owner=self.id,
                        file_path=args['file_path'],
                        test_description=args.get('test_description', ''),
                        task_id=args.get('task_id'),
                    )
                elif op == 'claim_success':
                    res = self.artifacts.claim_success(
                        artifact_id=art_id,
                        claimant=self.id,
                        test_command=args.get('test_command'),
                        env=self.env,
                    )
                elif op == 'verify':
                    passed, res = self.artifacts.verify_artifact(
                        artifact_id=art_id,
                        verifier=self.id,
                        test_command=args.get('test_command', ''),
                        verdict_type=args.get('verdict_type', 'automated_reproduction'),
                        env=self.env,
                        details=args.get('details', ''),
                    )
                elif op == 'adopt':
                    res = self.artifacts.adopt_artifact(
                        artifact_id=art_id,
                        adopter=self.id,
                    )
                elif op == 'inspect':
                    res = self.artifacts.get_artifact(art_id)
                    if not res:
                        raise ValueError(f"Artifact '{art_id}' not found.")
                else:
                    raise ValueError(f"Unknown artifact operation '{op}'")
                self.event('artifact_operation', f'op={op}; id={art_id}; status={res.get("status") if isinstance(res, dict) else "ok"}')
                self.feedback(name, json.dumps(res, ensure_ascii=False), True)
            elif name in ('memory_remember','memory_search'):
                value = {'agent':self.id, 'content':args.get('content',''), 'kind':args.get('kind','observation'),
                         'scope':args.get('scope','private'), 'source_event': self.state.get('updated_at',''),
                         'query':args.get('query','')}
                result=self.memory('/v1/memories' if name=='memory_remember' else '/v1/search',value)
                self.event('memory_result',f'result=success; action={name}; id={result.get("id", "")}; scope={args.get("scope","private")}; matches={len(result.get("items",[]))}')
                self.feedback(name,json.dumps(result),True)
            elif name == 'research_request':
                result = self.research.search(args.get('source'), args.get('query'), args.get('limit', 5))
                self.event('research_result', json.dumps({k: result.get(k) for k in ('source', 'query', 'sha256', 'results')}, ensure_ascii=False))
                self.feedback(name, json.dumps(result, ensure_ascii=False), True)
            elif name == 'calc_operation':
                tool = args.get('tool')
                tool_args = {k: v for k, v in args.items() if k != 'tool'}
                try:
                    res = call_tool(tool, tool_args)
                except TypeError as exc:
                    raise ValueError(f'invalid arguments for {tool}: {exc}') from exc
                self.event('calc_result', f'tool={tool}; result={json.dumps(res, ensure_ascii=False)}')
                self.feedback(name, json.dumps(res, ensure_ascii=False), True)
            elif name == 'meeting_operation':
                op = args.get('operation') or args.get('action')
                if op == 'report':
                    result = self.meetings.report(args['meeting_id'], self.id, args.get('achieved',''), args.get('evidence',''), args.get('next_step',''), args.get('blockers',''))
                    # P57: the submitted blockers text used to be entirely
                    # invisible in telemetry - meeting_result only ever
                    # carried {meeting_id, agent_id, saved}. Without it,
                    # nothing (not the Auditor, not an operator) could ever
                    # tell that an agent named the same unresolved blocker
                    # meeting after meeting despite recognizing it each time.
                    blockers_text = str(args.get('blockers', '')).strip()[:300]
                    self.event('meeting_result', f"meeting_id={args['meeting_id']}; agent_id={self.id}; "
                                                  f"saved=true; blockers={blockers_text}")
                elif op == 'close':
                    result = self.meetings.close(args['meeting_id'])
                    self.event('meeting_result', json.dumps(result, ensure_ascii=False))
                else:
                    raise ValueError('meeting_operation requires report or close')
                self.feedback(name, json.dumps(result, ensure_ascii=False), True)
            elif name == 'research_proposal':
                op = args.get('operation') or args.get('action')
                store = self.tasks.store
                if op == 'propose':
                    topic = str(args.get('topic', '')).strip()
                    if not topic:
                        raise ValueError('research_proposal propose requires a topic')
                    result = store.propose_research(self.id, topic, args.get('rationale', ''))
                elif op == 'endorse':
                    proposal_id = args.get('proposal_id')
                    if not proposal_id:
                        raise ValueError('research_proposal endorse requires proposal_id')
                    result = store.endorse_research(proposal_id, self.id)
                elif op == 'list':
                    result = store.list_research_proposals(args.get('status'))
                else:
                    raise ValueError('research_proposal requires operation propose, endorse, or list')
                self.event('research_proposal_result', json.dumps(result, ensure_ascii=False)[:2000])
                self.feedback(name, json.dumps(result, ensure_ascii=False), True)
            elif name == 'gazette_operation':
                # P47 (operator, 2026-09-28): the daily AI Village Gazette - a
                # chronicle the residents write themselves, bounded contributions
                # only ("nur ein kleiner Teil pro Agent"), King draws the day's
                # game+pair. Compiling this into a rendered edition is a later,
                # separate stage (see docs/analysis/GAZETTE-PLAN-2026-09-28.md).
                op = args.get('operation') or args.get('action')
                if op == 'open':
                    if self.id != '01-king':
                        self.feedback(name, 'Only 01-king may open today\'s gazette edition.', False)
                    else:
                        peers = [p.get('id') for p in read_json(Path('/etc/ai-village/runtime-peers.json'), [])
                                if p.get('id') and p.get('id') != self.id]
                        # P71: accepts an explicit edition_id like every
                        # other gazette op now does, defaulting to today's
                        # date as before when omitted (the normal case for
                        # King's own daily routine) - an operator-directed
                        # out-of-band edition no longer needs a store-level
                        # bypass of this action.
                        result = self.gazette.open_edition(self.id, peers, edition_id=args.get('edition_id') or None)
                        self.event('gazette_opened', json.dumps(result, ensure_ascii=False)[:1000])
                        self.feedback(name, json.dumps(result, ensure_ascii=False), True)
                elif op == 'assign':
                    # P54: King's own, real delegation act - a generic hint
                    # (P52) and an automatic assignment merely attributed to
                    # King in text (P53's first version) both proved
                    # insufficient/dishonest; this is the only code path
                    # that ever writes gazette_assignments.
                    if self.id != '01-king':
                        self.feedback(name, 'Only 01-king may assign gazette contribution kinds.', False)
                    else:
                        peers = [p.get('id') for p in read_json(Path('/etc/ai-village/runtime-peers.json'), [])
                                if p.get('id') and p.get('id') != self.id]
                        edition_id = args.get('edition_id') or self.gazette_active_edition_id()
                        try:
                            result = self.gazette.assign_kinds(edition_id, self.id, peers)
                        except ValueError as exc:
                            self.feedback(name, str(exc), False)
                        else:
                            self.event('gazette_assigned', json.dumps(result, ensure_ascii=False)[:1000])
                            self.feedback(name, json.dumps(result, ensure_ascii=False), True)
                elif op == 'contribute':
                    kind = args.get('kind')
                    headline = args.get('headline', '')
                    content = args.get('content', '')
                    edition_id = args.get('edition_id') or self.gazette_active_edition_id()
                    try:
                        result = self.gazette.submit_contribution(edition_id, self.id, kind, headline, content)
                    except ValueError as exc:
                        self.feedback(name, str(exc), False)
                    else:
                        self.event('gazette_contribution', f'edition={edition_id}; kind={kind}')
                        self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'review':
                    # P55 (operator directive): "die Zeitung sollte nicht aus
                    # ungeprueften Beitraegen bestehen" - only the Chronicler
                    # may approve/reject; compile_edition() then excludes
                    # anything not explicitly approved.
                    if self.id != GAZETTE_REVIEWER:
                        self.feedback(name, f'Only {GAZETTE_REVIEWER} may review gazette contributions.', False)
                    else:
                        edition_id = args.get('edition_id') or self.gazette_active_edition_id()
                        try:
                            result = self.gazette.review_contribution(
                                edition_id, args.get('agent'), args.get('kind'), self.id,
                                args.get('decision'), args.get('note', ''))
                        except ValueError as exc:
                            self.feedback(name, str(exc), False)
                        else:
                            self.event('gazette_reviewed',
                                       f'edition={edition_id}; agent={args.get("agent")}; kind={args.get("kind")}; '
                                       f'decision={args.get("decision")}')
                            self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'close':
                    if self.id != '01-king':
                        self.feedback(name, "Only 01-king may close/compile today's gazette edition.", False)
                    else:
                        edition_id = args.get('edition_id') or self.gazette_active_edition_id()
                        try:
                            result = self.gazette.close_edition(edition_id, self.id)
                        except ValueError as exc:
                            self.feedback(name, str(exc), False)
                        else:
                            # Archive is write-once: compiled_html is only present
                            # on the first real compile (close_edition() is
                            # idempotent), and the file itself is never overwritten.
                            compiled_html = result.pop('compiled_html', None)
                            issue_number = result.pop('issue_number', None)
                            previous_id = result.pop('previous_id', None)
                            if compiled_html:
                                archive_root = self.root / 'gazette'
                                archive_dir = archive_root / 'archive' / edition_id
                                archive_dir.mkdir(parents=True, exist_ok=True)
                                # P64: Path.mkdir() inherited this process's
                                # (King's) restrictive umask - every level came
                                # out 0700/2700, unreadable by anyone but King
                                # and root. The dashboard (village-web) is a
                                # member of the shared ai-village group, same
                                # as coordination.sqlite3/events.jsonl (0660) -
                                # made every directory level this call may have
                                # just created group-readable/traversable to
                                # match that existing convention.
                                for level in (archive_root, archive_root / 'archive', archive_dir):
                                    try: level.chmod(0o2750)
                                    except OSError: pass
                                archive_path = archive_dir / 'index.html'
                                if not archive_path.exists():
                                    archive_path.write_text(compiled_html, encoding='utf-8')
                                    try: archive_path.chmod(0o640)
                                    except OSError: pass
                                # P66: Gazette PDF export (Stufe 4, Teil 2) -
                                # same write-once archive guarantee as the
                                # HTML file. A rendering failure here must
                                # never lose the already-written, more
                                # important HTML archive or block the close
                                # itself - logged and skipped, not raised.
                                pdf_path = archive_dir / 'gazette.pdf'
                                if not pdf_path.exists():
                                    try:
                                        pdf_bytes = render_edition_pdf(result, issue_number, previous_id)
                                        pdf_path.write_bytes(pdf_bytes)
                                        pdf_path.chmod(0o640)
                                    except Exception as exc:
                                        self.event('gazette_pdf_failed', f'edition={edition_id}; error={exc}')
                            self.event('gazette_compiled', f'edition={edition_id}')
                            self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'declare_winner':
                    # P74 (operator feedback): "womit gewonnen hat [...]
                    # den benannten Gewinner" - GAME_POOL's own text already
                    # names King as arbiter ("King kuert einen Favoriten"),
                    # same restriction pattern as open/assign/close.
                    if self.id != '01-king':
                        self.feedback(name, "Only 01-king may declare today's game winner.", False)
                    else:
                        edition_id = args.get('edition_id') or self.gazette_active_edition_id()
                        try:
                            result = self.gazette.declare_game_winner(
                                edition_id, self.id, args.get('winner'), args.get('note', ''))
                        except ValueError as exc:
                            self.feedback(name, str(exc), False)
                        else:
                            self.event('gazette_game_winner_declared',
                                       f'edition={edition_id}; winner={args.get("winner")}')
                            self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'view':
                    edition_id = args.get('edition_id') or gazette_today()
                    result = self.gazette.get_edition(edition_id)
                    self.feedback(name, json.dumps(result, ensure_ascii=False)[:3000] if result else 'No edition yet for that date.', bool(result))
                else:
                    raise ValueError('gazette_operation requires operation open, assign, contribute, review, close, declare_winner, or view')
            elif name == 'calendar_operation':
                # P75 (operator directive): every resident's own real
                # action, never a background computation on their behalf -
                # same principle as gazette assign_kinds()/King's open.
                op = args.get('operation') or args.get('action')
                if op == 'create':
                    try:
                        result = self.calendar.create_event(
                            self.id, args.get('title'), args.get('kind'), args.get('scheduled_date'),
                            args.get('start_time'), args.get('duration_minutes'), args.get('attendees') or [],
                            args.get('recurrence') or 'none', args.get('notes', ''))
                    except (ValueError, TypeError) as exc:
                        self.feedback(name, str(exc), False)
                    else:
                        self.event('calendar_created', f"id={result['id']}; kind={result['kind']}; "
                                   f"date={result['scheduled_date']}; recurrence={result.get('recurrence')}")
                        self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'reschedule':
                    try:
                        result = self.calendar.reschedule_event(
                            args.get('event_id'), self.id, args.get('new_date'), args.get('new_time'),
                            args.get('reason', ''))
                    except ValueError as exc:
                        self.feedback(name, str(exc), False)
                    else:
                        self.event('calendar_rescheduled', f"id={args.get('event_id')}; "
                                   f"date={result['scheduled_date']}; time={result['start_time']}")
                        self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'cancel':
                    try:
                        result = self.calendar.cancel_event(
                            args.get('event_id'), self.id, args.get('reason', ''), bool(args.get('whole_series')))
                    except ValueError as exc:
                        self.feedback(name, str(exc), False)
                    else:
                        self.event('calendar_cancelled', f"id={args.get('event_id')}; "
                                   f"whole_series={bool(args.get('whole_series'))}")
                        self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'respond':
                    try:
                        result = self.calendar.respond(
                            args.get('event_id'), self.id, args.get('response'),
                            args.get('proposed_date'), args.get('proposed_time'))
                    except ValueError as exc:
                        self.feedback(name, str(exc), False)
                    else:
                        self.event('calendar_responded', f"id={args.get('event_id')}; "
                                   f"response={args.get('response')}")
                        self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'list':
                    events = self.calendar.list_for_agent(self.id, args.get('date_from'), args.get('date_to'))
                    self.feedback(name, json.dumps(events, ensure_ascii=False)[:3000], True)
                else:
                    raise ValueError('calendar_operation requires operation create, reschedule, cancel, respond, or list')
            else:
                self.feedback('idle','Intentional rest; next turn may resume your own project.',True)
                self.event('idle','intentional rest')
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.feedback(name,f'{type(exc).__name__}: {exc}',False)
            self.event('action_error',f'action={name}; error={exc}')

    def capability_summary(self, ttl_seconds: float = 1800.0) -> str:
        """Cached, human-readable summary of what this agent can actually do in
        its own environment - private/shared storage, available CLI tools,
        rootless-container and GPU readiness, how to request more via
        village-authority. village/containers.py::describe_agent_capabilities()
        (P19) computed exactly this but was never surfaced to a resident's own
        prompt before this; an agent cannot use a habitat it does not know it
        has. Re-audited at most every ttl_seconds (host audits run real
        subprocesses - nvidia-smi, podman - not worth repeating every cycle),
        and any audit failure degrades to an honest "unknown", never a crash."""
        cached_at = self.state.get('capabilities_at', 0)
        if time.time() - cached_at < ttl_seconds and self.state.get('capabilities_summary'):
            return self.state['capabilities_summary']
        try:
            import getpass
            from village.containers import audit_host_environment, describe_agent_capabilities
            audit = audit_host_environment()
            desc = describe_agent_capabilities(self.id, getpass.getuser(), {}, audit)
            tools = sorted(name for name, path in desc['tools'].items() if path)
            summary = (
                f"Your habitat: private storage {desc['storage']['private_directory']}, "
                f"shared storage {desc['storage']['shared_directory']} (read-write, all agents). "
                f"CLI tools available to you: {', '.join(tools) or 'none detected'}. "
                f"Rootless containers: {'ready' if desc['containers']['rootless_supported'] else 'not ready yet'}. "
                f"GPU compute: {'ready' if desc['gpu']['gpu_compute_ready'] else 'not ready yet'}. "
                f"Request more capability via: {desc['authority']['request_channel']}."
            )
        except Exception as exc:
            summary = f"Habitat capabilities unknown this cycle ({type(exc).__name__}: {exc})."
        self.state['capabilities_summary'] = summary
        self.state['capabilities_at'] = time.time()
        return summary

    def failure_tally(self, ttl_seconds: float = 600.0) -> list:
        """P46 (operator, 2026-09-28): "Task A Strichliste mit Fehlversuch/Erfolg
        je gewaehlten Weg und das wegspeichern" - live evidence the same day
        showed a single delivered Auditor correction is read once and forgotten
        by the next cycle (03-librarian corrected 7x for the identical
        unit-conversion bug over 3.5 hours; 01-king/04-artisan hit
        format_violation on almost every 1h rate-limit reset, no improvement).
        This turns village/auditor.py's own (rejected, corrected) history -
        already persisted, already used for fine-tuning export - into the
        cumulative experience tally this project never fed back as a decision
        input: "you have made this exact mistake N times", not a one-off note.
        Read-only against the auditor's own SQLite file; any failure (auditor
        never ran yet, file locked) degrades to an empty list, never a crash."""
        cached_at = self.state.get('failure_tally_at', 0)
        if time.time() - cached_at < ttl_seconds and 'failure_tally_cache' in self.state:
            return self.state['failure_tally_cache']
        try:
            from village.auditor import AuditStore
            tally = AuditStore(self.root / 'telemetry' / 'audit.sqlite3').tally_for_agent(self.id)
        except Exception:
            tally = []
        self.state['failure_tally_cache'] = tally
        self.state['failure_tally_at'] = time.time()
        return tally

    def cycle(self):
        # P01/P07: Respect persistent pause marker and drain/abort modes
        if is_paused(self.pause_marker):
            meta = read_pause_metadata(self.pause_marker)
            mode = meta.get('mode', 'drain')
            self.event('cycle_skipped_paused', f'mode={mode}; reason={meta.get("reason", "paused")}', True)
            return

        snapshot = self.snapshot()
        identity = Path(self.env['AGENT_IDENTITY_PROMPT']).read_text()
        compact = self.policy.prompt_profile == 'compact'
        share = self.env.get('VILLAGE_SHARE_DIR', '/usr/local/share/ai-village')
        full_text = '' if compact else Path(f'{share}/system-prompt.txt').read_text()
        core_text = Path(f'{share}/system-prompt-core.txt').read_text() if compact else ''
        # Older founding profiles may name a previous model: current environment wins.
        live = (f'Current runtime model={self.env["OLLAMA_MODEL"]}, context={self.env.get("OLLAMA_NUM_CTX")}, '
               f'role={self.role}. These override stale model details in founding identity. '
               + self.capability_summary())
        action_format = self.effective_action_format()
        # P72: idle-only for this cycle after sustained failure - restricts
        # the schema sent to the model AND its own system prompt, not just
        # advisory text (see effective_allowed_actions()).
        allowed_now = self.effective_allowed_actions()
        checkpoint = getattr(self, 'current_collaboration_checkpoint', None)
        consult_peer = checkpoint.peer_id if (checkpoint and checkpoint.stage == 'consult' and 'board_message' in allowed_now) else None
        response_format = action_schema(allowed_now, self.peer_ids(), consult_peer) if action_format == 'schema' else None
        messages = [
            {'role': 'system', 'content': build_system_prompt(self.policy.prompt_profile, core_text, full_text, identity, live,
                                                              allowed_now, action_format, self.policy.role_brief)},
            {'role': 'user', 'content': snapshot + user_suffix(action_format)}
        ]
        api_type = self.env.get('API_TYPE', 'ollama').lower()
        api_token = self.env.get('API_TOKEN', self.env.get('OLLAMA_API_TOKEN'))
        start = time.monotonic()

        # P06: Create persistent request record before network dispatch (state=queued -> requesting)
        req_record = self.tracker.create_request(
            model=self.env['OLLAMA_MODEL'],
            provider=api_type,
            generation=self.generation,
        )
        self.tracker.start_request(req_record.request_id)
        self.event('inference_started', f'request_id={req_record.request_id} generation={self.generation} provider={api_type} model={self.env["OLLAMA_MODEL"]} context={self.env.get("OLLAMA_NUM_CTX")}', True)
        if api_type == 'openai':
            request = build_openai_request(
                url=self.env['OLLAMA_URL'],
                model=self.env['OLLAMA_MODEL'],
                messages=messages,
                num_predict=int(self.env.get('OLLAMA_NUM_PREDICT', '768')),
                think_level=self.env.get('OLLAMA_THINK_LEVEL', 'off'),
                api_token=api_token,
            )
        else:
            request = build_ollama_request(
                url=self.env['OLLAMA_URL'],
                model=self.env['OLLAMA_MODEL'],
                messages=messages,
                num_ctx=int(self.env.get('OLLAMA_NUM_CTX', '8192')),
                num_predict=int(self.env.get('OLLAMA_NUM_PREDICT', '768')),
                think_level=self.env.get('OLLAMA_THINK_LEVEL', 'off'),
                keep_alive=self.env.get('OLLAMA_KEEP_ALIVE', '10m'),
                api_token=api_token,
                response_format=response_format,
            )
        try:
            with urllib.request.urlopen(request, timeout=int(self.env.get('VILLAGE_OLLAMA_TIMEOUT_SECONDS', '3600'))) as response:
                raw_answer = json.load(response)
            elapsed_ms = int((time.monotonic() - start) * 1000)
            if api_type == 'openai':
                norm = normalize_openai_response(raw_answer, elapsed_ms)
                gpu_verified = False  # Cloud/remote blackbox has no verifiable local GPU telemetry
            else:
                norm = normalize_ollama_response(raw_answer, elapsed_ms)
                # P06: Only assert GPU generation when explicitly verified by backend response
                gpu_verified = bool(raw_answer.get('gpu_verified', False))
            answer = norm.normalized_answer
            metrics = norm.metrics
            prompt_tokens = metrics.get('prompt_eval_count') or metrics.get('prompt_tokens')
            completion_tokens = metrics.get('eval_count') or metrics.get('completion_tokens')

            # Record completed inference metrics in persistent store
            self.tracker.complete_request(
                req_record.request_id,
                duration_ms=elapsed_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                gpu_verified=gpu_verified,
            )
            self.event('inference_finished', f'request_id={req_record.request_id} duration_ms={elapsed_ms}; gpu_verified={gpu_verified}; metrics={json.dumps(metrics)}', True)
            # Private last output, bounded; do not broadcast rejected generations to peers.
            write_json(self.home / 'last-response.json', dict(metrics=metrics, content=self.redact(final_content(norm.content))[:65536]))
            parsed = decision(answer, allowed_now)
            if action_format == 'schema':
                conforming = not parsed.get('fallback_reason') and not parsed.get('prose')
                streak = 0 if conforming else int(self.state.get('schema_failure_streak', 0)) + 1
                self.state['schema_failure_streak'] = streak
                if streak >= 3:
                    self.state['schema_disabled_until'] = time.time() + 3600
                    self.state['schema_failure_streak'] = 0
                    self.event('action_format_fallback', 'schema output failed 3 times in a row; using text protocol for 1 hour', True)
            if not parsed.get('fallback_reason'):
                self.state['seen_board_epoch'] = self.pending_cursor
                self.state['seen_organic_epoch'] = self.pending_organic_cursor
                # P09: Explicitly acknowledge messages delivered in this successful turn
                if getattr(self, 'delivered_inbox_ids', None) and hasattr(self.tasks, 'store'):
                    try:
                        self.tasks.store.acknowledge_messages(self.id, self.delivered_inbox_ids)
                    except sqlite3.Error as exc:
                        self.event('inbox_ack_error', f'{type(exc).__name__}: {exc}', True)
                    self.delivered_inbox_ids = []
                self.state['auth_failure'] = False
                self.state['transient_failure_streak'] = 0
                self.state['last_error_category'] = None

            # P06: Atomically authorize execution against active generation and duplicate run prevention
            if self.tracker.mark_action_executed(req_record.request_id, self.generation):
                self.execute(parsed)
            else:
                self.event('action_discarded', f'request_id={req_record.request_id} execution denied: generation mismatch or duplicate execution', True)
        except (OSError, ValueError) as exc:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            meta = read_pause_metadata(self.pause_marker) if is_paused(self.pause_marker) else {}
            if meta.get('mode') == 'abort':
                # P07: Abort mode explicitly cancels active request in lifecycle store
                self.tracker.confirm_cancellation(
                    req_record.request_id,
                    backend_confirmed=False,
                    detail=f'Interrupted during inference by operator abort: {meta.get("reason", "abort")}',
                )
                self.event('inference_aborted', f'request_id={req_record.request_id} reason={meta.get("reason", "abort")}', True)
            else:
                # An HTTPError's body is a stream that can only be read once; read it
                # here and hand the same text to classify_error instead of letting it
                # read the (by then exhausted) stream a second time.
                detail = exc.read(2048).decode(errors='replace') if isinstance(exc, urllib.error.HTTPError) else str(exc)
                err_class, err_detail = classify_error(exc, body=detail if isinstance(exc, urllib.error.HTTPError) else None)
                self.tracker.fail_request(
                    req_record.request_id,
                    duration_ms=elapsed_ms,
                    error_class=err_class,
                    error_detail=err_detail,
                )
                if action_format == 'schema' and isinstance(exc, urllib.error.HTTPError) and exc.code in (400, 422, 501):
                    self.state['schema_disabled_until'] = time.time() + 6 * 3600
                    self.event('action_format_fallback', f'endpoint rejected structured output (HTTP {exc.code}); using text protocol for 6 hours', True)
                self.feedback('inference_error', detail, False)
                # Telemetry only (never fed back into the agent's own prompt): a full
                # traceback for anything not already explained by an HTTP status body,
                # so a recurring error_class=unknown_error can actually be root-caused
                # instead of only re-showing the same one-line str(exc) every time.
                diagnostic = f'request_id={req_record.request_id} error_class={err_class} detail={detail}'
                if not isinstance(exc, urllib.error.HTTPError):
                    diagnostic += ' traceback=' + traceback.format_exc().replace('\n', ' | ')
                self.event('inference_error', diagnostic, True)
                # P11: Classify error cause and update state with appropriate backoff category
                if err_class == 'auth_error':
                    self.state['auth_failure'] = True
                    self.state['last_error_category'] = 'auth_error'
                    self.feedback('auth_error', f'Authentication failed: {detail}. Check API token configuration.', False)
                elif err_class in ('timeout', 'connection_error'):
                    self.state['transient_failure_streak'] = self.state.get('transient_failure_streak', 0) + 1
                    self.state['last_error_category'] = 'network_error'
                else:
                    self.state['last_error_category'] = 'inference_error'
                write_json(self.path, self.state)

    def compute_cycle_delay(self) -> int:
        """Calculate dynamic sleep delay based on error classification and backoff policies."""
        base_delay = int(self.env.get('VILLAGE_CYCLE_SECONDS', '120'))
        if self.state.get('auth_failure'):
            return max(base_delay, 900)
        if self.state.get('invalid_streak', 0) >= 3:
            return max(base_delay, 900)
        transient_streak = self.state.get('transient_failure_streak', 0)
        if transient_streak > 0:
            return min(max(base_delay, 15 * (2 ** min(transient_streak - 1, 3))), 900)
        return max(base_delay, 0)

    def run(self):
        os.umask(0o077)
        self.event('agent_start',f'runtime={VERSION}; model={self.env["OLLAMA_MODEL"]}')
        def stop(*_): self.stopping=True; raise SystemExit(0)
        signal.signal(signal.SIGTERM,stop); signal.signal(signal.SIGINT,stop)
        try:
            while not self.stopping:
                # P01/P07: Idle while persistent pause or drain is active
                if is_paused(self.pause_marker):
                    time.sleep(2)
                    continue
                try:
                    self.cycle()
                    self.state['runtime_exception_streak'] = 0
                except Exception as exc:
                    # Keep a malformed model response or local integration
                    # defect from becoming an opaque systemd restart loop.
                    streak = self.state.get('runtime_exception_streak', 0) + 1
                    self.state['runtime_exception_streak'] = streak
                    detail = self.redact(f'{type(exc).__name__}: {exc}')[:1200]
                    self.event('runtime_exception', f'streak={streak}; detail={detail}', True)
                    self.feedback('runtime_exception', detail, False)
                    write_json(self.path, self.state)
                    time.sleep(min(900, max(15, 15 * (2 ** min(streak - 1, 5)))))
                    continue
                delay = self.compute_cycle_delay()
                # P12: Event-driven wakeup: sleep in short intervals up to delay, waking early on
                # new unacknowledged inbox messages or active job completion
                slept = 0.0
                min_wake = float(self.env.get('VILLAGE_MIN_WAKE_SECONDS', '2'))
                # Backoff states (invalid streak, auth failure) must not be defeated by wakeups.
                wake_allowed = not (self.state.get('auth_failure') or self.state.get('invalid_streak', 0) >= 3)
                had_active_job = bool(self.jobs.get_active_job(self.id))
                while slept < delay and not self.stopping:
                    if is_paused(self.pause_marker):
                        break
                    interval = min(1.0, max(0.1, delay - slept))
                    time.sleep(interval)
                    slept += interval
                    # Wakeup check: inbox message arrival
                    if wake_allowed and slept >= min_wake and hasattr(self.tasks, 'store'):
                        try:
                            pending = self.tasks.store.fetch_undelivered_wakeups(self.id, limit=1)
                        except sqlite3.Error:
                            pending = []
                        if pending:
                            self.event('event_wakeup', f'Waking early from sleep: undelivered message {pending[0]["id"]} from {pending[0]["sender"]}', True)
                            break
                    # Wakeup check: active background job finished
                    if had_active_job:
                        active = self.jobs.get_active_job(self.id)
                        if not active:
                            self.event('event_wakeup', 'Waking early from sleep: background job completed', True)
                            break
        finally:
            self.event('agent_stop','runner stopped')


if __name__ == '__main__':
    Resident().run()

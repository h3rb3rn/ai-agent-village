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
from village.gazette import today as gazette_today
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
            tools={name: ACTION_SPECS[name]['doc'] for name in normalize_allowed(self.policy.allowed_actions)},
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
        if self.id == '01-king':
            gazette_edition = self.gazette.get_edition(gazette_today())
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
        else:
            # P52: a one-off broadcast from King asking everyone to
            # contribute had the identical problem the direct nudge to King
            # had (P48/P49) - it competes with each resident's own ongoing
            # work and loses. The generic "pick any kind" version of this
            # hint (P52) still produced 0 contributions across all 9
            # residents after ~30 minutes, confirmed delivered. P53
            # escalates to real per-agent delegation: each resident is
            # deterministically assigned one specific kind at open time
            # (village/gazette.py::open_edition), named explicitly here
            # instead of leaving the choice open.
            gazette_edition = self.gazette.get_edition(gazette_today())
            if gazette_edition and not any(c['agent'] == self.id for c in gazette_edition['contributions']):
                assigned_kind = gazette_edition.get('assignments', {}).get(self.id)
                if assigned_kind:
                    context['gazette_daily_note'] = (
                        f"Today's AI Village Gazette edition is open. King has assigned you the "
                        f"'{assigned_kind}' section - send one gazette_operation contribute with "
                        f"kind='{assigned_kind}' and a short (max 400 chars) entry."
                    )
                else:
                    context['gazette_daily_note'] = (
                        "Today's AI Village Gazette edition is open and you have not contributed "
                        "yet. Send one short gazette_operation contribute (max 400 chars) - pick "
                        "any kind that fits: state/mood/wishes/topics/suggestions/learning/outlook/"
                        "village_news (game_result is reserved for today's drawn pair)."
                    )
        if own_project and own_project.get('blockers'):
            context['task_blocker_guidance'] = (
                f"Your active task {own_project['id']} has blockers: {own_project['blockers']}. "
                "You may work on independent unblocked steps, yield the task, or claim an alternative open task without waiting for external approval."
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
                      'recent_organic_messages_untrusted','untrusted_direct_messages'):
            while context.get(field) and len(json.dumps(context,ensure_ascii=False))>budget:
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

        if name not in self.policy.allowed_actions:
            self.feedback(name, f'Action {name} is not available to you. Choose one of: {", ".join(self.policy.allowed_actions)}.', False)
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

        # Open meetings are a bounded social checkpoint. Keep the request
        # advisory: small models may fail to emit the structured report, and a
        # hard gate would deadlock the village and suppress useful work.
        if name not in ('meeting_operation', 'idle'):
            pending = next((m for m in self.meetings.active() if not self.meetings.has_report(m['id'], self.id)), None)
            if pending:
                self.feedback(name, f"Meeting report requested: {pending['id']}. Submit one meeting_operation report when possible; continuing this reversible action.", True)
                self.event('meeting_required', f"meeting_id={pending['id']}")

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
            self.feedback('invalid_decision', parsed['fallback_reason']+'. No action executed. Correct your envelope: {"name":"tool_name","arguments":{...}}.'+meeting_hint+' Your rejected final text: '+preview, False)
            self.event('invalid_decision', parsed['fallback_reason'])
            # village/auditor.py::detect_format_violation() keys on this exact
            # event name and 'reason=...; preview=...' shape (P43): the bare
            # 'invalid_decision' event above has no preview and cannot feed the
            # auditor's fine-tuning export, which needs the actual rejected text.
            self.event('invalid_decision_detail', f'reason={parsed["fallback_reason"]}; preview={preview}')
            # A rejected generation is never published: it stays in the resident's
            # private last-response.json (bounded, redacted) and is only counted.
            self.state['last_rejected_fingerprint'] = hashlib.sha256(str(preview).encode('utf-8')).hexdigest()[:16]
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
                elif op == 'close':
                    result = self.meetings.close(args['meeting_id'])
                else:
                    raise ValueError('meeting_operation requires report or close')
                self.event('meeting_result', json.dumps(result, ensure_ascii=False)); self.feedback(name, json.dumps(result, ensure_ascii=False), True)
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
                        result = self.gazette.open_edition(self.id, peers)
                        self.event('gazette_opened', json.dumps(result, ensure_ascii=False)[:1000])
                        self.feedback(name, json.dumps(result, ensure_ascii=False), True)
                elif op == 'contribute':
                    kind = args.get('kind')
                    content = args.get('content', '')
                    edition_id = args.get('edition_id') or gazette_today()
                    try:
                        result = self.gazette.submit_contribution(edition_id, self.id, kind, content)
                    except ValueError as exc:
                        self.feedback(name, str(exc), False)
                    else:
                        self.event('gazette_contribution', f'edition={edition_id}; kind={kind}')
                        self.feedback(name, json.dumps(result, ensure_ascii=False)[:2000], True)
                elif op == 'view':
                    edition_id = args.get('edition_id') or gazette_today()
                    result = self.gazette.get_edition(edition_id)
                    self.feedback(name, json.dumps(result, ensure_ascii=False)[:3000] if result else 'No edition yet for that date.', bool(result))
                else:
                    raise ValueError('gazette_operation requires operation open, contribute, or view')
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
        checkpoint = getattr(self, 'current_collaboration_checkpoint', None)
        consult_peer = checkpoint.peer_id if (checkpoint and checkpoint.stage == 'consult' and 'board_message' in self.policy.allowed_actions) else None
        response_format = action_schema(self.policy.allowed_actions, self.peer_ids(), consult_peer) if action_format == 'schema' else None
        messages = [
            {'role': 'system', 'content': build_system_prompt(self.policy.prompt_profile, core_text, full_text, identity, live,
                                                              self.policy.allowed_actions, action_format, self.policy.role_brief)},
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
            parsed = decision(answer, self.policy.allowed_actions)
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

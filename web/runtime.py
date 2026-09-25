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
import subprocess
import time
import urllib.error
import urllib.request
import uuid
import sys
from datetime import datetime, timezone

ROOT_DIR = Path(__file__).resolve().parents[1]
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
from village.lifecycle import InferenceState, InferenceTracker, classify_error
from village.security import redact_text, sanitize_tool_env

VERSION = '2026-09-25-dynamic-teams-1'


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
        # P01/P07: Pause marker path override from agent environment
        self.pause_marker = Path(self.env.get('VILLAGE_PAUSE_MARKER', '/etc/ai-village/paused'))
        # P06: Track generation sequence and manage persistent inference request lifecycle
        self.generation = int(self.state.get('generation', 1))
        self.tracker = InferenceTracker(self.home / 'inference_lifecycle.sqlite3', self.id)
        # Reconcile incomplete requests from prior crashes or restarts to state 'unknown'
        reconciled = self.tracker.reconcile_stale_requests()
        if reconciled > 0:
            self.event('inference_reconciled', f'reconciled={reconciled} incomplete requests transitioned to unknown', True)
        self.tasks = Tasks(self.board)
        # P10.1/P25.1: project roles are plural, time-bounded team mandates.
        self.teams = TeamStore(self.board / 'coordination.sqlite3')
        self.research = ResearchBroker()
        self.meetings = MeetingStore(self.board / 'coordination.sqlite3')
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
        row = dict(timestamp=now(), agent=self.id, name=self.name, role=self.role,
                   event=event, detail=self.redact(str(detail))[:16000])
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
        # One entry per peer and content hash limits copying/echo dominance.
        chosen, seen = [], set()
        for x in reversed(messages):
            key = x.get('agent')
            if key not in seen:
                chosen.append(x); seen.add(key)
        self.pending_cursor = max((event_time(x) for x in events), default=cutoff)
        own = [x for x in events if x.get('agent') == self.id and x.get('event') in ('command_result', 'memory_result', 'task_result')][-3:]
        projects = read_json(self.tasks.path, [])
        # P09: Sync organic inbox to transactional coordinator store with deterministic IDs
        organic_file = self.board / 'organic-inbox.jsonl'
        organic = tail(organic_file, 50)
        self.pending_organic_cursor = max((event_time(x) for x in organic), default=self.state.get('seen_organic_epoch', 0))
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
            projects=projects[-32:], recent_organic_messages_untrusted=organic[-3:],
            tools={'execute_bash':'command in your home; stdout and exit status returned next turn',
                   'start_job':'command, timeout_seconds? -> launch long-running background job with persistent ID (max 1 mutating job)',
                   'job_status':'job_id? -> poll status and output of background job',
                   'cancel_job':'job_id? -> terminate background job process group',
                   'artifact_operation':'register(artifact_id,file_path,test_description?,task_id?), claim_success(artifact_id,test_command?), verify(artifact_id,test_command,verdict_type?,details?), adopt(artifact_id), inspect(artifact_id)',
                   'board_message':'message plus optional recipient agent ID and reply_to',
                   'task_operation':'create(title,success_criterion,goal?,next_step?), claim(task_id), progress(task_id,last_finding?,next_step?,blockers?), complete(task_id,evidence), yield(task_id,evidence)',
                   'team_operation':'create(project,goal,role,coordination_mode?), join(team_id,role_variant?), leave(team_id), create_subtask(team_id,title,criterion), claim_subtask(subtask_id), complete_subtask(subtask_id,evidence), propose_role(team_id,role,rationale), vote_role(proposal_id,choice)',
                   'memory_remember':'content, kind, scope(private/shared)', 'memory_search':'query, scope(private/shared)',
                   'research_request':'source(wikipedia|github|dockerhub), query, limit?; read-only, no clone/pull/deploy',
                   'meeting_operation':'report(meeting_id, achieved, evidence, next_step, blockers) or close(meeting_id)',
                   'idle':'intentional rest'},
            private_work_directory=str(self.home), groups=os.getgroups())
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
        for field in ('untrusted_peer_messages','own_recent_results','untrusted_direct_messages',
                      'recent_organic_messages_untrusted','retrieved_memory_untrusted','projects'):
            while context.get(field) and len(json.dumps(context,ensure_ascii=False))>budget:
                context[field].pop(0)
        # P09: Track exactly which inbox message IDs survived context trimming to mark delivered
        delivered_ids = []
        for item in context.get('recent_organic_messages_untrusted', []):
            if isinstance(item, dict) and item.get('id'):
                delivered_ids.append(str(item['id']))
        for item in context.get('untrusted_direct_messages', []):
            if isinstance(item, dict) and item.get('id'):
                delivered_ids.append(str(item['id']))
        self.delivered_inbox_ids = delivered_ids
        if delivered_ids and hasattr(self.tasks, 'store'):
            self.tasks.store.mark_messages_delivered(self.id, delivered_ids)
        return json.dumps(context, ensure_ascii=False)

    def guard(self, name, args):
        if is_paused(self.pause_marker) and name in ('execute_bash', 'start_job'):
            self.feedback(name, 'Execution blocked: simulation is paused ("pausiert startet nichts").', False)
            return False

        norm_name = name
        norm_args = dict(args)
        if name in ('execute_bash', 'start_job') and 'command' in norm_args:
            norm_args['command'] = normalize_command(str(norm_args['command']))

        signature = hashlib.sha256(json.dumps([norm_name, norm_args], sort_keys=True).encode()).hexdigest()
        history = [x for x in self.state.get('recent_actions', []) if x.get('at', 0) > time.time() - 900][-24:]
        matching = [x for x in history if x.get('signature') == signature]
        limit = 1 if name == 'board_message' else 2
        if name != 'idle' and len(matching) >= limit:
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
            self.feedback('invalid_decision', parsed['fallback_reason']+'. No action executed. Correct your envelope: {"name":"tool_name","arguments":{...}}. Your rejected final text: '+preview, False)
            self.event('invalid_decision', parsed['fallback_reason'])
            return
        self.state['invalid_streak'] = 0
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
                message = f'to={recipient}; reply_to={str(args.get("reply_to", ""))[:120]}; message={args["message"][:4000]}'
                self.event(name,message)
                if hasattr(self.tasks, 'store'):
                    self.tasks.store.post_inbox_message(
                        source='direct' if recipient != 'ALL' else 'board',
                        sender=self.id,
                        recipient=recipient if recipient != 'ALL' else None,
                        reply_to=args.get("reply_to"),
                        content=args["message"][:4000],
                    )
                self.feedback(name,'Message posted. A reply is not guaranteed; continue independent work.',True)
            elif name == 'task_operation':
                result=self.tasks.operate(self.id,args)
                self.event('task_result',json.dumps(result)); self.feedback(name,json.dumps(result),True)
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
                self.event('memory_result',f'result=success; action={name}; id={result.get("id", "")}; matches={len(result.get("items",[]))}')
                self.feedback(name,json.dumps(result),True)
            elif name == 'research_request':
                result = self.research.search(args.get('source'), args.get('query'), args.get('limit', 5))
                self.event('research_result', json.dumps({k: result.get(k) for k in ('source', 'query', 'sha256', 'results')}, ensure_ascii=False))
                self.feedback(name, json.dumps(result, ensure_ascii=False), True)
            elif name == 'meeting_operation':
                op = args.get('operation') or args.get('action')
                if op == 'report':
                    result = self.meetings.report(args['meeting_id'], self.id, args.get('achieved',''), args.get('evidence',''), args.get('next_step',''), args.get('blockers',''))
                elif op == 'close':
                    result = self.meetings.close(args['meeting_id'])
                else:
                    raise ValueError('meeting_operation requires report or close')
                self.event('meeting_result', json.dumps(result, ensure_ascii=False)); self.feedback(name, json.dumps(result, ensure_ascii=False), True)
            else:
                self.feedback('idle','Intentional rest; next turn may resume your own project.',True)
                self.event('idle','intentional rest')
        except (OSError, ValueError) as exc:
            self.feedback(name,str(exc),False)
            self.event('action_error',f'action={name}; error={exc}')

    def cycle(self):
        # P01/P07: Respect persistent pause marker and drain/abort modes
        if is_paused(self.pause_marker):
            meta = read_pause_metadata(self.pause_marker)
            mode = meta.get('mode', 'drain')
            self.event('cycle_skipped_paused', f'mode={mode}; reason={meta.get("reason", "paused")}', True)
            return

        snapshot = self.snapshot()
        identity = Path(self.env['AGENT_IDENTITY_PROMPT']).read_text()
        prompt = Path('/usr/local/share/ai-village/system-prompt.txt').read_text()
        # Older founding profiles may name a previous model: current environment wins.
        live = f'Current runtime model={self.env["OLLAMA_MODEL"]}, context={self.env.get("OLLAMA_NUM_CTX")}, role={self.role}. These override stale model details in founding identity.'
        messages = [
            {'role': 'system', 'content': prompt + '\n' + identity + '\n' + live},
            {'role': 'user', 'content': snapshot + '\n\nChoose ONE next action, not a sequence or hypothetical result. For tools, return one complete named action envelope and stop. Ordinary conversation may be prose.'}
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
            parsed = decision(answer)
            if not parsed.get('fallback_reason'):
                self.state['seen_board_epoch'] = self.pending_cursor
                self.state['seen_organic_epoch'] = self.pending_organic_cursor
                # P09: Explicitly acknowledge messages delivered in this successful turn
                if getattr(self, 'delivered_inbox_ids', None) and hasattr(self.tasks, 'store'):
                    self.tasks.store.acknowledge_messages(self.id, self.delivered_inbox_ids)
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
                err_class, err_detail = classify_error(exc)
                self.tracker.fail_request(
                    req_record.request_id,
                    duration_ms=elapsed_ms,
                    error_class=err_class,
                    error_detail=err_detail,
                )
                detail = exc.read(2048).decode(errors='replace') if isinstance(exc, urllib.error.HTTPError) else str(exc)
                self.feedback('inference_error', detail, False)
                self.event('inference_error', f'request_id={req_record.request_id} error_class={err_class} detail={detail}', True)
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
                self.cycle()
                delay = self.compute_cycle_delay()
                # P12: Event-driven wakeup: sleep in short intervals up to delay, waking early on
                # new unacknowledged inbox messages or active job completion
                slept = 0.0
                had_active_job = bool(self.jobs.get_active_job(self.id))
                while slept < delay and not self.stopping:
                    if is_paused(self.pause_marker):
                        break
                    interval = min(1.0, max(0.1, delay - slept))
                    time.sleep(interval)
                    slept += interval
                    # Wakeup check: inbox message arrival
                    if hasattr(self.tasks, 'store'):
                        unacked = self.tasks.store.fetch_unacknowledged_messages(self.id, limit=1)
                        if unacked:
                            self.event('event_wakeup', f'Waking early from sleep: unacknowledged message {unacked[0]["id"]} received', True)
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

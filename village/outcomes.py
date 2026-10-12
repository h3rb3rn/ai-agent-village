"""P151: conversation-to-outcome - which conversations lead to actions, task changes and verified results.

Operator analysis 2026-10-12 (AV-OBS-01): message counts are no measure of progress. This module is
a passive, offline research evaluation for the operator. It is never imported by the runtime, never
written into a resident's context, the board or memory, and opens every database read-only.

Links are only counted as belonging together when an explicit id connects them:
  reply         message.reply_to names an existing message
  task_ref      the text names the id of an existing task
  artifact_ref  the text names the id of an existing artifact
A later task revision or verification of a referenced task/artifact is an outcome of that reference.
That is an explicit id chain plus time order - not proof that the message caused it.
Only time-based connections (the recipient acknowledged the message and acted shortly afterwards)
are reported separately as candidate links. Everything else is 'unknown'.

The message classes (ack_echo, promise, repeat, progress, unknown) are keyword heuristics. Their
precision is measured on an annotated synthetic fixture (tests/fixtures/outcomes_annotated.json);
they are no diagnosis of behaviour.
"""
import argparse
import base64
import json
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

SCHEMA = 'village-conversation-outcomes/1'
PUBLIC_SOURCES = ('direct', 'board')
CLASSES = ('ack_echo', 'promise', 'repeat', 'progress', 'unknown')
LOOP_CLASSES = ('ack_echo', 'promise', 'repeat')
OUTCOMES = ('verified', 'task_activity', 'candidate_action', 'none', 'unknown')
OUTCOME_WINDOW = timedelta(hours=48)
CANDIDATE_WINDOW = timedelta(minutes=15)
REPEAT_WINDOW = timedelta(hours=24)
REPEAT_SIMILARITY = 0.7
LOOP_MIN = 4
# Events that are the result of a resident's own action (messaging itself is not an outcome).
ACTION_EVENT = re.compile(r'^(command_result|[a-z]+_result|calendar_created|calendar_responded|school_answered|gazette_reviewed)$')
SECRET = re.compile(r'(?i)(token|password|passwd|api[_-]?key|secret|authorization|cookie)(\s*[=:]\s*)[^\s;,]+')

HEX_ID = re.compile(r'\b[0-9a-f]{12}\b')
NUMBER = re.compile(r'\d+(?:[.,]\d+)?')
WORD = re.compile(r'[a-zäöüß]{3,}')
EVIDENCE = re.compile(
    r'\d+(?:[.,]\d+)?\s?(?:%|ti?b|gi?b|mi?b|kb|ms|s\b|sec|min|mib|gib)|exit(?:_| )?code|sha256|\b[0-9a-f]{12,}\b'
    r'|(?:^|\s)/[\w.-]+/[\w./-]+|```|\b\w+=\S+|returned|output(?:ted)?:|error:|traceback', re.I)
PROMISE = re.compile(r"\b(i'll|i will|i'm going to|i am going to|will (?:review|report|share|run|test|verify|execute|check|send|post|finalize|submit|look)|going to|plan to|shortly|as soon as)\b", re.I)
RESULT = re.compile(
    r"\b(ran|executed|returned|output(?:s|ted)?|exit(?:ed)?|matched|verified|measured|shows?|showed|completed|created|"
    r"updated|fixed|wrote|logged|found|failed|succeeded|confirmed that|reports?:|result(?:s|ed)?:?|is now|are now)\b", re.I)
REQUEST = re.compile(r"\?|\b(please|could you|can you|would you|need you|let me know)\b", re.I)
ACK = re.compile(r'\b(thanks|thank you|agreed?|sounds good|confirmed?|acknowledged?|got it|understood|great|perfect|noted|ok(?:ay)?|will review|looking forward|appreciate)\b', re.I)
MEASUREMENT = re.compile(
    r'(?P<mount>/mnt/[\w-]+|/var/lib/ai-village)\b[^\n.;]{0,80}?(?P<value>\d+(?:[.,]\d+)?)\s?(?P<unit>TiB|GiB|MiB|TB|GB|MB)\b', re.I)
UNIT_BYTES = {'tib': 1024 ** 4, 'gib': 1024 ** 3, 'mib': 1024 ** 2, 'tb': 1000 ** 4, 'gb': 1000 ** 3, 'mb': 1000 ** 2}
PROVENANCE_KEY = ('host_id', 'mount_namespace', 'device', 'mountpoint')


# ---------------------------------------------------------------------------- time

def utc(ts: Any) -> Tuple[Optional[datetime], bool]:
    """Aware UTC datetime and whether the input lacked a zone (then it is assumed to be UTC)."""
    try:
        value = datetime.fromisoformat(str(ts).strip().replace('Z', '+00:00'))
    except ValueError:
        return None, False
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc), True
    return value.astimezone(timezone.utc), False


def iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat().replace('+00:00', 'Z') if value else None


def redact(text: str) -> str:
    return SECRET.sub(r'\1\2<redacted>', text or '')


# ---------------------------------------------------------------------------- sources

def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(path: Optional[Path], query: str) -> Tuple[List[Dict[str, Any]], str]:
    if not path:
        return [], 'missing'
    if not Path(path).is_file():
        return [], 'missing'
    try:
        conn = _connect(path)
    except sqlite3.Error:
        return [], 'error'
    try:
        return [dict(r) for r in conn.execute(query).fetchall()], 'available'
    except sqlite3.Error:
        return [], 'error'
    finally:
        conn.close()


def load_databases(coordination: Optional[Path] = None, artifacts: Optional[Path] = None) -> Dict[str, Any]:
    """Messages, receipts, tasks, task revisions, artifacts and verifications, each with its status."""
    data: Dict[str, Any] = {'sources': {}}
    queries = {
        'messages': (coordination, 'SELECT id, source, sender, recipient, reply_to, content, timestamp FROM inbox_messages '
                                   "WHERE source IN ('direct', 'board')"),
        'receipts': (coordination, 'SELECT message_id, agent_id, acknowledged, acknowledged_at FROM message_receipts'),
        'tasks': (coordination, 'SELECT id, author, owner, status, blockers, completion_verified, created_at, updated_at FROM tasks'),
        'revisions': (coordination, 'SELECT task_id, revision, actor, action, status, timestamp FROM task_revisions'),
        'artifacts': (artifacts, 'SELECT artifact_id, owner, task_id, status, created_at FROM artifacts'),
        'verifications': (artifacts, 'SELECT artifact_id, verifier, passed, timestamp FROM artifact_verifications'),
    }
    for key, (path, query) in queries.items():
        data[key], data['sources'][key] = _rows(path, query)
    return data


def _messages_from_json(doc: Any) -> List[Dict[str, Any]]:
    if isinstance(doc, dict) and isinstance(doc.get('messages'), list):
        return [m for m in doc['messages'] if isinstance(m, dict)]
    if isinstance(doc, list):
        return [m for m in doc if isinstance(m, dict) and m.get('id')]
    if isinstance(doc, dict) and doc.get('id') and 'content' in doc:
        return [doc]
    return []


def _har_messages(har: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Message objects from archive-API responses in a browser HAR. Only response bodies of
    /api/conversations/messages are read - request headers, cookies and all other entries never are."""
    out = []
    for entry in (har.get('log') or {}).get('entries') or []:
        url = str(((entry or {}).get('request') or {}).get('url') or '')
        if '/api/conversations/messages' not in url:
            continue
        content = ((entry.get('response') or {}).get('content') or {})
        text = content.get('text') or ''
        if content.get('encoding') == 'base64':
            try:
                text = base64.b64decode(text).decode('utf-8')
            except (ValueError, UnicodeDecodeError):
                continue
        try:
            out.extend(_messages_from_json(json.loads(text)))
        except ValueError:
            continue
    return out


def load_exports(paths: Iterable[Path]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Messages from exports: HAR files, archive-API pages, JSON lists or JSON lines.
    Overlapping pages are deduplicated by message id."""
    seen: Dict[str, Dict[str, Any]] = {}
    read = 0
    for path in paths:
        text = Path(path).read_text(encoding='utf-8')
        try:
            doc = json.loads(text)
            items = _har_messages(doc) if isinstance(doc, dict) and 'log' in doc else _messages_from_json(doc)
        except ValueError:
            items = []
            for line in text.splitlines():
                try:
                    items.extend(_messages_from_json(json.loads(line)))
                except ValueError:
                    continue
        for item in items:
            read += 1
            seen.setdefault(str(item['id']), item)
    messages = list(seen.values())
    return messages, {'read': read, 'unique': len(messages), 'duplicates': read - len(messages)}


def load_events(path: Optional[Path]) -> Tuple[List[Dict[str, Any]], str]:
    if not path or not Path(path).is_file():
        return [], 'missing'
    out = []
    try:
        with Path(path).open(encoding='utf-8') as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and ACTION_EVENT.match(str(row.get('event', ''))):
                    out.append({'event_id': row.get('event_id'), 'agent': row.get('agent'),
                                'event': row.get('event'), 'timestamp': row.get('timestamp')})
    except OSError:
        return [], 'error'
    return out, 'available'


# ---------------------------------------------------------------------------- classification

def _words(text: str) -> set:
    return set(WORD.findall(HEX_ID.sub(' ', (text or '').lower())))


def similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def message_class(text: str, earlier: Iterable[str] = ()) -> str:
    """Heuristic class of one message. 'earlier' are recent texts of the same sender to the same
    recipient: a paraphrase of one of them with the same numbers is a repeat; with changed numbers
    (a periodic measurement with a new value) it is new information."""
    numbers = set(NUMBER.findall(text or ''))
    for prior in earlier:
        if similarity(text, prior) >= REPEAT_SIMILARITY and numbers == set(NUMBER.findall(prior)):
            return 'repeat'
    if EVIDENCE.search(text or '') and RESULT.search(text or ''):
        return 'progress'
    if PROMISE.search(text or ''):
        return 'promise'
    if ACK.search(text or '') and not REQUEST.search(text or '') and len((text or '').split()) <= 60:
        return 'ack_echo'
    return 'unknown'


def evaluate_classifier(labelled: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Precision and recall per class on annotated messages ({text, earlier, label})."""
    tp: Counter = Counter()
    fp: Counter = Counter()
    fn: Counter = Counter()
    for item in labelled:
        got = message_class(item['text'], item.get('earlier') or ())
        want = item['label']
        if got == want:
            tp[got] += 1
        else:
            fp[got] += 1
            fn[want] += 1
    out = {}
    for cls in CLASSES:
        p_den, r_den = tp[cls] + fp[cls], tp[cls] + fn[cls]
        out[cls] = {'precision': round(tp[cls] / p_den, 3) if p_den else None, 'predicted': p_den,
                    'recall': round(tp[cls] / r_den, 3) if r_den else None, 'annotated': r_den}
    return out


# ---------------------------------------------------------------------------- measurement claims

def measurement_claims(messages: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Disk sizes named in message text. Messages never carry host, mount namespace or device,
    so every such claim is unverified and not comparable with another one."""
    out = []
    for m in messages:
        for hit in MEASUREMENT.finditer(m.get('content') or ''):
            unit = hit.group('unit')
            value = float(hit.group('value').replace(',', '.'))
            out.append({'message_id': m['id'], 'sender': m['sender'], 'timestamp': m['timestamp'],
                        'mountpoint': hit.group('mount'), 'value': value, 'unit': unit,
                        'bytes': int(value * UNIT_BYTES[unit.lower()]), 'status': 'unverified_claim'})
    return out


def compare_measurements(measurements: List[Dict[str, Any]], window: timedelta = timedelta(hours=1),
                         tolerance: float = 0.05) -> List[Dict[str, Any]]:
    """Pairs of measurements that differ. Only measurements of the same host, mount namespace, device
    and mountpoint taken close in time can contradict each other; anything else is 'not_comparable'."""
    out = []
    for i, a in enumerate(measurements):
        for b in measurements[i + 1:]:
            if a.get('mountpoint') != b.get('mountpoint') or not a.get('bytes') or not b.get('bytes'):
                continue
            if abs(a['bytes'] - b['bytes']) <= tolerance * max(a['bytes'], b['bytes']):
                continue
            ta, tb = utc(a.get('timestamp'))[0], utc(b.get('timestamp'))[0]
            complete = all(a.get(k) and b.get(k) for k in PROVENANCE_KEY)
            same = complete and all(a[k] == b[k] for k in PROVENANCE_KEY)
            close = ta is not None and tb is not None and abs(ta - tb) <= window
            status = 'contradiction_candidate' if same and close else 'not_comparable'
            reason = None if status != 'not_comparable' else (
                'missing_provenance' if not complete else 'different_source' if not same else 'different_time')
            out.append({'a': a.get('message_id') or a.get('id'), 'b': b.get('message_id') or b.get('id'),
                        'mountpoint': a['mountpoint'], 'status': status, 'reason': reason})
    return out


# ---------------------------------------------------------------------------- analysis

def _prepare(messages: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    out, naive = [], 0
    for m in messages:
        if m.get('source') not in PUBLIC_SOURCES:
            continue  # signals from outside are written by humans
        when, was_naive = utc(m.get('timestamp'))
        if when is None:
            continue
        naive += was_naive
        recipient = m.get('recipient') if m.get('source') == 'direct' and m.get('recipient') else 'ALL'
        out.append({'id': str(m['id']), 'source': m['source'], 'sender': m.get('sender') or '?', 'recipient': recipient,
                    'reply_to': m.get('reply_to') or None, 'content': redact(m.get('content') or ''), 'when': when,
                    'timestamp': iso(when)})
    out.sort(key=lambda x: (x['when'], x['id']))
    return out, naive


def _rate(num: int, den: int, per: int = 1) -> Dict[str, Any]:
    return {'value': round(per * num / den, 3) if den else None, 'numerator': num, 'denominator': den}


def _median_hours(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 2) if values else None


def analyse(messages: Iterable[Dict[str, Any]], data: Optional[Dict[str, Any]] = None,
            events: Optional[List[Dict[str, Any]]] = None, events_status: str = 'missing',
            now: Optional[datetime] = None, since: Optional[datetime] = None,
            until: Optional[datetime] = None) -> Dict[str, Any]:
    data = data or {'sources': {}}
    now = now or datetime.now(timezone.utc)
    prepared, naive = _prepare(messages)
    if since:
        prepared = [m for m in prepared if m['when'] >= since]
    if until:
        prepared = [m for m in prepared if m['when'] < until]
    by_id = {m['id']: m for m in prepared}
    tasks = {t['id']: t for t in data.get('tasks') or []}
    artifacts = {a['artifact_id']: a for a in data.get('artifacts') or []}

    revisions = defaultdict(list)
    for r in data.get('revisions') or []:
        when = utc(r.get('timestamp'))[0]
        if when:
            revisions[r['task_id']].append((when, r))
    verifications = defaultdict(list)  # artifact id -> independent, passed verifications
    for v in data.get('verifications') or []:
        art = artifacts.get(v['artifact_id'])
        when = utc(v.get('timestamp'))[0]
        if art and when and v.get('passed') and v.get('verifier') != art.get('owner'):
            verifications[v['artifact_id']].append((when, v))
    task_artifacts = defaultdict(list)
    for a in artifacts.values():
        if a.get('task_id'):
            task_artifacts[a['task_id']].append(a['artifact_id'])
    acks = {}
    for r in data.get('receipts') or []:
        when = utc(r.get('acknowledged_at'))[0] if r.get('acknowledged') else None
        if when:
            acks[(r['message_id'], r['agent_id'])] = when
    actions = defaultdict(list)
    for e in events or []:
        when = utc(e.get('timestamp'))[0]
        if when and e.get('agent'):
            actions[e['agent']].append((when, e))
    for agent in actions:
        actions[agent].sort(key=lambda x: x[0])

    history: Dict[Tuple[str, str], deque] = defaultdict(lambda: deque(maxlen=20))
    results = []
    first_ref: Dict[str, datetime] = {}
    reuse: Dict[str, set] = defaultdict(set)
    for m in prepared:
        pair = (m['sender'], m['recipient'])
        recent = [t for when, t in history[pair] if m['when'] - when <= REPEAT_WINDOW]
        cls = message_class(m['content'], recent)
        history[pair].append((m['when'], m['content']))
        links, outcome = [], 'unknown'
        if m['reply_to'] and m['reply_to'] in by_id:
            links.append({'kind': 'reply', 'id': m['reply_to']})
        ids = set(HEX_ID.findall(m['content']))
        level = 0  # 0 none, 1 task activity, 2 verified
        end = m['when'] + OUTCOME_WINDOW
        for tid in sorted(ids & set(tasks)):
            links.append({'kind': 'task_ref', 'id': tid})
            first_ref.setdefault(tid, m['when'])
            if any(m['when'] < when <= end for when, _ in revisions.get(tid, ())):
                level = max(level, 1)
            arts = task_artifacts.get(tid, ())
            if any(m['when'] < when <= end for a in arts for when, _ in verifications.get(a, ())):
                level = 2
        for aid in sorted(ids & set(artifacts)):
            links.append({'kind': 'artifact_ref', 'id': aid})
            first_ref.setdefault(aid, m['when'])
            if m['sender'] != artifacts[aid].get('owner'):
                reuse[aid].add(m['sender'])
            if any(m['when'] < when <= end for when, _ in verifications.get(aid, ())):
                level = 2
        explicit = any(link['kind'] != 'reply' for link in links)
        if explicit:
            outcome = ('none', 'task_activity', 'verified')[level]
        candidate = None
        ack = acks.get((m['id'], m['recipient']))
        if ack and m['recipient'] != 'ALL':
            follow = next((e for when, e in actions.get(m['recipient'], ()) if ack <= when <= ack + CANDIDATE_WINDOW), None)
            if follow:
                candidate = {'kind': 'candidate_link', 'basis': 'ack_then_action', 'event': follow.get('event'),
                             'event_id': follow.get('event_id')}
                if outcome in ('unknown', 'none'):
                    outcome = 'candidate_action'
        results.append({'id': m['id'], 'sender': m['sender'], 'recipient': m['recipient'], 'timestamp': m['timestamp'],
                        'class': cls, 'links': links, 'candidate': candidate, 'outcome': outcome})

    for aid, items in verifications.items():
        reuse[aid].update(v['verifier'] for _, v in items)

    return {'messages': results, 'naive_timestamps': naive, 'first_ref': first_ref, 'reuse': reuse,
            'tasks': tasks, 'artifacts': artifacts, 'revisions': revisions, 'verifications': verifications,
            'task_artifacts': task_artifacts, 'prepared': prepared, 'now': now,
            'sources': dict(data.get('sources') or {}, events=events_status),
            'rows': {k: len(data.get(k) or []) for k in ('messages', 'receipts', 'tasks', 'revisions', 'artifacts', 'verifications')}
            | {'events': len(events or [])}}


def loops(results: List[Dict[str, Any]], min_length: int = LOOP_MIN) -> List[Dict[str, Any]]:
    """Runs of at least min_length consecutive acknowledgements, promises or repeats in one
    conversation pair of residents (both directions) with no progress message and no outcome in between."""
    by_pair = defaultdict(list)
    for r in results:
        if r['recipient'] == 'ALL' or r['sender'].startswith('village-') or r['recipient'].startswith('village-'):
            continue  # board posts and system feedback (auditor, council) are no conversation
        by_pair[tuple(sorted((r['sender'], r['recipient'])))].append(r)
    out = []
    for pair, items in by_pair.items():
        run: List[Dict[str, Any]] = []
        for r in items + [None]:
            if r is not None and r['class'] in LOOP_CLASSES and r['outcome'] in ('unknown', 'none'):
                run.append(r)
                continue
            if len(run) >= min_length:
                out.append({'pair': list(pair), 'length': len(run), 'start': run[0]['timestamp'], 'end': run[-1]['timestamp'],
                            'classes': dict(Counter(x['class'] for x in run)), 'message_ids': [x['id'] for x in run]})
            run = []
    out.sort(key=lambda x: -x['length'])
    return out


def _reply_depths(prepared: List[Dict[str, Any]]) -> List[int]:
    parent = {m['id']: m['reply_to'] for m in prepared}
    depths = []
    for mid in parent:
        depth, cur, seen = 0, parent[mid], {mid}
        while cur and cur in parent and cur not in seen:
            seen.add(cur)
            depth += 1
            cur = parent[cur]
        depths.append(depth)
    return depths


def _figures(rows: List[Dict[str, Any]], with_events: bool) -> Dict[str, Any]:
    n = len(rows)
    cls = Counter(r['class'] for r in rows)
    out = Counter(r['outcome'] for r in rows)
    explicit = sum(1 for r in rows if any(link['kind'] != 'reply' for link in r['links']))
    return {
        'messages_sent': n,
        'ack_only_rate': _rate(cls['ack_echo'], n),
        'semantic_repeat_rate': _rate(cls['repeat'], n),
        'promise_rate': _rate(cls['promise'], n),
        'progress_rate': _rate(cls['progress'], n),
        'explicit_link_rate': _rate(explicit, n),
        'task_activity_per_100_messages': _rate(out['task_activity'] + out['verified'], n, 100),
        'verified_outcomes_per_100_messages': _rate(out['verified'], n, 100),
        'actions_per_100_messages': _rate(sum(1 for r in rows if r['candidate']), n, 100) if with_events
        else {'value': None, 'reason': 'events_not_available'},
    }


def build_report(analysis: Dict[str, Any], exports: Optional[Dict[str, int]] = None,
                 since: Optional[datetime] = None, until: Optional[datetime] = None) -> Dict[str, Any]:
    results = analysis['messages']
    prepared = analysis['prepared']
    now = analysis['now']
    with_events = analysis['sources'].get('events') == 'available'
    sources = analysis['sources']
    warnings = []
    if analysis['naive_timestamps']:
        warnings.append(f"{analysis['naive_timestamps']} timestamps without a time zone were read as UTC")
    for key in ('tasks', 'revisions', 'artifacts', 'verifications', 'receipts', 'events'):
        if sources.get(key) != 'available':
            warnings.append(f'source {key} is {sources.get(key, "missing")}: links that need it are reported as unknown')
    if exports:
        warnings.append(f"export input: {exports['read']} message objects, {exports['unique']} unique, "
                        f"{exports['duplicates']} duplicates from overlapping pages removed; an export is not the full corpus")

    per_agent, per_pair = defaultdict(list), defaultdict(list)
    for r in results:
        per_agent[r['sender']].append(r)
        if r['recipient'] != 'ALL':
            per_pair[' <-> '.join(sorted((r['sender'], r['recipient'])))].append(r)

    # Task- and artifact-level figures (latency, blockers, reuse).
    latencies = []
    for ref, first in analysis['first_ref'].items():
        times = [w for w, _ in analysis['verifications'].get(ref, ())]
        for aid in analysis['task_artifacts'].get(ref, ()):
            times += [w for w, _ in analysis['verifications'].get(aid, ())]
        task = analysis['tasks'].get(ref)
        if task and task.get('completion_verified'):
            done = utc(task.get('updated_at'))[0]
            if done:
                times.append(done)
        later = [t for t in times if t > first]
        if later:
            latencies.append((min(later) - first).total_seconds() / 3600)
    blocker_ages = []
    for t in analysis['tasks'].values():
        if t.get('status') in ('open', 'active') and (t.get('blockers') or '').strip():
            updated = utc(t.get('updated_at'))[0]
            if updated:
                blocker_ages.append((now - updated).total_seconds() / 3600)
    reuse = {aid: sorted(agents) for aid, agents in analysis['reuse'].items() if agents}
    depths = _reply_depths(prepared)
    coverage = Counter()
    for r in results:
        explicit = any(link['kind'] != 'reply' for link in r['links'])
        if explicit and r['outcome'] in ('task_activity', 'verified'):
            coverage['fully_linked'] += 1
        elif explicit:
            coverage['explicit_only'] += 1
        elif r['candidate']:
            coverage['candidate_only'] += 1
        else:
            coverage['unlinked'] += 1
    claims = measurement_claims(prepared)

    return {
        'schema_version': SCHEMA,
        'generated_at_utc': iso(now),
        'window': {'start_utc': iso(since) or (prepared[0]['timestamp'] if prepared else None),
                   'end_utc': iso(until) or (prepared[-1]['timestamp'] if prepared else None)},
        'sources': [{'kind': k, 'status': v, 'rows': analysis['rows'].get(k)} for k, v in sorted(sources.items())]
        + ([{'kind': 'export', 'status': 'available'}] if exports else []),
        'metrics': dict(_figures(results, with_events),
                        reply_depth={'max': max(depths) if depths else 0,
                                     'mean': round(sum(depths) / len(depths), 3) if depths else None},
                        claim_to_verify_latency_hours={'median': _median_hours(latencies), 'count': len(latencies),
                                                       'denominator': len(analysis['first_ref'])},
                        unresolved_blocker_age_hours={'median': _median_hours(blocker_ages), 'count': len(blocker_ages),
                                                      'max': round(max(blocker_ages), 2) if blocker_ages else None},
                        artifact_reuse_count={'artifacts': len(reuse), 'agents': sum(len(v) for v in reuse.values())}),
        'evidence_coverage': {k: coverage[k] for k in ('fully_linked', 'explicit_only', 'candidate_only', 'unlinked')},
        'agents': {agent: _figures(rows, with_events) for agent, rows in sorted(per_agent.items())},
        'pairs': {pair: _figures(rows, with_events) for pair, rows in sorted(per_pair.items(), key=lambda x: -len(x[1]))},
        'loops': loops(results),
        'measurement_claims': {'claims': len(claims), 'by_mountpoint': dict(Counter(c['mountpoint'] for c in claims)),
                               'comparisons': dict(Counter(c['status'] for c in compare_measurements(claims)))},
        'interventions': [],
        'heuristic_fields': ['ack_only_rate', 'semantic_repeat_rate', 'promise_rate', 'progress_rate', 'loops'],
        'warnings': warnings,
    }


def _fmt(metric: Dict[str, Any]) -> str:
    if metric.get('value') is None:
        return f"– ({metric.get('reason', 'no data')})"
    return f"{metric['value']} ({metric['numerator']}/{metric['denominator']})"


def to_markdown(report: Dict[str, Any], top: int = 10) -> str:
    m = report['metrics']
    lines = [f"# Conversation-to-Outcome ({report['schema_version']})", '',
             f"Generated {report['generated_at_utc']} · window {report['window']['start_utc']} – {report['window']['end_utc']}", '',
             'Sources: ' + ', '.join(f"{s['kind']}={s['status']}" + (f" ({s['rows']} rows)" if s.get('rows') is not None else '')
                                    for s in report['sources']), '',
             '## Village', '', '| Metric | Value |', '|---|---|']
    for key in ('messages_sent', 'explicit_link_rate', 'task_activity_per_100_messages', 'verified_outcomes_per_100_messages',
                'actions_per_100_messages', 'ack_only_rate', 'promise_rate', 'semantic_repeat_rate', 'progress_rate'):
        value = m[key]
        lines.append(f"| {key} | {value if isinstance(value, int) else _fmt(value)} |")
    lines += [f"| reply_depth | max {m['reply_depth']['max']}, mean {m['reply_depth']['mean']} |",
              f"| claim_to_verify_latency_hours | median {m['claim_to_verify_latency_hours']['median']} "
              f"({m['claim_to_verify_latency_hours']['count']}/{m['claim_to_verify_latency_hours']['denominator']} references verified) |",
              f"| unresolved_blocker_age_hours | median {m['unresolved_blocker_age_hours']['median']}, max {m['unresolved_blocker_age_hours']['max']} "
              f"({m['unresolved_blocker_age_hours']['count']} tasks) |",
              f"| artifact_reuse_count | {m['artifact_reuse_count']['artifacts']} artifacts, {m['artifact_reuse_count']['agents']} non-owner agents |",
              '', '## Evidence coverage', '']
    lines += [f"- {k}: {v}" for k, v in report['evidence_coverage'].items()]
    lines += ['', '## Residents', '', '| Resident | Sent | Explicit links | Task activity /100 | Verified /100 | Ack | Promise | Repeat |',
              '|---|---|---|---|---|---|---|---|']
    for agent, f in report['agents'].items():
        lines.append(f"| {agent} | {f['messages_sent']} | {_fmt(f['explicit_link_rate'])} | {_fmt(f['task_activity_per_100_messages'])} | "
                     f"{_fmt(f['verified_outcomes_per_100_messages'])} | {_fmt(f['ack_only_rate'])} | {_fmt(f['promise_rate'])} | "
                     f"{_fmt(f['semantic_repeat_rate'])} |")
    lines += ['', f'## Top {top} pairs', '', '| Pair | Messages | Explicit links | Verified /100 | Ack | Repeat |', '|---|---|---|---|---|---|']
    for pair, f in list(report['pairs'].items())[:top]:
        lines.append(f"| {pair} | {f['messages_sent']} | {_fmt(f['explicit_link_rate'])} | {_fmt(f['verified_outcomes_per_100_messages'])} | "
                     f"{_fmt(f['ack_only_rate'])} | {_fmt(f['semantic_repeat_rate'])} |")
    lines += ['', f'## Longest loop candidates (heuristic, ≥{LOOP_MIN} messages without progress or outcome)', '']
    for loop in report['loops'][:top]:
        lines.append(f"- {' ↔ '.join(loop['pair'])}: {loop['length']} messages, {loop['start']} – {loop['end']}, "
                     f"{loop['classes']}, first {loop['message_ids'][0]}")
    if not report['loops']:
        lines.append('- none')
    mc = report['measurement_claims']
    lines += ['', '## Measurement claims in messages', '',
              f"{mc['claims']} disk-size claims ({mc['by_mountpoint']}); comparisons: {mc['comparisons'] or 'none'}. "
              'Messages carry no host, mount namespace or device, so differing values are not comparable.', '',
              '## Warnings', '']
    lines += [f'- {w}' for w in report['warnings']] or ['- none']
    lines += ['', 'Classes (ack, promise, repeat, progress) and loops are keyword heuristics, not diagnoses. '
              'An outcome after an explicit reference is time order plus id, not proof of cause.', '']
    return '\n'.join(lines)


def run(coordination: Optional[Path] = None, artifacts: Optional[Path] = None, events: Optional[Path] = None,
        exports: Iterable[Path] = (), now: Optional[datetime] = None, since: Optional[datetime] = None,
        until: Optional[datetime] = None) -> Dict[str, Any]:
    data = load_databases(coordination, artifacts)
    export_stats = None
    messages = data['messages']
    exports = list(exports)
    if exports:
        messages, export_stats = load_exports(exports)
        for m in messages:
            m.setdefault('source', 'board' if not m.get('recipient') or m.get('recipient') == 'ALL' else 'direct')
        data['sources']['messages'] = 'export'
    event_rows, event_status = load_events(events)
    analysis = analyse(messages, data, event_rows, event_status, now=now, since=since, until=until)
    return build_report(analysis, export_stats, since, until)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description='Passive conversation-to-outcome report (read-only).')
    parser.add_argument('--coordination', type=Path, help='board/coordination.sqlite3 (opened read-only)')
    parser.add_argument('--artifacts', type=Path, help='board/artifacts.sqlite3 (opened read-only)')
    parser.add_argument('--events', type=Path, help='board/events.jsonl')
    parser.add_argument('--export', type=Path, action='append', default=[], help='HAR, archive-API page or JSONL export')
    parser.add_argument('--since', help='ISO time, inclusive')
    parser.add_argument('--until', help='ISO time, exclusive')
    parser.add_argument('--json', type=Path, help='write the JSON report here')
    parser.add_argument('--md', type=Path, help='write the Markdown report here')
    args = parser.parse_args(argv)
    report = run(args.coordination, args.artifacts, args.events, args.export,
                 since=utc(args.since)[0] if args.since else None, until=utc(args.until)[0] if args.until else None)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json:
        args.json.write_text(text + '\n', encoding='utf-8')
    if args.md:
        args.md.write_text(to_markdown(report), encoding='utf-8')
    if not args.json and not args.md:
        sys.stdout.write(to_markdown(report))
    return 0


if __name__ == '__main__':
    sys.exit(main())

"""P156 stage 1: observe the assembled prompt before every inference, change nothing.

Operator (2026-10-13): check and optimise the prompt before it is sent. Stage 1 only measures,
so stage 2 (corrections) can be decided on numbers:
- token estimate per section (system prompt parts and every top-level snapshot key);
- contradictions: a runtime hint names an action that is not offered this cycle;
- stale hints: a section repeated unchanged for many cycles;
- duplicates: the same message id in more than one section.
Peer and memory text is untrusted data and never counted as a hint. The report goes to the
operator telemetry only (telemetry/agent-events.jsonl), never into a resident's own context.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

CHARS_PER_TOKEN = 3  # same conservative estimate as web/runtime.py CONTEXT_CHARS_PER_TOKEN
STALE_AFTER = 5  # cycles a section may stay unchanged before it counts as stale
TOP_SECTIONS = 8
# Sections that carry peer, memory or catalogue text, not runtime guidance.
NOT_HINTS = ('untrusted_', 'recent_organic', 'retrieved_memory', 'tools', 'peers', 'groups', 'projects', 'teams',
             'artifacts', 'own_recent_results', 'measured_resources', 'token_budget', 'your_notes', 'runtime',
             'identity', 'private_work_directory')
_ID = re.compile(r"\bmsg_[0-9a-f_]{6,}\b")
# P158: sections that mix facts with a demand; only the demand counts as a hint. The last
# action's result and which action failed are facts, not requests (None = facts only).
DEMAND_FIELDS = {'last_action_feedback': None, 'recovery_guidance': ('directive',),
                 'your_repeated_mistakes': ('last_solution',), 'your_model_gaps': ('hint',)}


def demand_text(key: str, value: Any) -> str:
    if key not in DEMAND_FIELDS:
        return _text(value)
    fields = DEMAND_FIELDS[key] or ()
    rows = value if isinstance(value, list) else [value]
    return ' '.join(str(r.get(f, '')) for r in rows if isinstance(r, dict) for f in fields)


def tokens(text: str) -> int:
    return -(-len(text) // CHARS_PER_TOKEN)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _ids(value: Any) -> set:
    found = set()
    if isinstance(value, dict):
        if isinstance(value.get('id'), str):
            found.add(value['id'])
        for v in value.values():
            found |= _ids(v)
    elif isinstance(value, list):
        for v in value:
            found |= _ids(v)
    elif isinstance(value, str):
        found |= set(_ID.findall(value))
    return found


def is_hint(key: str) -> bool:
    return not key.startswith(NOT_HINTS)


def inspect(system_parts: Mapping[str, str], snapshot: str, allowed: Iterable[str], actions: Iterable[str],
            previous: Optional[Dict[str, List[Any]]] = None) -> Tuple[Dict[str, Any], Dict[str, List[Any]]]:
    """Return (report, new_state). `previous` maps section -> [hash, unchanged_cycles]."""
    allowed = set(allowed)
    not_offered = sorted(set(actions) - allowed)
    action_re = re.compile(r"\b(" + "|".join(map(re.escape, not_offered)) + r")\b") if not_offered else None
    previous = previous or {}
    sizes = {f'system.{k}': tokens(v or '') for k, v in system_parts.items()}
    try:
        snap = json.loads(snapshot)
    except (TypeError, ValueError):
        snap = None
    if not isinstance(snap, dict):
        sizes['user'] = tokens(snapshot or '')
        snap = {}
    contradictions, stale, state, owners = [], [], {}, {}
    for key, value in snap.items():
        text = _text(value)
        sizes[f'user.{key}'] = tokens(text)
        if value in (None, '', [], {}, False):
            continue
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]
        old = previous.get(key)
        count = old[1] + 1 if old and old[0] == digest else 1
        state[key] = [digest, count]
        for msg_id in _ids(value):
            owners.setdefault(msg_id, []).append(key)
        if not is_hint(key):
            continue
        if count >= STALE_AFTER:
            stale.append({'section': key, 'cycles': count, 'tokens': tokens(text)})
        if action_re:
            named = sorted(set(action_re.findall(demand_text(key, value))))
            if named:
                contradictions.append({'section': key, 'actions': named})
    duplicates = {i: k for i, k in owners.items() if len(k) > 1}
    total = sum(sizes.values())
    top = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)[:TOP_SECTIONS]
    report = {
        'tokens_total': total,
        'top_sections': [{'section': k, 'tokens': v} for k, v in top],
        'offered': len(allowed),
        'contradictions': contradictions,
        'stale': sorted(stale, key=lambda s: s['tokens'], reverse=True),
        'stale_tokens': sum(s['tokens'] for s in stale),
        'duplicate_ids': len(duplicates),
        'duplicate_sections': sorted({'+'.join(sorted(set(k))) for k in duplicates.values()}),
    }
    return report, state


# ---------------------------------------------------------------------------
# P158 stage 2: correct the snapshot before it is sent.
# Operator (2026-10-13): "Wenn das Problem doch schon bekannt ist musst du nicht weiter Daten
# sammeln". Stage 1 showed live: under the loop breaker, seven hint sections asked for actions
# the same cycle did not offer (06-operator, 3 offered actions), every resident's
# your_repeated_mistakes named meeting_operation while it was hidden, and today's calendar was
# the largest section (3.8k-5.6k tokens). Facts (last_action_feedback, error texts, peer and
# memory text) are never rewritten; every correction is reported for the operator telemetry.
# ---------------------------------------------------------------------------
REPLY_SECTIONS = ('direct_ack_target', 'discussion_target')  # oblige a board_message reply
# Advisory notes: dropped for this cycle when they ask for an action that is not offered;
# they come back on their own once it is offered again.
ADVISORY_NOTES = ('school_lesson', 'notes_hint', 'resident_profile_note', 'task_blocker_guidance',
                  'finetune_daily_note', 'gazette_daily_note', 'calendar_daily_note', 'calendar_week_gaps',
                  'task_templates_note', 'task_ownership_note')
# Recurring advisory notes that are not tied to a gate: once unchanged for STALE_AFTER cycles,
# shown only every REPEAT_EVERY-th cycle (calendar_daily_note stays: it carries the planning gate).
RECURRING_NOTES = ('calendar_week_gaps', 'resident_profile_note', 'notes_hint', 'finetune_daily_note',
                   'task_ownership_note', 'task_templates_note')
REPEAT_EVERY = 5
CALENDAR_UPCOMING = 5
CALENDAR_NOTES_CHARS = 160


def _names(text: str, not_offered: List[str]) -> List[str]:
    if not not_offered:
        return []
    return sorted(set(re.findall(r"\b(" + "|".join(map(re.escape, not_offered)) + r")\b", text)))


def _upcoming(events: List[Any], minute: int) -> Tuple[List[Any], int]:
    """Today's events that have not ended yet, first CALENDAR_UPCOMING of them, slimmed down."""
    def minutes(t: Any) -> int:
        hh, _, mm = str(t or '0:0').partition(':')
        try:
            return int(hh) * 60 + int(mm or 0)
        except ValueError:
            return 0
    live = [e for e in events if isinstance(e, dict)
            and minutes(e.get('start_time')) + int(e.get('duration_minutes') or 0) > minute]
    live.sort(key=lambda e: minutes(e.get('start_time')))
    slim = []
    for e in live[:CALENDAR_UPCOMING]:
        row = {k: e[k] for k in ('id', 'title', 'kind', 'start_time', 'duration_minutes', 'status', 'organizer',
                                 'meeting_id') if e.get(k) not in (None, '')}
        if e.get('notes'):
            row['notes'] = str(e['notes'])[:CALENDAR_NOTES_CHARS]
        others = {a.get('agent_id'): a.get('response') for a in e.get('attendees') or []
                  if isinstance(a, dict) and a.get('agent_id') != e.get('organizer')}
        if others:
            row['attendees'] = others
        slim.append(row)
    return slim, len(events) - len(slim)


def correct(context: Dict[str, Any], allowed: Iterable[str], actions: Iterable[str], minute: int,
            previous: Optional[Dict[str, List[Any]]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, List[Any]]]:
    """Correct `context` in place; return (corrections, new_state).

    `previous` maps a recurring note -> [hash, unchanged_cycles] over the uncorrected text, so a
    note that is hidden still keeps counting instead of starting over."""
    allowed = set(allowed)
    not_offered = sorted(set(actions) - allowed)
    fixes: List[Dict[str, Any]] = []

    def size(value: Any) -> int:
        return tokens(_text(value))

    if 'board_message' not in allowed:
        for key in REPLY_SECTIONS:
            if context.get(key):
                fixes.append({'section': key, 'change': 'dropped', 'actions': ['board_message'],
                              'tokens': size(context[key])})
                context[key] = None
    for key in ADVISORY_NOTES:
        if context.get(key):
            named = _names(_text(context[key]), not_offered)
            if named:
                fixes.append({'section': key, 'change': 'dropped', 'actions': named, 'tokens': size(context[key])})
                del context[key]
    recovery = context.get('recovery_guidance')
    if isinstance(recovery, dict) and recovery.get('failed_action') in not_offered:
        recovery['directive'] = ('The failed action is not offered this cycle; do not retry it now. Choose one of: '
                                 + ', '.join(sorted(allowed)) + '.')
        fixes.append({'section': 'recovery_guidance', 'change': 'directive_rewritten',
                      'actions': [recovery['failed_action']]})
    gaps = context.get('your_model_gaps')
    if isinstance(gaps, dict) and _names(str(gaps.get('hint', '')), not_offered):
        fixes.append({'section': 'your_model_gaps', 'change': 'hint_dropped',
                      'actions': _names(str(gaps['hint']), not_offered), 'tokens': size(gaps['hint'])})
        gaps.pop('hint')
    mistakes = context.get('your_repeated_mistakes')
    if isinstance(mistakes, list):
        rewritten, named_all = [], set()
        for entry in mistakes:
            named = _names(str(entry.get('last_solution', '')), not_offered) if isinstance(entry, dict) else []
            if named:
                entry = dict(entry, last_solution='(its fix needs an action not offered this cycle)')
                named_all.update(named)
            rewritten.append(entry)
        if named_all:
            context['your_repeated_mistakes'] = rewritten
            fixes.append({'section': 'your_repeated_mistakes', 'change': 'solutions_masked',
                          'actions': sorted(named_all)})
    events = context.get('calendar_today_untrusted')
    if isinstance(events, list) and events:
        before = size(events)
        slim, omitted = _upcoming(events, minute)
        if slim:
            context['calendar_today_untrusted'] = slim
        else:
            del context['calendar_today_untrusted']
        if omitted:
            context['calendar_today_omitted'] = (f'{omitted} of today\'s events are past or later than the next '
                                                 f'{CALENDAR_UPCOMING}'
                                                 + ('; calendar_operation list shows all.' if 'calendar_operation' in allowed else '.'))
        if size(slim) < before:
            fixes.append({'section': 'calendar_today_untrusted', 'change': 'upcoming_only', 'kept': len(slim),
                          'omitted': omitted, 'tokens': before - size(slim) - size(context.get('calendar_today_omitted', ''))})
    previous = previous or {}
    state: Dict[str, List[Any]] = {}
    for key in RECURRING_NOTES:
        if not context.get(key):
            continue
        digest = hashlib.sha256(_text(context[key]).encode('utf-8')).hexdigest()[:12]
        old = previous.get(key)
        count = old[1] + 1 if old and old[0] == digest else 1
        state[key] = [digest, count]
        if count >= STALE_AFTER and (count - STALE_AFTER) % REPEAT_EVERY:
            fixes.append({'section': key, 'change': 'stale_skipped', 'cycles': count, 'tokens': size(context[key])})
            del context[key]
    return fixes, state

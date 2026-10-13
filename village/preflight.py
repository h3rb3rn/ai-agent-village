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
            named = sorted(set(action_re.findall(text)))
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

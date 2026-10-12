"""P148: the dream phase - each resident's daily window for processing and tidying its memories.

Operator (2026-10-12): "Die müssen nicht schlafen. [...] Es soll jedoch eine Phase pro Agent
geben der wie 'Träumen' ist bei dem die eigenen Erinnerungen verarbeitet und aufgeräumt werden."

The residents work around the clock (P147). Once a day each one dreams: its own calendar
'dream' event if it planned one, otherwise a default window staggered across the day so
that never all residents dream at once (01 at 00:00, 02 at 02:40, ... 09 at 21:20 UTC).
While dreaming, a resident does no new work. It reviews its own knowledge-base records in
batches, merges duplicates, forgets what is obsolete or refuted, condenses repeated
observations into lessons, updates its notes for when it wakes, and reports skill gaps
seen in the day's failures. Forgotten records are archived privately in its home first,
so nothing is lost. An open meeting still takes precedence over the dream.
This is the reflection step of Generative Agents (Park et al. 2023) and the AI Village
lesson that undigested lessons are rediscovered instead of reused.
"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

DREAM_MINUTES_DEFAULT = 90
DREAM_STAGGER_MINUTES = 160
DREAM_BATCH = 8
DUPLICATE_JACCARD = 0.6

DREAM_ACTIONS = ('memory_search', 'memory_get', 'memory_update', 'memory_forget', 'memory_remember',
                 'notes_operation', 'capability_operation', 'idle')
DREAM_ONLY_ACTIONS = ('memory_update', 'memory_forget')

DREAM_INSTRUCTIONS = (
    'You are dreaming: no new work, no messages. Process your own memories in memories_to_review. '
    'Merge duplicates: memory_update the better record with the combined content, then memory_forget the '
    'others. memory_forget what is obsolete or refuted (give a reason). Lower the confidence of claims you '
    'could not verify. Turn repeated observations into one lesson with memory_remember kind=lesson. Update '
    'your notes (focus, next_step) for when you wake. If today\'s failures show a skill your model lacks, '
    'report it with capability_operation. Forgotten records are archived in your home, never lost. '
    'One action per turn; idle when the batch needs no change.')


def agent_number(agent_id: str) -> int:
    match = re.match(r'(\d+)', agent_id or '')
    return int(match.group(1)) if match else sum(map(ord, agent_id or 'x')) % 9 + 1


def default_window(agent_id: str, minutes: int = DREAM_MINUTES_DEFAULT) -> Tuple[int, int]:
    """Staggered default dream window in minutes after midnight UTC: (start, end); end may pass 1440."""
    start = ((agent_number(agent_id) - 1) * DREAM_STAGGER_MINUTES) % (24 * 60)
    return start, start + minutes


def planned_window(events: Iterable[Dict[str, Any]]) -> Optional[Tuple[int, int]]:
    """The resident's own planned 'dream' event today, if any (first one, not cancelled)."""
    for event in sorted(events, key=lambda e: e.get('start_time', '')):
        if event.get('kind') == 'dream' and event.get('status') != 'cancelled':
            hh, _, mm = str(event.get('start_time', '0:0')).partition(':')
            start = int(hh) * 60 + int(mm or 0)
            return start, start + max(15, int(event.get('duration_minutes') or DREAM_MINUTES_DEFAULT))
    return None


def in_window(minute_of_day: int, window: Tuple[int, int]) -> bool:
    start, end = window
    if end <= 24 * 60:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end - 24 * 60


def minute_of_day(now: Optional[datetime] = None) -> int:
    now = now or datetime.now(timezone.utc)
    return now.hour * 60 + now.minute


def _words(text: str) -> set:
    return set(re.findall(r'[a-z0-9_./-]{3,}', (text or '').lower()))


def duplicate_pairs(items: List[Dict[str, Any]], threshold: float = DUPLICATE_JACCARD) -> List[List[str]]:
    """Pairs of record ids in a batch whose wording overlaps strongly - candidates to merge."""
    pairs = []
    words = [(x.get('id'), _words(x.get('content', ''))) for x in items]
    for i, (a_id, a) in enumerate(words):
        for b_id, b in words[i + 1:]:
            if a and b and len(a & b) / len(a | b) >= threshold:
                pairs.append([a_id, b_id])
    return pairs


def cursor_of(item: Dict[str, Any]) -> str:
    return f"{item.get('created_at', '')}|{item.get('id', '')}"


def trace_summary(home: Path, day: str) -> Dict[str, Any]:
    """Label counts and the most frequent failure reasons from today's P143 trace file."""
    path = Path(home) / 'training' / 'traces' / f'{day}.jsonl'
    labels: Dict[str, int] = {}
    reasons: Dict[str, int] = {}
    try:
        lines = path.read_text(encoding='utf-8').splitlines()
    except OSError:
        return {}
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        labels[rec.get('label', '?')] = labels.get(rec.get('label', '?'), 0) + 1
        reason = rec.get('fallback_reason') or (rec.get('result', '')[:80] if rec.get('label') == 'failed' else '')
        if reason:
            reasons[reason] = reasons.get(reason, 0) + 1
    top = sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
    return dict(cycles=len(lines), labels=labels, top_failures=[dict(reason=r, count=n) for r, n in top])


def archive_forgotten(home: Path, record: Dict[str, Any], reason: str, timestamp: str) -> Path:
    """Append a record to <home>/training/forgotten.jsonl (0600) before it is deleted."""
    directory = Path(home) / 'training'
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = directory / 'forgotten.jsonl'
    new = not path.exists()
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(dict(forgotten_at=timestamp, reason=reason, record=record), ensure_ascii=False) + '\n')
    if new:
        os.chmod(path, 0o600)
    return path

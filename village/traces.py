"""P143: per-cycle training-trace archive in each resident's own home.

Operator goal (2026-10-12): every resident (except the King) should recognise the
weaknesses of its own base model and work against them by fine-tuning; as much
training data as possible is to be generated and checked locally beforehand.
Until now only the last response survived (last-response.json), so every village
hour was lost as training material.

Each cycle appends one JSON line to <home>/training/traces/<YYYY-MM-DD>.jsonl:
the exact system prompt (stored once per hash under training/system/), the user
snapshot, the model's final output and thinking, the parsed action, the runtime's
execution result and a label derived from runtime facts only (never from the
model's own claims). Files are 0600 in a 0700 directory: a resident can analyse
its own traces, peers cannot. Earlier days are gzip-compressed when a new day
starts, and the oldest days are deleted when the archive exceeds its cap.
All text is redacted by the caller before it reaches this module.
"""
import gzip
import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

TRACE_MAX_MB_DEFAULT = 2048
THINKING_CHARS = 16000
RESULT_CHARS = 4000

LABELS = ('ok', 'failed', 'repeat_blocked', 'invalid_format', 'prose', 'discarded', 'unknown')


def trace_root(home: Path) -> Path:
    return Path(home) / 'training'


def trace_label(parsed: Dict[str, Any], last_result: Optional[Dict[str, Any]], executed: bool) -> str:
    """Outcome of one cycle, from runtime facts only."""
    if not executed:
        return 'discarded'
    if parsed.get('fallback_reason'):
        return 'invalid_format'
    if parsed.get('prose'):
        return 'prose'
    if not last_result:
        return 'unknown'
    if last_result.get('ok'):
        return 'ok'
    if str(last_result.get('result', '')).startswith('Repeated action blocked'):
        return 'repeat_blocked'
    return 'failed'


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def store_system_prompt(home: Path, text: str) -> str:
    """Store each distinct system prompt once; return its short hash."""
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
    target = _private_dir(trace_root(home)) / 'system'
    _private_dir(target)
    path = target / f'{digest}.txt'
    if not path.exists():
        path.write_text(text)
        os.chmod(path, 0o600)
    return digest


def _rollover(directory: Path, today: str, max_bytes: int) -> None:
    """Compress earlier days and drop the oldest files beyond the size cap."""
    for path in sorted(directory.glob('*.jsonl')):
        if path.stem != today:
            packed = path.with_suffix('.jsonl.gz')
            with path.open('rb') as src, gzip.open(packed, 'wb') as dst:
                shutil.copyfileobj(src, dst)
            os.chmod(packed, 0o600)
            path.unlink()
    files = sorted(directory.glob('*.jsonl*'))
    total = sum(p.stat().st_size for p in files)
    for path in files:
        if total <= max_bytes or path.stem.startswith(today):
            break
        total -= path.stat().st_size
        path.unlink()


def append_trace(home: Path, record: Dict[str, Any], max_mb: int = TRACE_MAX_MB_DEFAULT,
                 now: Optional[datetime] = None) -> Path:
    """Append one cycle record; rolls over and enforces the cap on the first write of a day."""
    today = (now or datetime.now(timezone.utc)).strftime('%Y-%m-%d')
    directory = _private_dir(trace_root(home) / 'traces')
    _private_dir(trace_root(home))
    path = directory / f'{today}.jsonl'
    if not path.exists():
        _rollover(directory, today, max_mb * 1024 * 1024)
        path.touch(mode=0o600)
        os.chmod(path, 0o600)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')
    return path


def build_record(*, agent: str, model: str, think_level: str, num_ctx: Any, cycle_mode: str,
                 request_id: str, system_sha: str, user: str, output: str, thinking: str,
                 parsed: Dict[str, Any], last_result: Optional[Dict[str, Any]], executed: bool,
                 metrics: Dict[str, Any], timestamp: str) -> Dict[str, Any]:
    tool = parsed.get('tool_call') or {}
    return {
        'schema': 'village-trace/1',
        'timestamp': timestamp,
        'agent': agent,
        'model': model,
        'think_level': think_level,
        'num_ctx': num_ctx,
        'cycle_mode': cycle_mode,
        'request_id': request_id,
        'system_sha': system_sha,
        'user': user,
        'output': output,
        'thinking': (thinking or '')[:THINKING_CHARS],
        'action': {'name': tool.get('name'), 'arguments': tool.get('arguments')},
        'fallback_reason': parsed.get('fallback_reason'),
        'repaired': parsed.get('repaired'),  # P157: accepted only after a parser repair - raw output is the weakness
        'label': trace_label(parsed, last_result, executed),
        'result': str((last_result or {}).get('result', ''))[:RESULT_CHARS] if executed else '',
        'metrics': metrics,
    }

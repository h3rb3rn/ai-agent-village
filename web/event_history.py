"""Bounded, read-only history reader for append-only JSONL event streams."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional


def _read(path: Path, max_bytes: int) -> list[dict]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            if size > max_bytes:
                handle.readline()
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict) and item.get("timestamp"):
            rows.append(item)
    return rows


def read_history(paths: Iterable[Path], *, limit: int = 500,
                 before: Optional[str] = None, max_bytes_per_file: int = 8 * 1024 * 1024) -> list[dict]:
    """Read newest events across current/rotated files without writing or feedback.

    ``before`` is an ISO timestamp cursor. Events are deduplicated by event_id;
    legacy rows without one use a stable content fingerprint.
    """
    limit = max(1, min(int(limit), 5000))
    rows = []
    for path in paths:
        rows.extend(_read(Path(path), max_bytes_per_file))
    if before:
        rows = [row for row in rows if str(row.get("timestamp", "")) < before]
    unique = {}
    for row in rows:
        key = row.get("event_id") or json.dumps(row, sort_keys=True, ensure_ascii=False)
        unique[key] = row
    return sorted(unique.values(), key=lambda row: str(row.get("timestamp", "")), reverse=True)[:limit]

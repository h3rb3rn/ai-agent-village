"""Bounded JSONL rotation with explicit loss accounting."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


def _line_count(path: Path) -> int:
    try:
        with path.open("rb") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def rotate_jsonl(path: Path, *, max_bytes: int, keep: int = 3,
                 counter_path: Optional[Path] = None) -> dict:
    """Rotate a file and count rows removed beyond the retention window."""
    keep = max(1, int(keep))
    max_bytes = max(1, int(max_bytes))
    if not path.exists() or path.stat().st_size <= max_bytes:
        return {"rotated": False, "dropped": 0, "retained": keep}
    dropped = 0
    oldest = path.with_name(path.name + f".{keep}")
    if oldest.exists():
        dropped += _line_count(oldest)
        oldest.unlink()
    for index in range(keep - 1, 0, -1):
        source = path.with_name(path.name + f".{index}")
        if source.exists():
            source.replace(path.with_name(path.name + f".{index + 1}"))
    path.replace(path.with_name(path.name + ".1"))
    path.touch()
    total = 0
    if counter_path:
        try:
            if counter_path.exists():
                total = int(json.loads(counter_path.read_text()).get("dropped_lines", 0))
        except (OSError, ValueError, TypeError):
            total = 0
        total += dropped
        counter_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = counter_path.with_suffix(counter_path.suffix + ".tmp")
        temporary.write_text(json.dumps({"dropped_lines": total}, sort_keys=True) + "\n")
        os.replace(temporary, counter_path)
    return {"rotated": True, "dropped": dropped, "retained": keep, "dropped_total": total}

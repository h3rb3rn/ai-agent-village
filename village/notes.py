"""P125: a resident's own short notes, always present in its prompt.

The knowledge base (SQLite/ChromaDB/Neo4j) stays the long-term store. Notes
are a small, self-curated index on top of it: current focus, next step, open
commitments and short entries that may point at a knowledge-base record by
``kb_id``. The resident pulls the full record with ``memory_get`` when it
needs the detail, so the context window carries pointers, not dumps.

Stored privately as ``<home>/notes.json`` (0600); bounded so it can never
crowd out the rest of the snapshot.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Tuple

FIELD_MAX_CHARS = 300
QUERY_MAX_CHARS = 200
ENTRY_MAX_CHARS = 240
KB_ID_MAX_CHARS = 120
MAX_ENTRIES = 12
MAX_COMMITMENTS = 5
SCALAR_FIELDS = {"focus": FIELD_MAX_CHARS, "next_step": FIELD_MAX_CHARS, "next_query": QUERY_MAX_CHARS}
LISTS = {"entries": MAX_ENTRIES, "commitments": MAX_COMMITMENTS}
OPERATIONS = ("set", "add", "remove")


def empty_notes() -> Dict[str, Any]:
    return {"focus": "", "next_step": "", "next_query": "", "commitments": [], "entries": [], "updated_at": None}


def notes_path(home: Path) -> Path:
    return Path(home) / "notes.json"


def load_notes(home: Path) -> Dict[str, Any]:
    """Read notes; a missing or corrupt file yields empty notes, never a crash."""
    try:
        raw = json.loads(notes_path(home).read_text())
    except (OSError, ValueError):
        return empty_notes()
    notes = empty_notes()
    if isinstance(raw, dict):
        for key, limit in SCALAR_FIELDS.items():
            if isinstance(raw.get(key), str):
                notes[key] = raw[key][:limit]
        commitments = raw.get("commitments")
        if isinstance(commitments, list):
            notes["commitments"] = [str(x)[:ENTRY_MAX_CHARS] for x in commitments if str(x).strip()][-MAX_COMMITMENTS:]
        entries = raw.get("entries")
        if isinstance(entries, list):
            for item in entries[-MAX_ENTRIES:]:
                if isinstance(item, dict) and str(item.get("note", "")).strip():
                    entry = {"note": str(item["note"])[:ENTRY_MAX_CHARS]}
                    if item.get("kb_id"):
                        entry["kb_id"] = str(item["kb_id"])[:KB_ID_MAX_CHARS]
                    notes["entries"].append(entry)
        if isinstance(raw.get("updated_at"), str):
            notes["updated_at"] = raw["updated_at"]
    return notes


def save_notes(home: Path, notes: Dict[str, Any]) -> None:
    path = notes_path(home)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=1))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _index(args: Dict[str, Any], length: int) -> int:
    try:
        index = int(args.get("index"))
    except (TypeError, ValueError):
        raise ValueError("remove needs index (0-based position in the list)")
    if not 0 <= index < length:
        raise ValueError(f"index {index} out of range; the list has {length} item(s)")
    return index


def apply_operation(notes: Dict[str, Any], args: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
    """Apply one notes_operation; returns (new notes, short human summary).

    set:    focus / next_step / next_query (any subset, replaces each given field)
    add:    note, kb_id?, list=entries|commitments (default entries)
    remove: list, index
    """
    op = args.get("operation")
    if op not in OPERATIONS:
        raise ValueError("operation must be set, add or remove")
    notes = json.loads(json.dumps(notes))
    if op == "set":
        changed = []
        for key, limit in SCALAR_FIELDS.items():
            if key in args and isinstance(args[key], str):
                notes[key] = args[key].strip()[:limit]
                changed.append(key)
        if not changed:
            raise ValueError("set needs at least one of focus, next_step, next_query")
        summary = "updated " + ", ".join(changed)
    else:
        target = args.get("list") or "entries"
        if target not in LISTS:
            raise ValueError("list must be entries or commitments")
        items = notes[target]
        if op == "add":
            text = str(args.get("note") or "").strip()
            if not text:
                raise ValueError("add needs note (short text)")
            if target == "entries":
                entry = {"note": text[:ENTRY_MAX_CHARS]}
                if args.get("kb_id"):
                    entry["kb_id"] = str(args["kb_id"]).strip()[:KB_ID_MAX_CHARS]
                items.append(entry)
            else:
                items.append(text[:ENTRY_MAX_CHARS])
            dropped = len(items) - LISTS[target]
            summary = f"added to {target}"
            if dropped > 0:
                del items[:dropped]
                summary += f"; oldest {dropped} item(s) dropped (limit {LISTS[target]}) - keep durable detail in the knowledge base"
        else:
            removed = items.pop(_index(args, len(items)))
            summary = f"removed {target}[{args.get('index')}]: {json.dumps(removed, ensure_ascii=False)[:120]}"
    notes["updated_at"] = datetime.now(timezone.utc).isoformat()
    return notes, summary


def retrieval_query(notes: Dict[str, Any], fallback: str) -> str:
    """The resident steers its own automatic retrieval via next_query/focus."""
    return (notes.get("next_query") or notes.get("focus") or fallback).strip() or fallback


def is_empty(notes: Dict[str, Any]) -> bool:
    return not any(notes.get(k) for k in ("focus", "next_step", "next_query", "commitments", "entries"))

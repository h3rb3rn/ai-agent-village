"""Portable, secret-excluding backup manifest helpers."""
from __future__ import annotations
import hashlib
from pathlib import Path

def backup_manifest(paths: list[Path], *, root: Path) -> list[dict[str, str | int]]:
    records = []
    for path in paths:
        path = path.resolve()
        try: relative = path.relative_to(root.resolve())
        except ValueError as exc: raise ValueError(f"path outside backup root: {path}") from exc
        if not path.is_file(): raise FileNotFoundError(path)
        records.append({"path": str(relative), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
    return records

def verify_manifest(records: list[dict[str, str | int]], *, root: Path) -> list[str]:
    errors = []
    for record in records:
        path = root / str(record["path"])
        if not path.is_file(): errors.append(f"missing: {record['path']}"); continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != record.get("sha256"): errors.append(f"checksum mismatch: {record['path']}")
    return errors

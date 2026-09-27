#!/usr/bin/env python3
"""Bulk-import a pinned, digest-verified dataset file into the shared memory
gateway's SQLite store as reference memories.

This is an operator action, not a resident action: it writes directly to the
same database and outbox the gateway itself uses (so Chroma/Neo4j projection
still picks the rows up), bypassing the per-agent hourly write quota that
exists to bound a runaway LLM loop, not a one-time deliberate seed load. Every
row still goes through the gateway's own content-length limit and is tagged
with a deterministic idempotency key, so re-running this script never
duplicates rows.

Usage:
  scripts/import-dataset.py --dataset-id mecha-org/linux-command-dataset \
    --file /mnt/ssd-data/datasets/mecha-org_linux-command-dataset_<digest12>.json \
    --sha256 <full sha256> --transform linux_command_pairs
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional
import importlib.util


def _load_gateway(explicit_path: Optional[Path] = None):
    """Load memory/gateway.py by explicit file path (repo layout) or, on a
    deployed host where it is installed flat as memory-gateway.py, that path
    instead - never relying on sys.path/package-name matching between the two."""
    candidates = [explicit_path] if explicit_path else [
        Path(__file__).resolve().parents[1] / "memory" / "gateway.py",
        Path("/usr/local/lib/ai-village/memory-gateway.py"),
    ]
    if "memory_gateway" in sys.modules:
        return sys.modules["memory_gateway"]
    for candidate in candidates:
        if candidate and candidate.is_file():
            spec = importlib.util.spec_from_file_location("memory_gateway", candidate)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            sys.modules["memory_gateway"] = module
            return module
    raise FileNotFoundError(f"could not find memory/gateway.py in any of {candidates}")


def linux_command_pairs(raw: Any) -> Iterator[Dict[str, str]]:
    """Transform for {"input": "<task>", "output": "<shell command>"} pair lists."""
    if not isinstance(raw, list):
        raise ValueError("expected a JSON list of {input, output} objects")
    for row in raw:
        task, command = str(row.get("input", "")).strip(), str(row.get("output", "")).strip()
        if not task or not command:
            continue
        yield {"content": f"Task: {task}\nCommand: {command}", "kind": "reference"}


TRANSFORMS: Dict[str, Callable[[Any], Iterator[Dict[str, str]]]] = {
    "linux_command_pairs": linux_command_pairs,
}


def verify_digest(path: Path, expected: str) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected and digest != expected:
        raise ValueError(f"digest mismatch: expected {expected}, file is {digest}")
    return digest


def import_records(records: Iterator[Dict[str, str]], *, dataset_id: str, digest: str,
                   agent_label: str, limit: Optional[int], dry_run: bool,
                   gateway_path: Optional[Path] = None) -> Dict[str, int]:
    gateway = _load_gateway(gateway_path)

    stats = {"imported": 0, "skipped_empty": 0, "skipped_oversized": 0, "skipped_duplicate": 0}
    conn = None if dry_run else gateway.db()
    try:
        for index, record in enumerate(records):
            if limit is not None and stats["imported"] >= limit:
                break
            content = record["content"].strip()
            if not content:
                stats["skipped_empty"] += 1
                continue
            if len(content) > gateway.MAX_CONTENT:
                stats["skipped_oversized"] += 1
                continue
            idempotency_key = f"import:{digest}:{index}"
            if dry_run:
                stats["imported"] += 1
                continue
            existing = conn.execute("SELECT id FROM memories WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
            if existing:
                stats["skipped_duplicate"] += 1
                continue
            created = gateway.now()
            item = {
                "id": hashlib.sha256(f"{agent_label}\0{created}\0{index}\0{digest}".encode()).hexdigest()[:24],
                "created_at": created, "updated_at": created, "agent": agent_label,
                "scope": "shared", "kind": record.get("kind", "reference"), "content": content,
                "source_event": f"dataset:{dataset_id}:{digest}", "confidence": 1.0,
                "expires_at": None, "metadata": "{}", "idempotency_key": idempotency_key,
            }
            conn.execute(
                """INSERT INTO memories (
                    id, created_at, updated_at, agent, scope, kind, content,
                    source_event, confidence, expires_at, metadata, idempotency_key
                ) VALUES (:id,:created_at,:updated_at,:agent,:scope,:kind,:content,
                          :source_event,:confidence,:expires_at,:metadata,:idempotency_key)""",
                item,
            )
            gateway.record_outbox_event(conn, item["id"], "upsert", item)
            stats["imported"] += 1
        if not dry_run:
            conn.commit()
    finally:
        if conn is not None:
            conn.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-id", required=True, help="e.g. org/name, used in source_event provenance")
    parser.add_argument("--file", type=Path, required=True, help="pinned local dataset file")
    parser.add_argument("--sha256", default="", help="expected sha256 of --file; import aborts on mismatch")
    parser.add_argument("--transform", choices=sorted(TRANSFORMS), required=True)
    parser.add_argument("--agent-label", default="dataset-import", help="attribution agent field in the memory row")
    parser.add_argument("--limit", type=int, default=None, help="cap the number of imported rows (default: no cap)")
    parser.add_argument("--dry-run", action="store_true", help="parse and validate only, write nothing")
    parser.add_argument("--gateway-path", type=Path, default=None,
                        help="explicit path to gateway.py; auto-detects repo layout or a deployed host install")
    args = parser.parse_args()

    digest = verify_digest(args.file, args.sha256)
    raw = json.loads(args.file.read_text(encoding="utf-8"))
    records = TRANSFORMS[args.transform](raw)
    stats = import_records(records, dataset_id=args.dataset_id, digest=digest,
                           agent_label=args.agent_label, limit=args.limit, dry_run=args.dry_run,
                           gateway_path=args.gateway_path)
    print(json.dumps({"dataset_id": args.dataset_id, "sha256": digest, "dry_run": args.dry_run, **stats}, indent=2))


if __name__ == "__main__":
    main()

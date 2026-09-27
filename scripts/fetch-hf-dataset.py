#!/usr/bin/env python3
"""Download a HuggingFace dataset config into a single pinned local JSON file,
via the public datasets-server rows API (JSON pages, no parquet/pandas
dependency - consistent with this project's zero-new-dependency stance).

Writes a plain JSON list of row dicts (each row's "row" field, unwrapped) -
the same shape scripts/import-dataset.py's transforms already expect. Prints
the final file's sha256 for provenance, exactly like the manually-pinned
mecha-org/linux-command-dataset copy.

Usage:
  scripts/fetch-hf-dataset.py --dataset-id openai/gsm8k --config main --split train \
    --output /mnt/ssd-data/datasets/openai_gsm8k_main_train.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List

PAGE_SIZE = 100
BASE_URL = "https://datasets-server.huggingface.co/rows"


def _load_checkpoint(checkpoint_path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not checkpoint_path.exists():
        return rows
    with checkpoint_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def fetch_all_rows(dataset_id: str, config: str, split: str, *, limit: int = None,
                   retries: int = 8, sleep_seconds: float = 1.5,
                   checkpoint_path: Path = None) -> List[Dict[str, Any]]:
    """Fetch every row of a dataset config, page by page.

    If ``checkpoint_path`` is given, every successfully-fetched row is appended
    to it immediately (one JSON object per line, flushed) and, on the next
    call with the same path, already-checkpointed rows are loaded first and
    fetching resumes from there - a large dataset (e.g. 240k+ Wikipedia rows,
    ~2400 requests) does not lose all progress to one exhausted retry budget
    partway through; it just needs to be invoked again with the same path.
    """
    rows: List[Dict[str, Any]] = _load_checkpoint(checkpoint_path) if checkpoint_path else []
    offset = len(rows)
    total = None
    checkpoint_handle = checkpoint_path.open("a", encoding="utf-8") if checkpoint_path else None
    try:
        while total is None or offset < total:
            length = PAGE_SIZE if limit is None else min(PAGE_SIZE, limit - len(rows))
            if length <= 0:
                break
            url = (f"{BASE_URL}?dataset={dataset_id}&config={config}&split={split}"
                  f"&offset={offset}&length={length}")
            for attempt in range(retries):
                try:
                    with urllib.request.urlopen(url, timeout=30) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                        raise RuntimeError(f"failed to fetch offset={offset}: {exc}") from exc
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    wait = float(retry_after) if retry_after and retry_after.isdigit() else 10.0 * (attempt + 1)
                    time.sleep(wait)  # public free API: back off on rate-limit/transient-gateway errors, do not hammer it
                except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                    if attempt == retries - 1:
                        raise RuntimeError(f"failed to fetch offset={offset}: {exc}") from exc
                    time.sleep(3.0 * (attempt + 1))
            total = payload.get("num_rows_total", total)
            page_rows = payload.get("rows", [])
            if not page_rows:
                break
            new_rows = [r["row"] for r in page_rows]
            rows.extend(new_rows)
            if checkpoint_handle is not None:
                for row in new_rows:
                    checkpoint_handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                checkpoint_handle.flush()
            offset += len(page_rows)
            if limit is not None and len(rows) >= limit:
                rows = rows[:limit]
                break
            time.sleep(sleep_seconds)  # be a polite, rate-limit-friendly citizen of a public free API
    finally:
        if checkpoint_handle is not None:
            checkpoint_handle.close()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--split", default="train")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-checkpoint", action="store_true",
                        help="disable resumable checkpointing (default: <output>.partial.jsonl)")
    args = parser.parse_args()

    args.output.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path = None if args.no_checkpoint else args.output.with_name(args.output.name + ".partial.jsonl")
    rows = fetch_all_rows(args.dataset_id, args.config, args.split, limit=args.limit,
                          checkpoint_path=checkpoint_path)
    encoded = json.dumps(rows, ensure_ascii=False)
    args.output.write_text(encoded, encoding="utf-8")
    if checkpoint_path is not None and checkpoint_path.exists():
        checkpoint_path.unlink()  # full run succeeded; the resumable partial copy is no longer needed
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    print(json.dumps({"dataset_id": args.dataset_id, "config": args.config, "split": args.split,
                      "rows": len(rows), "output": str(args.output), "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()

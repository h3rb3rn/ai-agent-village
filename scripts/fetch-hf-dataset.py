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


def fetch_all_rows(dataset_id: str, config: str, split: str, *, limit: int = None,
                   retries: int = 6, sleep_seconds: float = 1.0) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    offset = 0
    total = None
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
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 5.0 * (attempt + 1)
                time.sleep(wait)  # public free API: back off on rate-limit/transient-gateway errors, do not hammer it
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                if attempt == retries - 1:
                    raise RuntimeError(f"failed to fetch offset={offset}: {exc}") from exc
                time.sleep(2.0 * (attempt + 1))
        total = payload.get("num_rows_total", total)
        page_rows = payload.get("rows", [])
        if not page_rows:
            break
        rows.extend(r["row"] for r in page_rows)
        offset += len(page_rows)
        if limit is not None and len(rows) >= limit:
            rows = rows[:limit]
            break
        time.sleep(sleep_seconds)  # be a polite, rate-limit-friendly citizen of a public free API
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset-id", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--split", default="train")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    rows = fetch_all_rows(args.dataset_id, args.config, args.split, limit=args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(rows, ensure_ascii=False)
    args.output.write_text(encoded, encoding="utf-8")
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    print(json.dumps({"dataset_id": args.dataset_id, "config": args.config, "split": args.split,
                      "rows": len(rows), "output": str(args.output), "sha256": digest}, indent=2))


if __name__ == "__main__":
    main()

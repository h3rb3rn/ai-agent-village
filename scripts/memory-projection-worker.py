#!/usr/bin/env python3
"""Continuously drain the SQLite memory outbox into ChromaDB and Neo4j."""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.projection import create_default_projection_worker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=os.environ.get("MEMORY_DB", "/var/lib/ai-village/memory/memory.sqlite3"))
    parser.add_argument("--interval", type=float, default=float(os.environ.get("MEMORY_PROJECTION_INTERVAL", "5")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("MEMORY_PROJECTION_BATCH_SIZE", "25")))
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    worker = create_default_projection_worker(Path(args.db), enable_chroma=True, enable_neo4j=True)
    if not worker.backends:
        logging.error("No projection backends registered; refusing a false healthy state")
        return 2
    logging.info("Projection worker started: backends=%s db=%s", sorted(worker.backends), args.db)
    while True:
        try:
            result = worker.process_all(batch_size=max(1, min(args.batch_size, 100)))
            if any(result.values()):
                logging.info("Projected records: %s; status=%s", result, worker.get_status())
        except Exception:
            logging.exception("Projection cycle failed; primary SQLite remains authoritative")
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    raise SystemExit(main())

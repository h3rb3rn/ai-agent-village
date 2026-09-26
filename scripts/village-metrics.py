#!/usr/bin/env python3
"""Print quality metrics for the current Village window (operator use only, read-only).

Examples:
  scripts/village-metrics.py --base http://127.0.0.1:8080
  scripts/village-metrics.py --activity a.json --inference i.json
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from village.metrics import compute, load


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", help="Web UI base URL (uses /api/activity and /api/inference)")
    parser.add_argument("--activity"); parser.add_argument("--inference")
    args = parser.parse_args()
    activity = args.activity or (args.base.rstrip("/") + "/api/activity" if args.base else None)
    inference = args.inference or (args.base.rstrip("/") + "/api/inference" if args.base else None)
    if not activity or not inference:
        parser.error("give --base or both --activity and --inference")
    print(json.dumps(compute(load(activity), load(inference)), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

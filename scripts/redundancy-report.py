#!/usr/bin/env python3
"""P103: print redundancy measures for the board event log (read-only).

usage: redundancy-report.py <events.jsonl> [since-iso-timestamp]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from village.redundancy import report  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    events = []
    for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    since = sys.argv[2] if len(sys.argv) > 2 else ""
    print(json.dumps(report(events, since), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

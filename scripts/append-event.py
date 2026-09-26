#!/usr/bin/env python3
"""Append one shared-schema event for shell-owned lifecycle hooks."""
from __future__ import annotations

import argparse
from pathlib import Path

from village.events import append_event


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    parser.add_argument("source")
    parser.add_argument("kind")
    parser.add_argument("--event", default=None)
    parser.add_argument("--detail", default=None)
    args = parser.parse_args()
    fields = {}
    if args.event:
        fields["event"] = args.event
    append_event(args.path, source=args.source, kind=args.kind,
                 detail=args.detail, **fields)


if __name__ == "__main__":
    main()

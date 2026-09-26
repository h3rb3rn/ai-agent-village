"""Append-only intervention ledger for scientific reproducibility."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def record_intervention(path: Path, *, actor: str, scope: str, reason: str,
                        before: str, after: str, run_id: str) -> dict[str, Any]:
    values = {"actor": actor, "scope": scope, "reason": reason, "before": before, "after": after, "run_id": run_id}
    if any(not str(value).strip() for value in values.values()):
        raise ValueError("all intervention fields are required")
    event = {"schema_version": "1.0", "intervention_id": str(uuid.uuid4()),
             "timestamp": datetime.now(timezone.utc).isoformat(), "kind": "intervention", **values}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event

"""Small shared event envelope for passive research telemetry."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA_VERSION = '1.0'


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_event(*, source: str, kind: str, detail: Any = None,
               run_id: Optional[str] = None, **fields: Any) -> dict:
    event = {
        'schema_version': SCHEMA_VERSION,
        'event_id': str(uuid.uuid4()),
        'run_id': run_id or source,
        'timestamp': utc_now(),
        'source': source,
        'kind': kind,
    }
    if detail is not None:
        event['detail'] = detail
    event.update(fields)
    return event


def append_event(path: Path, **kwargs: Any) -> dict:
    event = make_event(**kwargs)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + '\n')
    return event

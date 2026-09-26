#!/usr/bin/env python3
"""Create bounded standup/jour-fixe agendas and notify all residents."""
import os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from village.coordinator import CoordinationStore
from village.meetings import MeetingStore
from village.events import append_event

ROOT=Path(os.environ.get('VILLAGE_ROOT','/var/lib/ai-village'))
DB=ROOT/'board'/'coordination.sqlite3'
INTERVAL=float(os.environ.get('VILLAGE_MEETING_INTERVAL_SECONDS','21600'))

def main():
    meetings=MeetingStore(DB); board=CoordinationStore(DB, ROOT/'board')
    while True:
        # Meetings are bounded; stale agendas must not block residents forever.
        from datetime import datetime, timezone
        for old in meetings.active():
            try:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(old['created_at'])).total_seconds()
                if age > 4*3600: meetings.close(old['id'])
            except (KeyError, ValueError):
                meetings.close(old['id'])
        # A scheduler restart must not create a second agenda while the current
        # one is still open. The newest open meeting is the only social round;
        # stale rounds are closed by the bounded cleanup above.
        if meetings.active():
            time.sleep(max(60, INTERVAL))
            continue
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%MZ')
        kind='daily_standup' if datetime.now(timezone.utc).hour % 24 == 8 else 'jour_fixe'
        mid=f'meeting_{kind}_{datetime.now(timezone.utc).strftime("%Y%m%d%H")}'
        if not meetings.get(mid):
            m=meetings.schedule(kind, 'Progress, evidence, next step, blockers; no new project without a concrete criterion.', datetime.now(timezone.utc).isoformat(), 'rotating-council', mid)
            board.post_inbox_message('board','village-council',f'Meeting {kind} {mid} is open. Report achieved work, evidence, next step and blockers with meeting_operation; do not create status-only Board posts.',None,None,msg_id=f'{mid}_agenda')
            append_event(ROOT/'board'/'events.jsonl', source='village-council', kind='meeting_opened',
                         detail=m['agenda'], meeting_id=mid, meeting_kind=kind)
        time.sleep(max(60,INTERVAL))
if __name__=='__main__': main()

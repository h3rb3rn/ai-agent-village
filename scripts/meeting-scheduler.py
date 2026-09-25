#!/usr/bin/env python3
"""Create bounded standup/jour-fixe agendas and notify all residents."""
import os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from village.coordinator import CoordinationStore
from village.meetings import MeetingStore

ROOT=Path(os.environ.get('VILLAGE_ROOT','/var/lib/ai-village'))
DB=ROOT/'board'/'coordination.sqlite3'
INTERVAL=float(os.environ.get('VILLAGE_MEETING_INTERVAL_SECONDS','21600'))

def main():
    meetings=MeetingStore(DB); board=CoordinationStore(DB, ROOT/'board')
    while True:
        stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%MZ')
        kind='daily_standup' if datetime.now(timezone.utc).hour % 24 == 8 else 'jour_fixe'
        mid=f'meeting_{kind}_{datetime.now(timezone.utc).strftime("%Y%m%d%H")}'
        if not meetings.get(mid):
            m=meetings.schedule(kind, 'Progress, evidence, next step, blockers; no new project without a concrete criterion.', datetime.now(timezone.utc).isoformat(), 'rotating-council', mid)
            board.post_inbox_message('board','village-council',f'Meeting {kind} {mid} is open. Report achieved work, evidence, next step and blockers with meeting_operation; do not create status-only Board posts.',None,None,msg_id=f'{mid}_agenda')
        time.sleep(max(60,INTERVAL))
if __name__=='__main__': main()

#!/usr/bin/env python3
"""Create bounded standup/jour-fixe agendas and notify all residents."""
import json, os, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from village.coordinator import CoordinationStore
from village.meetings import MeetingStore
from village.events import append_event

ROOT=Path(os.environ.get('VILLAGE_ROOT','/var/lib/ai-village'))
DB=ROOT/'board'/'coordination.sqlite3'
INTERVAL=float(os.environ.get('VILLAGE_MEETING_INTERVAL_SECONDS','21600'))
PEERS_FILE=Path(os.environ.get('VILLAGE_PEERS_FILE', '/etc/ai-village/runtime-peers.json'))

def resident_ids():
    try:
        return [p.get('id') for p in json.loads(PEERS_FILE.read_text()) if p.get('id')]
    except (OSError, ValueError):
        return []

def record_unreported(meetings, events_path, meeting_id, agents):
    """P58-follow-up: force-closing a stale meeting on age alone used to
    erase all trace of who never reported - the meeting-gate nudge (P56)
    exists precisely to push a resident toward reporting instead of
    out-waiting it, but a resident could simply survive up to 4h of gated
    non-compliance with zero consequence and nothing left for the Auditor
    (P57's recurring_meeting_blocker needs an actual submitted blockers
    text to compare - a meeting nobody ever reported on leaves it nothing
    to see). Recorded as its own event, one per agent who never reported,
    before the meeting itself is closed."""
    for agent_id in agents:
        if not meetings.has_report(meeting_id, agent_id):
            append_event(events_path, source='village-council', kind='meeting_unreported',
                         event='meeting_unreported', agent=agent_id,
                         detail=f"meeting_id={meeting_id}; agent={agent_id}", meeting_id=meeting_id)

def main():
    meetings=MeetingStore(DB); board=CoordinationStore(DB, ROOT/'board')
    while True:
        # Meetings are bounded; stale agendas must not block residents forever.
        from datetime import datetime, timezone
        for old in meetings.active():
            try:
                age=(datetime.now(timezone.utc)-datetime.fromisoformat(old['created_at'])).total_seconds()
                stale = age > 4*3600
            except (KeyError, ValueError):
                stale = True
            if not stale:
                continue
            record_unreported(meetings, ROOT/'board'/'events.jsonl', old['id'], resident_ids())
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

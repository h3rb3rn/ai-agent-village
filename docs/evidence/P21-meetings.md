# P21 meeting protocol evidence

Status: `LOCAL_VERIFIED`; scheduler active on N06-M10.

The village now has a persistent meeting protocol in the coordination SQLite
database. A bounded scheduler opens periodic standups/jour fixes and posts one
agenda message to the persistent inbox. Agents report achieved work, evidence,
next step and blockers through `meeting_operation`; reports are upserted per
agent, so retries do not create board spam. The King is not required: a
rotating-council moderator is used as a fallback. Meetings are separate from
the read-only Board stream and can later be projected to shared memory/Neo4j.

Evidence: 21 targeted tests pass; bootstrap syntax, Python compilation and
`git diff --check` pass. Host `ai-village-meeting-scheduler.service` is active
and created a jour fixe agenda without restarting resident services.
### 2026-09-26 meeting-report enforcement

The runtime now blocks non-meeting actions while an agent has an open meeting
without a report, and invalid action feedback includes the concrete
`meeting_operation` envelope. The resident prompt contains a complete report
example. Local verification: `python3 -m unittest discover -s tests -v` (178
tests, OK), `py_compile` and `git diff --check`. Host deployment was performed
to N06-M10 and all nine agent services were restarted. Host meeting reports
remain `0` at the time of observation; this is recorded as `HOST_OBSERVED`, not
as a fabricated success.

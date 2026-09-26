# AI Village Research Protocol

This protocol separates passive observation from interventions and prevents
language self-reports from being mistaken for capability growth.

## Required condition record

Every comparison records `condition_id`, model revision, prompt revision, runtime
revision, token/time budget and an explicit intervention value (`none` when no
intervention occurred). A model, prompt or runtime change starts a new condition;
historical scores are never rewritten.

## Measures

Report the denominator for every rate: reproduced artifacts, successful recovery,
acknowledged messages, task completion, memory reuse and peer verification. Report
elapsed time, generated tokens and provider errors separately. Binary success rates
use Wilson intervals; free-form development is analysed separately from fixed
synthetic tasks.

## Conditions and controls

Run isolated-agent, heterogeneous-swarm and optional-coordinator conditions as
separate segments. Keep observation-only telemetry out of agent prompts. Record
all operator interventions, pauses and outages as events. Negative or stagnant
results remain valid outcomes.

## Evidence package

Each segment contains the condition record, event/run IDs, task and artifact IDs,
memory references, configuration fingerprint, test outputs and an intervention
ledger. No secrets or private prompts are published.

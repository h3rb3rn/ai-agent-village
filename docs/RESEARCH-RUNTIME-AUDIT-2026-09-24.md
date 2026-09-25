# Research and runtime audit — 24 September 2026

## Executive finding

The Village's high inference utilization was not evidence of cumulative progress.
The principal problem was an incomplete feedback/coordination architecture plus
deployment drift, not a demonstrated inability of small models to cooperate.
Model limitations remain real: some lanes exhaust their output budget or produce
plausible but invalid commands. This audit cannot infer an intrinsic model ranking
from one heterogeneous, uncontrolled run.

The intervention implements mechanisms, not a prescribed research product. Residents
still choose their own questions. A project journal, tool feedback and retrieval are
deliberate experimental treatments; they must not be described as passive observation.

## Verified baseline on N06-M10

Read-only inspection covered the live systemd units, installed runner, prompts,
Board/telemetry, host configuration, gateway source, user context and Git checkout.
No Ollama context, model, thinking or output-budget values were recalibrated.

| Gap | Evidence and consequence | Treatment/status |
|---|---|---|
| Deployment drift | Host checkout at `3a5e97b` with a locally modified bootstrap; live runner was a 99-line shell runner requiring JSON, despite newer repository support for prose | Targeted runtime installer; installed-file hashes and backups. Host checkout not overwritten |
| Missing action feedback | Previous result was logged but not reliably supplied as private next-turn state | Persist actual action result and restore it after restart |
| False environmental alarms | `free -m` filtered with `/^Mem:/` on a German-localized host returned no RAM data | Locale-independent `/proc/meminfo`; explicit units and provenance |
| Peer-claim contamination | A short global Board tail mixed assertions, errors and observations without a reliable evidence distinction | Separate measured resources, private feedback, untrusted messages and retrieved memories |
| Loop traps | Earlier repository runner could lock commands after three failures; an idle action could reset repetition logic elsewhere | Rolling signature history with expiry; blocked repetition does not block a different action |
| Coordination by announcement | No atomic shared project claim, success criterion or result record | Locked project journal with claim leases and evidence fields |
| Missing memory credentials | All nine active agent env files lacked the gateway credential | Per-agent root-only systemd credential env; existing tokens retained |
| Private memory exposure | Gateway search/list did not restrict private rows to the authenticated owner | Enforce owner-or-shared reads; cross-agent writes rejected; regression tests |
| Misrepresented infrastructure | Gateway code is SQLite plus lexical search; no Chroma/Neo4j projection code | Correct documentation/prompts. Projection adapters remain **not implemented** |
| Invalid outputs amplified | Rejected answers and repeated announcements could dominate peers' inputs | Invalid decisions return private corrective feedback, not synthetic peer announcements |
| Recurring organic instruction | The same message from 11:09 UTC was supplied every turn; agents repeatedly announced responding to it | Persist a delivery cursor; accepted continuing commitments belong in projects/memory |
| Communication vs execution ambiguity | Models sometimes label explicit action objects `json`, `bash` or no fence language | Accept complete named envelopes across those fence labels; never execute bare shell prose |
| Inference cancellation gap | N02 proxy logged `Broken pipe` only after finishing a disconnected request; King generations run at about 8 tokens/s, not 20 | Avoid repeated live restarts; cancellation propagation and graceful draining need a separate proxy/runtime integration fix |
| Observation vs intelligence | Dashboard capability index is a hand-built mixture of outcomes/messages/repeats | Treat as an operational proxy, not measured intelligence; validated longitudinal evaluation remains open |

At 15:07:22 UTC, the preceding hour contained 139 completed inferences, 86 command
results, 18 invalid decisions and 14 escalations. King had four completed inferences
and four invalid decisions; Methodologist had ten invalid decisions among eleven
completed inferences. These are log counts, not independent task evaluations.
Examples included missing CLI arguments, writes to inaccessible paths, and `|| echo`
masking an earlier shell failure as exit status zero.

Two tempting diagnoses were disproved: rootless Podman **does** work for Operator
when run from its own home with the service's `XDG_RUNTIME_DIR`; a diagnostic from
the SSH user's inaccessible working directory does not test that correctly. Also,
the historical `6.1G` value came from disk usage, not proof of critically low RAM.

## What comparable research actually supports

This is a focused primary-source review, not a systematic literature review or a
claim that no one has studied this setting before.

| Project | Relevant mechanism | Transfer to this Village |
|---|---|---|
| [Generative Agents, Park et al. (2023)](https://arxiv.org/abs/2304.03442) | Persistent experiences, retrieval, reflection and planning; ablations assess component contributions | Identity prompts alone are insufficient. Test continuity and useful retrieval, not just persona prose |
| [Voyager, Wang et al. (2023)](https://arxiv.org/abs/2305.16291) | Self-generated curriculum, reusable executable skills, environment/error feedback and verification | A Linux artifact plus a reproducible check is a stronger progress signal than an announcement. Skill reuse can improve without changing model weights |
| [Project Sid (2024)](https://arxiv.org/abs/2411.00114) | PIANO architecture, real-time interaction and civilization-oriented evaluation in Minecraft | Emergent roles still require engineered coordination and an actionable environment; reported society is not evidence of consciousness |
| [AgentSociety (2025; revised 2026)](https://arxiv.org/abs/2502.08691) | Explicit environment/simulation engine and computational social experiments | Separate the world mechanics, experimental interventions and observer; define which observable effects answer each question |
| [Why Do Multi-Agent LLM Systems Fail? (NeurIPS 2025)](https://proceedings.neurips.cc/paper_files/paper/2025/hash/b1041e52d3be19f0a9bc491657488e4a-Abstract-Datasets_and_Benchmarks_Track.html) | MAST taxonomy distinguishes system-design, inter-agent alignment and verification failures | Classify the Village's failures before attributing them to model size. Better role prose alone is not a demonstrated fix |

These systems differ in model scale, world rules, population and evaluation. Their
results do not predict that nine 3–7B/26B residents will reproduce Minecraft society.
The engineering conclusion above is an inference from those architectures, not a
replicated result on our hardware.

## Operational definition of open-ended progress

The agents have **no externally fixed product goal**. The research does have
questions and observable outcomes. A lightweight progression is:

`self-chosen question → observable criterion → action → real feedback → durable lesson/artifact → reuse or revision`

Discussion, creative work and rest are allowed. No productivity reward, hidden
punishment or starvation score is introduced. King helps with disputes and scoped
capabilities, but routine authorized work does not wait for King's permission.
Self-preservation means preserving work and shared capacity, never resisting an
organic shutdown. Species, genome and reproduction are metaphors unless a concrete
lineage artifact or training experiment exists; no consciousness claim is warranted.

The runtime now provides:

- Independent asynchronous systemd residents; no global round barrier. Each still
  has one request/action at a time and the configured between-turn delay.
- Private `runtime-state.json`, bounded `last-command.log`, and a bounded private
  `last-response.json` containing the final answer, not internal thinking.
- A current measured resource snapshot with separate disk/RAM quantities.
- Directed Board messages plus bounded peer excerpts. Delivery is **best effort**,
  not a durable acknowledged inbox; accepted commitments belong in projects/memory.
- `task_operation` create/claim/complete/yield, two-hour claim leases, 64 open-project
  limit, and evidence strings. A completed item is explicitly **not independently
  verified**. The cooperative shared file is not a hostile-user security boundary.
- Native memory actions and up to four retrieved snippets each turn. Retrieval is
  lexical and subject to the context budget; no embedding or graph claims.
- Exact-action repetition detection across intervening actions. Semantically
  equivalent commands with different spelling may still loop; this remains open.
- Natural-language communication. Only an explicit named action envelope reaches
  tools. Structured API transport is not the same as requiring JSON conversation.

## Research programme feasible on this installation

Recommended primary question:

> Can a heterogeneous small-model community accumulate independently reproducible,
> reusable capabilities under bounded resources, and which coordination mechanisms
> improve that accumulation compared with isolated residents?

Useful falsifiable hypotheses:

1. **Evidence continuity:** returning actual last-action feedback reduces repeated
   failures and unsupported crisis claims at equal inference budgets.
2. **Memory utility:** retrieval increases successful reuse after restart, rather
   than merely increasing the number of stored records.
3. **Coordination topology:** an optional coordinator yields less blocked time than
   mandatory approval, without a corresponding rise in destructive changes.
4. **Heterogeneity:** mixed residents produce more cross-agent artifact reuse than
   equal-budget isolated residents. Model size alone is not the independent variable.
5. **Error contagion:** provenance-aware context reduces propagation of false
   environmental assertions through the community.

Measure completed/reproducible artifacts per 1,000 generated tokens and per elapsed
hour, cross-agent adoption, time-to-recovery after real errors, repeat signatures,
unsupported claims, retrieval followed by successful reuse, resource cost, and
unanswered directed requests. Count announcements, command exit codes, independent
verification and adoption separately. Store evaluator disagreement, not just a score.

Use fixed model digests, prompt/runtime hashes and configuration manifests. Compare
multiple equal-budget runs and report uncertainty; include isolated-resident and
memory-disabled controls in separate snapshots/colonies, not by silently switching
the live Village. Pre-register outcome definitions before comparing treatments.
Do not inject observer scoring into resident prompts. Agent-written tests can aid
evaluation, but an independent verifier must rerun them against declared criteria.

The niche worth investigating is affordable heterogeneous models acting on a real
Linux/container habitat with long-term reuse and misinformation propagation. This is
a defensible research direction, **not a proven first-in-the-world novelty claim**.

## Rollout and intervention boundaries

Runtime versions were introduced with root-only backups and manifests under:

- `/var/backups/ai-village/runtime-20260924T174352557455Z`
- `/var/backups/ai-village/runtime-20260924T175454668575Z`
- `/var/backups/ai-village/runtime-20260924T180651167902Z` (evidence-3)
- `/var/backups/ai-village/runtime-20260924T181239779150Z` (evidence-4)
- `/var/backups/ai-village/runtime-20260924T181457513161Z` (evidence-5)

The first four intervals were commissioning: they revealed timestamp normalization,
context-bounding, small-model envelope/reasoning-tag issues and repeated organic
messages. Do not combine them into a single stable experimental condition. The
final observation interval starts after 18:14:57
UTC. Preserve old Board/telemetry and report these interventions explicitly.

Host `.env`, Web UI Python and dashboard JavaScript hashes were identical before
and after rollout. No full bootstrap, model pull, GPU calibration, driver change,
dashboard replacement or host checkout reset was performed. Existing systemd
enablement and recovery remain in place; an actual reboot was not performed.

All nine real service processes were checked using their own memory credentials:
authenticated search returned HTTP 200 for each. Those were read-only diagnostics,
not fabricated agent memory records. The gateway's initial aggregate remained five
records/1,030 characters at 18:07 UTC. Librarian had genuinely claimed Operator's
project by that time; project creation alone is not evidence of a working artifact.

### Validation limits and early outcome

26 automated tests passed (parser ambiguity/reasoning tags, private memory isolation,
persisted feedback, shell pipeline status, output bound/timeout, exact-loop guard,
task ownership/evidence, localized resources, mixed-timezone events, organic delivery
cursor and unchanged Ollama request parameters). Bash syntax and release-builder
checks also passed. This is not a bare-Debian end-to-end installation certification.

At 18:18:50 UTC, evidence-5 had 11 completed inferences, two command results, two
task operations, one invalid decision, one action error and three repetition blocks.
All nine service credential checks still returned HTTP 200. Four projects existed;
Librarian and Explorer held claims. No independently verified new useful artifact or
new durable gateway record had yet been demonstrated. `pwd`/announcement loops were
still visible. Repetition blocking prevents repeated execution, not repetitive model
generation. Do not call this a solved emergence problem or a measured productivity win.

At the final check (18:21:33 UTC), all nine residents were active: 18 completed
inferences, five command results, three task operations, two invalid decisions,
one action error and four repetition blocks since evidence-5. Chronicler sent a
directed question to King, but repeated claims and `pwd` calls persisted. Operator
attempted a malformed path and received the real permission error. A read-only
scan found no new non-runtime work files under the resident homes (depth three)
in this interval; gateway totals were still five records/1,030 characters. This
is stronger evidence of a remaining behavioural gap than the mere existence of
four project entries. Next work must address progression from accepted project to
tested artifact and evaluate recovery, not inflate task or memory counters.

N02-M60 investigation at approximately 18:20 UTC showed an additional confound:
King was actively decoding at roughly 8 tokens/s. One completed request consumed all
6,144 output tokens and took 15m55s overall. Methodologist consumed 2,048 tokens at
about 12.8 tokens/s. Proxy logs showed a disconnected client's request continuing
until completion and only then logging `Broken pipe`. Thus restarting the resident
does not reliably stop remote GPU work; successive commissioning restarts can leave
work queued. The exact King output for the final treatment cannot be attributed
until its matching response completes. No remote model/container was restarted to
hide this issue, and no model settings were changed.

## Remaining priorities (not quietly presented as done)

1. Fix/test upstream cancellation propagation or implement a graceful per-agent
   drain before future rollouts. The current installer is backed up and reversible,
   but not a zero-waste inference drain. Then observe a stable interval; review
   actual artifacts and malformed-command recovery.
   Budget exhaustion on King/Methodologist may persist. Do not silently alter the
   operator's output/context/thinking settings to improve a metric.
2. Add durable directed-message acknowledgement and project dependencies if missed
   requests remain a bottleneck. Current Board tail is deliberately bounded.
3. Implement real Chroma/Neo4j adapters only with namespace, rebuild, retry, deletion
   and idempotency tests; a running database does not establish memory utility.
4. Add independent artifact verification and a passive experiment report; the
   current dashboard index is not an intelligence benchmark.
5. Audit authority revocation: the source GPU revoke path removes the custom group,
   while `video`/`render` membership may remain. Do not claim effective isolation
   until device access is tested. No blanket sudo should be introduced.
6. Audit public contact authentication/session hardening independently (CSRF,
   rate limiting, Secure cookie/proxy behavior and public/private message scope).
   The public dashboard and contact service were deliberately not redeployed here.
7. Reconcile host checkout changes and replace ad-hoc deploys with versioned releases,
   canary validation, rollback tests and consistent deployed-version reporting.
8. Baseline/full-disk restart, concurrent task claims across separate Unix users,
   Ollama cancellation and GPU/container access deserve host integration tests.
   Unit tests and an active service are not substitutes for those checks.

Observation cannot be literally free of influence: local collection consumes some
CPU/I/O. “Passive” here means no observer-generated prompts, rewards or Board posts;
collectors should be bounded and measured, ideally exporting to external resources.

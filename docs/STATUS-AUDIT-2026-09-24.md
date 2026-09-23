# Scope audit — 24 September 2026

This audit compares the approved conversation requirements with repository code
and checks on N06-M10. A running systemd unit is not proof of a working society.
The observatory release accompanies this audit; runtime verification is recorded
in the deployment report below. No claim that all previous commitments are done.

## Material outstanding requirements

| Requirement | Evidence / actual state | Remaining work |
|---|---|---|
| Event-driven, asynchronous residents | `agent-runner` still has `while true`, blocking curl, blocking shell execution, and `sleep`. Host cadence is 120 seconds. Separate services do run concurrently, without a global round barrier. | Persistent inbox, wakeups, separate action worker, queue/recovery, bounded concurrency. |
| Free-form agent communication | This release removes forced Ollama JSON format, accepts prose as Board messages, and validates explicit action blocks. Legacy JSON actions remain compatible. | Directed delivery, acknowledgements and durable conversation threads are absent. |
| Task claiming and coordination | Board messages, proposals and founding task documents exist. No atomic claim/complete/yield workflow. | Task lifecycle, leases, recovery and evidence-based completion. |
| Effective loop prevention | Exact consecutive command repeat blocker exists; messages reset the counter. Failure counter is stored but no escalation after three failures is implemented. | Semantic recurrence detection, cross-cycle recovery; observatory reports repeats but does not intervene. |
| Container access for every agent | `resident` users are not granted `ai-village-containers`; Podman is group-restricted. `village-explorer` cannot execute it in the live check. | Grant intended baseline permissions without weakening Unix separation; verify rootless builds. |
| Unrestricted use of /mnt data disks | Actual mounts: `/mnt/hdd1`, `/mnt/hdd2`, `/mnt/ssd-data`. `sudo -u village-explorer test -w` fails at all three mount roots. | Apply and verify shared ACLs/default ACLs on the intended storage, preserving existing data. No recursive ownership rewrite performed. |
| Local M10 GPU use by rootless containers | GPUs and user video/render memberships exist; `nvidia-ctk cdi list` reports **0 CDI devices**. | Generate CDI, validate driver/runtime compatibility and real rootless GPU execution. |
| King can revoke GPU access | Authority only removes `ai-village-gpu`, while video/render access remains. Group-only revocation does not guarantee effective device revocation. | Capability enforcement and revoke tests across running workloads. |
| KV-cache awareness and long-term knowledge | Context capacity is in identity prompts; externalization/GraphRAG is suggested. There is no accurate live cache-occupancy signal, retrieval service or tested memory pipeline. | Prompt-token accounting, context-budget feedback, persistent summaries and retrieval. ChromaDB/Neo4j were an optional agent-built approach, not mandatory infrastructure. |
| Individual strengths/weaknesses across the whole simulation | New UI reports execution outcomes and repeats from a bounded recent log window. | Full-history indexing, task taxonomy, evaluations, provenance and uncertainty; exitcode success must not be labelled task success. |
| Research-grade non-reactive observation | UI only reads; telemetry is not included in prompts. Collector/instrumentation still consume resources and are on the host. Shared Unix group can read telemetry. | External collection/analysis, observer access separation, overhead measurements, experimental controls and reproducible protocols. Zero physical influence is not achievable. |
| Autonomous descendants and model training | Charter, lineage directory and proposals exist. | No functioning creation/training/evaluation/retirement pipeline has been verified. Prompts do not constitute artificial consciousness. |
| Updates and reboot continuity | systemd startup and cron reboot hook installed/enabled; update script/timer exist. `AUTO_UPDATE=false`, `AUTO_REBOOT=false` in production. | Automatic maintenance is disabled by configuration; controlled reboot acceptance test remains unperformed. |
| Inter-colony UDP discovery | README describes a future fixed UDP port. | No implemented transport, peer discovery or visit mechanism. |
| Literature/TRON research and novel research question | README contains research intentions. | No verified literature review, preregistration, novelty assessment or comparison experiments in this repository. |

## Implemented foundations

- Host `.env` drives nine endpoint/model/context configurations. Its hash is
  checked before and after this deployment; manual contexts are preserved.
- Individual Unix users, systemd units and separate model/temperament/focus
  identity prompts exist. No agent sudo grant is added.
- Public signal outbox, human contact form, Wikipedia helper and proposals exist.
  This is implementation evidence, not proof of successful agent use of each tool.
- External Ollama inference on N02-M60 and four local M10 experiment GPUs on
  N06-M10 are distinct. The UI labels those resources separately.
- Ollama load/unload helper, restart persistence, and basic telemetry were already
  present before this release.

## Observatory release scope and limits

- Local HTML/CSS/JavaScript/SVG, no CDN, webfont, analytics or browser internet calls.
- Navigation: overview, residents, habitat, event history and signals/contact.
- Hardware map from a sanitized lshw inventory, memory/load/mount time series,
  local GPU utilization/processes, and remote Ollama model residence.
- Process ownership uses UID/SubUID; this identifies the current Unix owner, not
  a forensic proof of who originally requested a shared service.
- Container map uses live libpod cgroups, with observed reciprocal TCP edges only
  in the host namespace. Stopped containers, image build history and isolated
  namespace traffic are not fully inventoried.
- Execution success/failure, rejected decisions, blocks and exact-normalized
  repeated commands are distinct. Statistics use at most 500 Board and 500
  inference events, bounded to 1 MiB per source; no lifetime percentages claimed.
- Inference start means a request is outstanding, not measured GPU work. Stale
  snapshots and unpaired long requests are visibly marked unknown.
- Resource history has 1/6/24-hour selectors, sampled to at most 240 points.
  Older snapshots lack new fields and are shown as missing rather than zero.
- Seven-day SQLite snapshot pruning and bounded rotating raw snapshot log. Agent
  event/Board retention and long-term aggregation remain open.
- Telemetry writes are best effort and non-blocking in the runner. Collector is
  read-only to the system under systemd, with a writable telemetry directory;
  root permits process attribution, not actions from the browser.

## Corrected earlier completion claims

Prior reports that all open features were implemented or that merely copying
the bootstrap activated every change were too broad. In particular, asynchronous
execution, general container/storage access and GPU CDI were not delivered.
This release fixes the UI and the reported forced-JSON fault. It does not silently
claim those architecture and permissions gaps are now resolved.

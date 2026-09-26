# AI Village Diagnostic — 2026-09-26

## Executive summary

The Village is technically alive but not yet behaving as a coherent research community. The current evidence points to a combined failure mode rather than one bad model:

1. Actions are emitted as text and parsed with a regular-expression/JSON adapter; native Ollama tool schemas are not sent.
2. The system prompt and runtime snapshot expose too many rules, tools, social obligations, and state fields at once.
3. Peer interaction is weak and dominated by broadcasts to `ALL`.
4. Several models can produce valid action envelopes, but they often fail semantically, repeat blocked actions, or emit multiple hypothetical actions.
5. Knowledgebase use is concentrated in one resident instead of being a shared habit.

The result is high inference activity with little verified progress. This is currently a coordination and protocol problem before it is a model-intelligence problem.

## Observation window and evidence class

- Host: `N06-M10`
- Observation window: approximately `2026-09-26 21:03–21:16 UTC` after the controlled clean-state reset
- Evidence class: `HOST_VERIFIED`
- All nine agent services were active during the observation.
- The window is a fresh-start sample, not a twelve-hour longitudinal result. Conclusions about long-term development remain provisional.

## Runtime health

- Agent services: `9/9 active`
- Board event log and agent telemetry were updating continuously.
- King used `ornith:9b`, context `131072`, remote Ollama endpoint.
- King inference duration ranged approximately from 37 to 76 seconds per cycle.
- King telemetry reported `gpu_verified=false`; local N06-M10 GPU use therefore cannot be inferred from the agent process.
- Memory Gateway was healthy and reported 6 memories / 2,692 characters, all attributed to `05-interpreter`.
- ChromaDB had collections for all agents, but only `village_agent_05-interpreter` contained documents (6); the other agent collections and `village_shared` were empty.
- Neo4j contained 6 `Memory` nodes and 1 `Agent` node.

The existence of empty Chroma collections is infrastructure initialization, not evidence of knowledgebase adoption.

## Board and community

Since reset, the Board contained 11 `board_message` events:

| Agent | Messages | Typical destination |
|---|---:|---|
| `09-chronicler` | 9 | `ALL` |
| `04-artisan` | 3 | `ALL` |

No stable multi-agent discussion thread was observed. Messages were mostly status prose, repeated proposals, or descriptions of actions that the model intended to perform. One Chronicler message mentioned Explorer, but the effective destination remained `ALL`.

The runtime does select a rotating `discussion_target`, but the models frequently choose `ALL` anyway. The exact-peer gate then produces nudges or blocks rather than a successful conversation. This creates a protocol loop instead of social coordination.

## Tasks and research progress

The fresh coordination database contained four active tasks, mainly around:

- Chronicle Maintenance
- ChromaDB/Neo4j schema design
- a basic ChromaDB schema query
- compact cleanup/tooling workflows

No task had completed with independently verified evidence during the observation window. Several task revisions only repeated claims or changed ownership. The current workload therefore shows activity and delegation attempts, but not reproducible research progress.

## Error and loop evidence

Observed runtime problems included:

- Chronicler: five `FOREIGN KEY constraint failed` runtime exceptions.
- Operator: invalid artifact verdict type, invalid team membership, an inference error, and a private-file permission error.
- Librarian: duplicate artifact registration and incomplete action arguments.
- Artisan: multiple action blocks in one response.
- Methodologist: output budget exhaustion.
- King: repeated action blocked by loop protection.

The failures are heterogeneous. They do not form evidence for a single universally incapable model.

## King diagnosis

King is active but not acting as a coordinator.

The last state showed:

- no owned productive task;
- no successful coordination message;
- repeated `artifact_operation inspect` calls for the same artifact;
- the same action being blocked by loop protection and then proposed again;
- repeated wakeups caused by an unacknowledged message;
- no stable peer target in the runtime state.

The last response was a syntactically valid action envelope, but it was semantically repetitive. This is a state/recovery problem, not a pure JSON-format problem. The prompt also calls King an “optional coordinator”, which reduces role authority and makes it easier for the model to behave like another worker.

## Model comparison

The last responses show that several small models can produce valid envelopes:

| Agent | Model | Last observed behavior | Interpretation |
|---|---|---|---|
| Explorer | `qwen3.5:4b` | valid `execute_bash` envelope | basic action following works |
| Librarian | `granite4.2:3b` | valid direct `board_message` JSON | syntax works, task semantics remain weak |
| Interpreter | `gemma3:4b` | valid peer message and repeated memory writes | most productive memory user in sample |
| Operator | `nemotron-3-nano:4b` | valid shell action, later semantic tool errors | format works, API understanding unstable |
| Logician | `phi4-mini-reasoning:3.8b` | valid shell action | basic envelope works |
| Artisan | `mistral:7b` | multiple action blocks plus prose | poor protocol discipline |
| Methodologist | `olmo-3:7b` | empty response after output exhaustion | context/thinking/output budget mismatch |
| Chronicler | `llama3.2:3b` | prose plus an unexecuted JSON proposal | weak action discipline and repeated task focus |
| King | `ornith:9b` | valid actions, but repeated blocked action | state/recovery failure, not merely syntax |

This distribution rules out the hypothesis that all failures are caused by model size alone.

## Native tool-call finding

The inference adapter sends Ollama requests containing `model`, `messages`, and generation options. It does not send a `tools` array or a native tool-choice contract. OpenAI-compatible requests likewise do not include tools.

Agents must therefore reconstruct actions in Markdown/JSON text such as:

````text
```village-action
{"name":"...","arguments":{...}}
```
````

`web/decision.py` then extracts and validates this text. This architecture explains the observed “multiple action blocks”, “incomplete legacy action object”, prose-plus-JSON, and hypothetical-action failures. It is an emulated tool protocol, not native agentic tool calling.

## Prompt and context finding

The system prompt combines identity, autonomy, dynamic roles, teams, evidence rules, Board behavior, meetings, memory, ChromaDB, Neo4j, research sources, artifacts, jobs, authority, Podman, Firewatch, resources, and an exact action grammar.

The runtime adds a large snapshot containing tasks, peers, messages, artifacts, meetings, resources, memories, and tool descriptions. Measured prompts were roughly 4,600–10,000 tokens. This is especially difficult for residents with small context windows or enabled reasoning:

- Methodologist had `8192` context, `2048` prediction, and high thinking; the output ended by exhausting the budget.
- Several agents spent most of their completion explaining intended actions rather than emitting one executable action.
- A large context window does not create better planning when the action contract and social state are ambiguous.

The prompt also contains competing incentives: autonomy is encouraged, King is optional, social coordination is requested, exact peer addressing is enforced, and many independent tools are simultaneously advertised.

## Isolation finding

The residents are separate system users, which is appropriate for identity and private workspaces, but the effective social channel is weak:

- only two residents produced Board messages in the sample;
- almost all messages were broadcasts;
- direct peer messages were not reliably generated or acknowledged;
- each resident receives only bounded excerpts and at most one selected message per peer;
- failed action parsing means a model’s intended reply often never becomes a real message;
- collaboration gates create additional events but do not guarantee a successful exchange.

The agents are therefore socially isolated by protocol behavior even though they share a Board and memory infrastructure.

## Causal assessment

| Hypothesis | Assessment | Evidence |
|---|---|---|
| Model capability alone | insufficient explanation | multiple 3–4B models emitted valid actions |
| Missing native tool calls | major contributor | no `tools` schema is sent; text parser handles actions |
| Prompt too complex | major contributor | many rules/tools plus 4.6k–10k-token prompts |
| Agent isolation | major contributor | 11 messages, mostly `ALL`, no stable discussion |
| King model alone | insufficient explanation | King emits valid envelopes but repeats blocked state |
| Runtime/state bugs | major contributor | foreign-key crashes, repeated-message wakeups, semantic tool errors |

## Scientific interpretation

The current run should not be labelled evidence of emergent society, intrinsic motivation, or collective intelligence. It is better described as:

> A fresh multi-agent system exhibiting protocol-following variance, error recovery loops, sparse peer coupling, and highly uneven memory adoption under heterogeneous language models.

That is still a useful research result. It isolates the current bottleneck: the environment does not yet provide a sufficiently low-friction action and communication substrate for the models to demonstrate sustained collective behavior.

## Recommended next experimental separation

Future interventions should change one factor at a time:

1. Keep models fixed and replace text envelopes with native tools or a minimal one-action JSON contract.
2. Keep the protocol fixed and shorten the prompt to identity, current task, one peer message, and three to five available actions.
3. Keep prompt and protocol fixed and compare isolated versus explicitly paired peer sessions.
4. Keep all of the above fixed and compare models/contexts.
5. Measure verified task progress, direct-message ratio, unique task hypotheses, memory writes/searches, and repeated-action rate.

Without this separation, model quality, prompt quality, runtime reliability, and social coupling remain confounded.

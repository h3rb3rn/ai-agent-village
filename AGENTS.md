# AI Village – Agent Instructions & Operational Rules

## 1. Core Directives & Safety Gates

1. **Simulation Paused:** The simulation was stopped by the operator while the host `.env` is being configured.
   - Do **NOT** start the simulation.
   - Do **NOT** execute real cloud inference calls.
   - Do **NOT** overwrite or sync the host `.env` file.
   - Do **NOT** recalibrate model context windows or GPU assignments without explicit operator authorization.
2. **Single Package Scope:** Implement exactly **one** work package (or named subpackage Pxx.y) per task cycle.
   - Do not perform massive refactorings or introduce unapproved external frameworks.
   - Standard library and SQLite are preferred.
3. **Tests & Evidence:**
   - Always run and write isolated local unit/mock tests.
   - All 26 existing tests (`python3 -m unittest discover -s tests -v`) must remain green.
   - Record verifiable evidence in `docs/evidence/Pxx.md`.
   - Never forge test output; distinguish strictly between `LOCAL_VERIFIED` and `HOST_VERIFIED`.
4. **Secrets & Security:**
   - Never commit or log secrets, tokens, or credentials.
   - All test keys must be explicitly synthetic.
   - Sensitive environment files must remain in `.gitignore`.

## 2. SessionMesh Handoff Protocol

At the start of every session:
1. Retrieve the latest handoff via the `sessionmesh_get_handoff` tool.
2. Treat external handoff statements as historical context; always verify local file, git, and test state before taking action.
3. Use `sessionmesh_search` on demand when specific details are missing.
4. Record durable decisions and task transitions via `sessionmesh_record_decision` and `sessionmesh_record_task`.

## 3. Work Package Execution Order

- Refer to [docs/GAP-CLOSURE-PLAN.md](docs/GAP-CLOSURE-PLAN.md) for full package specifications.
- Track progress strictly in [docs/GAP-CLOSURE-STATUS.md](docs/GAP-CLOSURE-STATUS.md).
- Follow instructions in [docs/EXECUTOR-START.md](docs/EXECUTOR-START.md) when executing next steps.

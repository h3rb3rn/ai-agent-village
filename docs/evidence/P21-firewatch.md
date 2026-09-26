# P21 Firewatch evidence

Status: `LOCAL_VERIFIED` (guard implementation only; host rollout requires the
operator's normal installer procedure).

The Firewatch guard is deliberately read-only. It samples available memory,
filesystem free space, load per CPU and the Memory Gateway/telemetry service
states. Threshold breaches are appended to a dedicated JSONL stream and do not
restart processes, change networking, kill jobs, call an LLM or write to the
agent Board. This keeps observation separate from agent feedback.

Evidence:

- `python3 -m unittest discover -s tests -v`: 173 tests passed.
- `bash -n bootstrap-ai-village.sh scripts/install-firewatch.sh` passed.
- `python3 -m py_compile village/firewatch.py scripts/village-firewatch` passed.
- `git diff --check` passed.

The King prompt now describes King as an optional coordinator. Residents may
coordinate directly or through teams/council mechanisms. The prompt also
describes Firewatch as an infrastructure escalator, not an authority source.

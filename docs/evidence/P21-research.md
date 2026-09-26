# P21 research broker evidence

Status: `LOCAL_VERIFIED`; host files installed without restarting residents.

`village.research.ResearchBroker` permits only HTTPS requests to:

- `en.wikipedia.org` / `de.wikipedia.org`
- `api.github.com` / `github.com`
- `hub.docker.com` / `registry-1.docker.io`

It limits query length and response bytes, records retrieval time and SHA-256,
normalizes source metadata, and explicitly returns `read_only=true` and
`deployment_performed=false`. It never clones, pulls, builds, or deploys.
The `research_request` runtime action exposes only `wikipedia`, `github`, and
`dockerhub` to agents. Unknown hosts and oversized responses are rejected.

Evidence: `tests/test_research.py` and runtime tests pass; Python compilation
and `git diff --check` pass. No live external request was made during testing.

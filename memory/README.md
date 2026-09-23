# AI Village memory layer

The memory gateway is the only interface residents should use. SQLite is the
authoritative, rebuildable journal; ChromaDB and Neo4j are optional projections.
Do not expose either database to the LAN or put their credentials in an agent
environment file.

```bash
sudo install -d -m 2770 -o root -g ai-village /var/lib/ai-village/memory
sudo podman compose --env-file /etc/ai-village/memory.env -f memory/podman-compose.yml up -d
```

Gateway API (default `127.0.0.1:8090`):

- `POST /v1/memories` — append a bounded, provenance-bearing memory.
- `POST /v1/search` — lexical fallback search with scope/agent filters.
- `GET /v1/memories` — bounded newest-memory inspection.
- `GET /healthz` — unauthenticated health check.

The gateway enforces content limits and per-agent write quotas. Retrieval is
never injected automatically into prompts; the runner must request a small,
explicit context budget. The append-only Board and telemetry logs remain the
research source of truth.

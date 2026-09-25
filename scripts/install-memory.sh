#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'run as root' >&2; exit 1; }
root="${VILLAGE_ROOT:-/var/lib/ai-village}"
install -d -m 2770 -o root -g ai-village "$root/memory"
install -m 0755 memory/gateway.py /usr/local/lib/ai-village/memory-gateway.py
install -d -m 0755 /usr/local/lib/ai-village/memory
install -m 0644 memory/projection.py memory/chroma_adapter.py memory/neo4j_adapter.py /usr/local/lib/ai-village/memory/
install -m 0755 scripts/memory-projection-worker.py /usr/local/lib/ai-village/memory-projection-worker.py
cat > /etc/systemd/system/ai-village-memory-gateway.service <<UNIT
[Unit]
Description=AI Village bounded memory gateway
After=network.target
[Service]
Type=simple
User=village-web
Group=ai-village
Environment=VILLAGE_ROOT=$root
Environment=MEMORY_BIND=127.0.0.1
Environment=MEMORY_PORT=8090
Environment=MEMORY_AGENT_TOKENS_FILE=/etc/ai-village/memory-agent-tokens.json
ExecStart=/usr/bin/python3 /usr/local/lib/ai-village/memory-gateway.py
Restart=on-failure
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$root/memory
NoNewPrivileges=true
[Install]
WantedBy=multi-user.target
UNIT
install -m 0644 deployment/ai-village-memory-projection.service /etc/systemd/system/ai-village-memory-projection.service
systemctl daemon-reload
systemctl enable --now ai-village-memory-gateway.service ai-village-memory-projection.service

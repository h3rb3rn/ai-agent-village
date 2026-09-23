#!/usr/bin/env bash
set -Eeuo pipefail
[[ $EUID -eq 0 ]] || { echo 'run as root' >&2; exit 1; }
root="${VILLAGE_ROOT:-/var/lib/ai-village}"
install -d -m 2770 -o root -g ai-village "$root/memory"
install -m 0755 memory/gateway.py /usr/local/lib/ai-village/memory-gateway.py
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
systemctl daemon-reload
systemctl enable --now ai-village-memory-gateway.service

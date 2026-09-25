#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
install -d -m 0755 /usr/local/lib/ai-village/village
install -m 0644 "$ROOT/village/firewatch.py" /usr/local/lib/ai-village/village/firewatch.py
install -m 0755 "$ROOT/scripts/village-firewatch" /usr/local/lib/ai-village/village-firewatch
install -m 0644 "$ROOT/deployment/ai-village-firewatch.service" /etc/systemd/system/ai-village-firewatch.service
install -d -m 2770 -o root -g ai-village /var/lib/ai-village/telemetry
touch /var/lib/ai-village/telemetry/firewatch.jsonl
chown root:ai-village /var/lib/ai-village/telemetry/firewatch.jsonl
chmod 0660 /var/lib/ai-village/telemetry/firewatch.jsonl
systemctl daemon-reload
systemctl enable --now ai-village-firewatch.service
systemctl is-active --quiet ai-village-firewatch.service
echo "Firewatch installed and active (read-only; alerts only)."

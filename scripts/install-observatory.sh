#!/usr/bin/env bash
# Targeted installation; no apt run, no rewrite of .env, users or mount permissions.
set -Eeuo pipefail
[[ $EUID == 0 ]] || { echo 'Run as root'; exit 1; }
release="${1:?usage: install-observatory.sh RELEASE_DIR [--runner]}"
[[ -f "$release/observatory.html" && -f "$release/webui.py" && -f "$release/observer.py" ]] || exit 2
source /etc/ai-village/village.env
VILLAGE_ROOT="${VILLAGE_ROOT:-/var/lib/ai-village}"
backup="/var/backups/ai-village/observatory-$(date +%Y%m%dT%H%M%S)"
install -d -m 0700 "$backup"
cp -a /usr/local/lib/ai-village "$backup/lib"
cp -a /usr/local/share/ai-village "$backup/share"
cp -a /etc/systemd/system/ai-village-telemetry.service "$backup/telemetry.service"
sha256sum /opt/ai-agent-village/.env > "$backup/env.sha256"
install -d -m 0755 /usr/local/share/ai-village/web
install -m 0644 "$release"/observatory.* /usr/local/share/ai-village/web/
install -m 0644 "$release/observer.py" /usr/local/lib/ai-village/observer.py
install -m 0755 "$release/webui.py" "$release/telemetry-collector.py" /usr/local/lib/ai-village/
install -m 0644 "$release/ai-village-telemetry.service" /etc/systemd/system/ai-village-telemetry.service
sed -i "s|ReadWritePaths=/var/lib/ai-village/telemetry|ReadWritePaths=$VILLAGE_ROOT/telemetry|" /etc/systemd/system/ai-village-telemetry.service
systemctl daemon-reload
systemctl restart ai-village-webui.service ai-village-telemetry.service
if [[ "${2:-}" == --runner ]]; then
  install -m 0644 "$release/decision.py" /usr/local/lib/ai-village/decision.py
  install -m 0755 "$release/agent-runner" /usr/local/lib/ai-village/agent-runner
  install -m 0644 "$release/system-prompt.txt" /usr/local/share/ai-village/system-prompt.txt
  mapfile -t units < <(systemctl list-unit-files 'ai-village-agent-*.service' --no-legend --no-pager | awk '{print $1}')
  systemctl restart "${units[@]}"
fi
sha256sum -c "$backup/env.sha256"
printf 'Backup: %s\n' "$backup"
systemctl is-active ai-village-webui.service ai-village-telemetry.service

#!/usr/bin/env bash
# Targeted installation; no apt run, no rewrite of .env, users or mount permissions.
# Supports --dry-run and --no-start for safe verification and deployment.
set -Eeuo pipefail

dry_run=false
no_start=false
release=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=true; shift ;;
    --no-start) no_start=true; shift ;;
    --runner)
      echo 'Use scripts/install-runtime.py for runner changes; it also provisions memory access and shared tasks.' >&2
      exit 2 ;;
    *)
      if [[ -z "$release" ]]; then
        release="$1"
        shift
      else
        echo "Unknown argument: $1" >&2
        exit 1
      fi ;;
  esac
done

if [[ -z "$release" ]]; then
  echo "usage: install-observatory.sh RELEASE_DIR [--dry-run] [--no-start]" >&2
  exit 2
fi

is_structured=false
if [[ -f "$release/lib/webui.py" && -f "$release/lib/observer.py" && -f "$release/share/web/observatory.html" ]]; then
  is_structured=true
elif [[ ! (-f "$release/observatory.html" && -f "$release/webui.py" && -f "$release/observer.py") ]]; then
  echo "Release missing required files: observatory.html, webui.py, observer.py" >&2
  exit 2
fi

# Verify release manifest if present
if [[ -f "$release/release-manifest.json" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    python3 -m village.release verify "$release" || { echo "Manifest verification failed!" >&2; exit 1; }
  fi
fi

if [[ "$dry_run" == true ]]; then
  echo "Dry-run: release $release verified (structured=$is_structured). No files copied, no services restarted."
  exit 0
fi

[[ $EUID == 0 ]] || { echo 'Run as root'; exit 1; }

source /etc/ai-village/village.env
VILLAGE_ROOT="${VILLAGE_ROOT:-/var/lib/ai-village}"
backup="/var/backups/ai-village/observatory-$(date +%Y%m%dT%H%M%S)"
install -d -m 0700 "$backup"
if [[ -d /usr/local/lib/ai-village ]]; then cp -a /usr/local/lib/ai-village "$backup/lib"; fi
if [[ -d /usr/local/share/ai-village ]]; then cp -a /usr/local/share/ai-village "$backup/share"; fi
if [[ -f /etc/systemd/system/ai-village-telemetry.service ]]; then cp -a /etc/systemd/system/ai-village-telemetry.service "$backup/telemetry.service"; fi
if [[ -f /opt/ai-agent-village/.env ]]; then sha256sum /opt/ai-agent-village/.env > "$backup/env.sha256"; fi

install -d -m 0755 /usr/local/share/ai-village/web
install -m 0644 "$release"/observatory.* /usr/local/share/ai-village/web/
install -m 0644 "$release/observer.py" /usr/local/lib/ai-village/observer.py
install -m 0755 "$release/webui.py" /usr/local/lib/ai-village/webui.py
if [[ -f "$release/telemetry-collector.py" ]]; then
  install -m 0755 "$release/telemetry-collector.py" /usr/local/lib/ai-village/
fi
if [[ -f "$release/ai-village-telemetry.service" ]]; then
  install -m 0644 "$release/ai-village-telemetry.service" /etc/systemd/system/ai-village-telemetry.service
  sed -i "s|ReadWritePaths=/var/lib/ai-village/telemetry|ReadWritePaths=$VILLAGE_ROOT/telemetry|" /etc/systemd/system/ai-village-telemetry.service
fi

systemctl daemon-reload

if [[ -f "$backup/env.sha256" ]]; then
  sha256sum -c "$backup/env.sha256"
fi
printf 'Backup: %s\n' "$backup"

if [[ "$no_start" == true ]]; then
  echo "Installed successfully without starting or restarting services (--no-start)."
  exit 0
fi

systemctl restart ai-village-webui.service ai-village-telemetry.service
systemctl is-active ai-village-webui.service ai-village-telemetry.service

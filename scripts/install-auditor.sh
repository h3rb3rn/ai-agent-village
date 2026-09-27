#!/usr/bin/env bash
set -euo pipefail

# Installs the auditor as a systemd service and provisions its memory-gateway
# identity, but deliberately does NOT start it - unlike install-firewatch.sh,
# this service actively intervenes (direct inbox messages to a resident, or a
# shared-memory write visible to all of them), so going live is left as one
# explicit, separately-reviewable step for the operator:
#   systemctl start ai-village-auditor.service

if [[ ${EUID} -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

install -d -m 0755 /usr/local/lib/ai-village/village
install -m 0644 "$ROOT/village/auditor.py" /usr/local/lib/ai-village/village/auditor.py
install -m 0644 "$ROOT/village/auditor_llm.py" /usr/local/lib/ai-village/village/auditor_llm.py
install -m 0755 "$ROOT/scripts/village-auditor" /usr/local/lib/ai-village/village-auditor
install -m 0644 "$ROOT/deployment/ai-village-auditor.service" /etc/systemd/system/ai-village-auditor.service

install -d -m 2770 -o root -g ai-village /var/lib/ai-village/telemetry
: > /var/lib/ai-village/telemetry/audit.sqlite3
chown root:ai-village /var/lib/ai-village/telemetry/audit.sqlite3
chmod 0660 /var/lib/ai-village/telemetry/audit.sqlite3

install -d -m 0700 /etc/ai-village/credentials
GATEWAY_URL="${MEMORY_GATEWAY_URL:-http://127.0.0.1:8090}"
python3 - "$GATEWAY_URL" <<'PY'
import json
import secrets
import sys
from pathlib import Path

gateway_url = sys.argv[1]
token_path = Path("/etc/ai-village/memory-agent-tokens.json")
tokens = json.loads(token_path.read_text()) if token_path.exists() else {}
token = tokens.get("village-auditor") or secrets.token_urlsafe(36)
tokens["village-auditor"] = token
token_path.write_text(json.dumps(tokens))
token_path.chmod(0o600)

credential = Path("/etc/ai-village/credentials/village-auditor.env")
credential.write_text(f"MEMORY_AGENT_TOKEN={token}\nMEMORY_GATEWAY_URL={gateway_url}\n")
credential.chmod(0o600)
PY

systemctl daemon-reload
systemctl enable ai-village-auditor.service
echo "Auditor installed and enabled, but NOT started."
echo "Review docs/evidence/P36.md, then go live with:"
echo "  systemctl start ai-village-auditor.service"

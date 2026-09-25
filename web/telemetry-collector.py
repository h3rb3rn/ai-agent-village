#!/usr/bin/env python3
"""Passive AI Village telemetry collector. It never writes to the Board or prompts."""
import json, os, sqlite3, subprocess, time, urllib.request
from observer import Resources, hardware
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get("VILLAGE_ROOT", "/var/lib/ai-village"))
OUT = ROOT / "telemetry"
DB = OUT / "events.sqlite3"
LATEST = OUT / "latest.json"
RAW = OUT / "events.jsonl"
AGENTS = Path("/etc/ai-village/agents")
INTERVAL = max(5, int(os.environ.get("VILLAGE_TELEMETRY_INTERVAL_SECONDS", "15")))

def now(): return datetime.now(timezone.utc).isoformat()
def envfile(path):
    values = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1); values[key] = value.strip().strip('"')
    except OSError: pass
    return values
def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=4) as response:
            return json.loads(response.read().decode())
    except Exception as exc:
        return {"error": str(exc)}
def active(unit):
    try:
        return subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", unit], capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception: return "unknown"
def memory_status():
    result = {'gateway': {'service': active('ai-village-memory-gateway.service'), 'url': 'http://127.0.0.1:8090'}, 'chroma': {'service': 'not-configured'}, 'neo4j': {'service': 'not-configured'}}
    for name, port, probe_url in (('chroma', 8000, 'http://127.0.0.1:8000/api/v2/heartbeat'), ('neo4j', 7687, 'http://127.0.0.1:7474')):
        try:
            with urllib.request.urlopen(probe_url, timeout=2) as response:
                result[name] = {'service': 'healthy', 'port': port, 'http_status': response.status}
        except Exception: result[name] = {'service': 'unknown', 'port': port}
    try:
        with urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2) as response:
            result['gateway']['health'] = json.loads(response.read().decode()).get('ok', False)
    except Exception: result['gateway']['health'] = False
    try:
        with urllib.request.urlopen('http://127.0.0.1:8090/v1/stats', timeout=2) as response:
            result['stats'] = json.loads(response.read().decode())
    except Exception: result['stats'] = {'agents': [], 'total': 0, 'chars': 0}
    return result
def gpu():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,memory.used,memory.total,utilization.gpu,power.draw", "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5)
        return {"available": out.returncode == 0, "rows": [line.strip() for line in out.stdout.splitlines() if line.strip()]}
    except Exception as exc: return {"available": False, "error": str(exc), "rows": []}
def habitat():
    memory = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, value = line.split(':', 1); memory[key] = int(value.split()[0]) * 1024
    except (OSError, ValueError): pass
    mounts = []
    try:
        entries = json.loads(subprocess.run(['findmnt', '--json', '--list', '--output', 'TARGET,SOURCE,FSTYPE'], capture_output=True, text=True, timeout=4).stdout)['filesystems']
        for item in entries:
            target = item['target']
            if target not in ('/', str(ROOT), '/mnt') and not target.startswith('/mnt/'): continue
            stats = os.statvfs(target)
            mounts.append({'path': target, 'source': item['source'], 'total': stats.f_blocks * stats.f_frsize, 'available': stats.f_bavail * stats.f_frsize, 'used': (stats.f_blocks - stats.f_bfree) * stats.f_frsize})
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired): pass
    return {'hostname': os.uname().nodename, 'load': list(os.getloadavg()), 'cpus': os.cpu_count(), 'memory_total': memory.get('MemTotal'), 'memory_available': memory.get('MemAvailable'), 'mounts': mounts}
def main():
    OUT.mkdir(parents=True, exist_ok=True)
    inventory = hardware(); resources = Resources()
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, payload TEXT NOT NULL)")
    db.execute("CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots(timestamp)")
    db.commit()
    while True:
        stamp = now(); agents = []
        for path in sorted(AGENTS.glob("*.env")):
            values = envfile(path); agent_id = path.stem; name = values.get("AGENT_NAME", agent_id); url = values.get("OLLAMA_URL", "")
            api_type = values.get("API_TYPE", "ollama").lower()
            # P05/P06: OpenAI endpoints do not have /api/ps. Do not treat missing table as unloaded models.
            if api_type == "openai":
                ps = {"models": [], "status": "not_applicable"}
                ollama_error = "openai_provider_no_vram_table"
            else:
                ps = get_json(url.rstrip("/") + "/api/ps") if url else {"error": "missing endpoint"}
                ollama_error = ps.get("error") if isinstance(ps, dict) else "invalid response"
            models = ps.get("models", []) if isinstance(ps, dict) else []

            # P06: Inspect active inference request state and reconcile inactive services
            active_req_file = ROOT / "users" / name / "active_request.json"
            inf_info = {}
            if active_req_file.exists():
                try:
                    inf_info = json.loads(active_req_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            svc_active = active("ai-village-agent-" + agent_id + ".service")
            if inf_info.get("state") in ("queued", "requesting") and svc_active != "active":
                inf_info["state"] = "unknown"
                inf_info["error_class"] = "service_inactive"

            agents.append({
                "id": agent_id,
                "name": name,
                "role": values.get("AGENT_ROLE", ""),
                "model": values.get("OLLAMA_MODEL", ""),
                "context": values.get("OLLAMA_NUM_CTX", ""),
                "endpoint": url,
                "provider": api_type,
                "service": svc_active,
                "ollama": models,
                "ollama_error": ollama_error,
                "inference": inf_info,
            })
        payload = {"timestamp": now(), "agents": agents, "gpu": gpu(), "host": habitat(), "hardware": inventory, "memory": memory_status(), "resources": resources.sample(agents)}
        encoded = json.dumps(payload, ensure_ascii=False)
        db.execute("INSERT INTO snapshots(timestamp,payload) VALUES (?,?)", (stamp, encoded)); db.commit()
        if RAW.exists() and RAW.stat().st_size > 16 * 1024 * 1024:
            RAW.replace(OUT / 'events.previous.jsonl')
        with RAW.open("a", encoding="utf-8") as handle: handle.write(encoded + "\n")
        temporary = LATEST.with_suffix('.tmp')
        temporary.write_text(encoded + "\n", encoding="utf-8"); temporary.replace(LATEST)
        db.execute("DELETE FROM snapshots WHERE timestamp < ?", (datetime.fromtimestamp(time.time() - 7 * 86400, timezone.utc).isoformat(),)); db.commit()
        time.sleep(INTERVAL)
if __name__ == "__main__": main()

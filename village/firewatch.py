"""Read-only infrastructure guard for the AI Village."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from village.events import append_event


@dataclass(frozen=True)
class GuardThresholds:
    min_memory_available_mib: int = 1024
    min_disk_free_gib: int = 10
    max_load_per_cpu: float = 2.0


def _memory_available_mib() -> int | None:
    try:
        values = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            values[key] = int(value.strip().split()[0])
        return values.get("MemAvailable", 0) // 1024
    except (OSError, ValueError):
        return None


def _load_per_cpu() -> float | None:
    try:
        return os.getloadavg()[0] / max(1, os.cpu_count() or 1)
    except OSError:
        return None


def _service_active(name: str) -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", name],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=4, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def observe(*, thresholds: GuardThresholds = GuardThresholds(),
            disk_path: str = "/var/lib/ai-village",
            service_checker: Callable[[str], bool] = _service_active) -> dict:
    """Return one side-effect-free health observation and derived alerts."""
    memory = _memory_available_mib()
    disk = shutil.disk_usage(disk_path)
    disk_free_gib = disk.free / (1024 ** 3)
    load_per_cpu = _load_per_cpu()
    services = {
        "memory_gateway": service_checker("ai-village-memory-gateway.service"),
        "telemetry": service_checker("ai-village-telemetry.service"),
    }
    alerts = []
    if memory is not None and memory < thresholds.min_memory_available_mib:
        alerts.append({"severity": "critical", "code": "memory_pressure", "message": "available memory below threshold"})
    if disk_free_gib < thresholds.min_disk_free_gib:
        alerts.append({"severity": "critical", "code": "disk_pressure", "message": "disk free space below threshold"})
    if load_per_cpu is not None and load_per_cpu > thresholds.max_load_per_cpu:
        alerts.append({"severity": "warning", "code": "load_pressure", "message": "load average per CPU above threshold"})
    for service, active in services.items():
        if not active:
            alerts.append({"severity": "critical", "code": f"service_{service}_down", "message": f"{service} is inactive"})
    return {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "memory_available_mib": memory, "disk_free_gib": round(disk_free_gib, 2),
            "load_per_cpu": round(load_per_cpu, 3) if load_per_cpu is not None else None,
            "services": services, "thresholds": asdict(thresholds),
            "alerts": alerts, "mode": "read_only_escalation"}


def append_alerts(observation: dict, path: Path) -> int:
    """Append active alerts durably; never perform remediation."""
    alerts = observation.get("alerts", [])
    if not alerts:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    for alert in alerts:
        append_event(path, source='firewatch', kind='firewatch_alert',
                     detail=alert.get('message'), observation=observation,
                     alert=alert)
    return len(alerts)


def run(interval: float = 30.0, output: Path = Path("/var/lib/ai-village/telemetry/firewatch.jsonl")) -> None:
    while True:
        append_alerts(observe(), output)
        time.sleep(max(1.0, interval))

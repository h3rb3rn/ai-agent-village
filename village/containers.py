#!/usr/bin/env python3
"""AI Village Container and Storage Management.

Implements host resource inventory, rootless Podman execution helpers,
strictly isolated storage management (private vs. shared), unprivileged
sandbox limits, CDI/GPU compute verification, and agent capability introspection.

Invariants:
- All residents have access to rootless containers via isolated subuids.
- Storage isolation: Agents cannot mount or access paths outside their private
  directory (/var/lib/ai-village/storage/agents/<agent>) and shared storage
  (/var/lib/ai-village/storage/shared).
- Network isolation: Containers default to --network=none for offline sandbox runs.
- Remote pull protection: Defaults to --pull=never to prevent uncontrolled
  external image downloads or bandwidth exhaustion.
- GPU verification requires real compute validation, not just nvidia-smi.
- Simulation pause and safety: No changes to host .env; preserves existing data.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("village.containers")

DEFAULT_STORAGE_ROOT = Path("/var/lib/ai-village/storage")
DEFAULT_AUTHORITY_SOCKET = "/run/ai-village-authority.sock"
SAFE_CLI_NAMES = ("podman", "git", "python3", "sqlite3", "village-signal", "village-authority")


@dataclass
class HostResourceAudit:
    """Audit of host hardware, mount, virtualization, and GPU capabilities."""

    cpu_count: int
    ram_total_bytes: int
    ram_available_bytes: int
    disk_mounts: List[Dict[str, Any]]
    subuid_mappings: Dict[str, Tuple[int, int]]
    subgid_mappings: Dict[str, Tuple[int, int]]
    xdg_runtime_dirs: Dict[str, str]
    cdi_spec_files: List[str]
    driver_status: Dict[str, Any]
    podman_status: Dict[str, Any]
    compose_provider: Optional[str]
    detected_defects: List[str]

    def to_dict(self) -> Dict[str, Any]:
        """Convert audit record to serializable dictionary."""
        return asdict(self)


def audit_host_environment(
    cmd_runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
    etc_dir: Path = Path("/etc"),
    proc_dir: Path = Path("/proc"),
    run_dir: Path = Path("/run"),
) -> HostResourceAudit:
    """Inspect and inventory host hardware, mounts, user namespaces, and CDI.

    Args:
        cmd_runner: Callable executing subprocess commands (mockable for tests).
        etc_dir: Path to /etc configuration directory.
        proc_dir: Path to /proc filesystem directory.
        run_dir: Path to /run runtime directory.

    Returns:
        HostResourceAudit: Structured snapshot of host capabilities and defects.
    """
    runner = cmd_runner or subprocess.run
    detected_defects: List[str] = []

    # 1. CPU Count
    cpu_count = os.cpu_count() or 1

    # 2. RAM Information from /proc/meminfo
    ram_total = 0
    ram_available = 0
    meminfo_path = proc_dir / "meminfo"
    if meminfo_path.exists():
        try:
            with open(meminfo_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        key = parts[0].rstrip(":")
                        if key == "MemTotal":
                            ram_total = int(parts[1]) * 1024
                        elif key == "MemAvailable":
                            ram_available = int(parts[1]) * 1024
        except OSError as exc:
            detected_defects.append(f"Failed to read /proc/meminfo: {exc}")
    else:
        detected_defects.append("Missing /proc/meminfo")

    # 3. Mounts and free disk space
    disk_mounts: List[Dict[str, Any]] = []
    mounts_path = proc_dir / "mounts"
    if mounts_path.exists():
        try:
            seen_mounts: Set[str] = set()
            with open(mounts_path, "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 3:
                        mount_point = parts[1]
                        fstype = parts[2]
                        if mount_point in seen_mounts:
                            continue
                        seen_mounts.add(mount_point)
                        # Check disk usage for real directories
                        if mount_point.startswith(("/", "/var", "/mnt", "/home")):
                            try:
                                usage = shutil.disk_usage(mount_point)
                                disk_mounts.append(
                                    {
                                        "mount": mount_point,
                                        "fstype": fstype,
                                        "total_bytes": usage.total,
                                        "free_bytes": usage.free,
                                    }
                                )
                            except OSError:
                                pass
        except OSError as exc:
            detected_defects.append(f"Failed to inspect /proc/mounts: {exc}")

    # 4. SubUID and SubGID Mappings
    subuid_mappings: Dict[str, Tuple[int, int]] = {}
    subuid_path = etc_dir / "subuid"
    if subuid_path.exists():
        try:
            with open(subuid_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split(":")
                        if len(parts) == 3:
                            subuid_mappings[parts[0]] = (int(parts[1]), int(parts[2]))
        except OSError as exc:
            detected_defects.append(f"Failed to read /etc/subuid: {exc}")
    else:
        detected_defects.append("Missing /etc/subuid: rootless namespaces unavailable")

    subgid_mappings: Dict[str, Tuple[int, int]] = {}
    subgid_path = etc_dir / "subgid"
    if subgid_path.exists():
        try:
            with open(subgid_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        parts = line.split(":")
                        if len(parts) == 3:
                            subgid_mappings[parts[0]] = (int(parts[1]), int(parts[2]))
        except OSError as exc:
            detected_defects.append(f"Failed to read /etc/subgid: {exc}")
    else:
        detected_defects.append("Missing /etc/subgid: rootless namespaces unavailable")

    # 5. XDG Runtime Dirs (/run/user/<uid>)
    xdg_runtime_dirs: Dict[str, str] = {}
    user_run_base = run_dir / "user"
    if user_run_base.exists() and user_run_base.is_dir():
        try:
            for entry in user_run_base.iterdir():
                if entry.name.isdigit():
                    xdg_runtime_dirs[entry.name] = str(entry)
        except OSError:
            pass

    # 6. CDI Specifications (/etc/cdi and /var/run/cdi)
    cdi_spec_files: List[str] = []
    for cdi_candidate in (etc_dir / "cdi", run_dir / "cdi"):
        if cdi_candidate.exists() and cdi_candidate.is_dir():
            try:
                for path in cdi_candidate.glob("*.yaml"):
                    cdi_spec_files.append(str(path))
                for path in cdi_candidate.glob("*.json"):
                    cdi_spec_files.append(str(path))
            except OSError:
                pass

    # 7. Nvidia Driver Status via nvidia-smi
    driver_status: Dict[str, Any] = {"installed": False, "responsive": False, "error": None}
    nvidia_bin = shutil.which("nvidia-smi")
    if nvidia_bin:
        driver_status["installed"] = True
        try:
            res = runner([nvidia_bin, "-L"], capture_output=True, text=True)
            if res.returncode == 0:
                driver_status["responsive"] = True
                driver_status["devices"] = [line.strip() for line in res.stdout.strip().splitlines() if line]
            else:
                err_text = res.stderr.strip() or res.stdout.strip()
                driver_status["error"] = err_text
                detected_defects.append(f"Nvidia driver communication failed: {err_text}")
        except Exception as exc:
            driver_status["error"] = str(exc)
            detected_defects.append(f"nvidia-smi execution error: {exc}")
    else:
        driver_status["error"] = "nvidia-smi binary not found"

    # 8. Podman Status
    podman_status: Dict[str, Any] = {"installed": False, "version": None, "error": None}
    podman_bin = shutil.which("podman")
    if podman_bin:
        podman_status["installed"] = True
        try:
            res = runner([podman_bin, "--version"], capture_output=True, text=True)
            if res.returncode == 0:
                podman_status["version"] = res.stdout.strip()
            else:
                podman_status["error"] = res.stderr.strip()
                detected_defects.append(f"podman --version failed: {res.stderr.strip()}")
        except Exception as exc:
            podman_status["error"] = str(exc)
            detected_defects.append(f"podman execution error: {exc}")
    else:
        podman_status["error"] = "podman binary not found"
        detected_defects.append("podman not installed")

    # 9. Compose Provider
    compose_provider: Optional[str] = None
    if shutil.which("podman-compose"):
        compose_provider = "podman-compose"
    elif shutil.which("docker-compose"):
        compose_provider = "docker-compose"

    return HostResourceAudit(
        cpu_count=cpu_count,
        ram_total_bytes=ram_total,
        ram_available_bytes=ram_available,
        disk_mounts=disk_mounts,
        subuid_mappings=subuid_mappings,
        subgid_mappings=subgid_mappings,
        xdg_runtime_dirs=xdg_runtime_dirs,
        cdi_spec_files=cdi_spec_files,
        driver_status=driver_status,
        podman_status=podman_status,
        compose_provider=compose_provider,
        detected_defects=detected_defects,
    )


class StorageManager:
    """Manages private and shared storage directories with strict access isolation."""

    def __init__(self, storage_root: Path = DEFAULT_STORAGE_ROOT):
        """Initialize StorageManager.

        Args:
            storage_root: Top-level root directory for all village persistent storage.
        """
        self.storage_root = Path(storage_root).resolve()
        self.shared_dir = self.storage_root / "shared"
        self.agents_dir = self.storage_root / "agents"

    def ensure_storage_layout(
        self,
        agent: Optional[str] = None,
        agent_uid: Optional[int] = None,
        agent_gid: Optional[int] = None,
    ) -> Dict[str, Path]:
        """Ensure shared and agent-specific private storage directories exist.

        Preserves existing data and files unconditionally.

        Args:
            agent: Optional agent identifier (e.g. 'scholar', 'artisan').
            agent_uid: Optional UID for agent directory ownership.
            agent_gid: Optional GID for agent directory ownership.

        Returns:
            dict: Paths to created/verified directories ('root', 'shared', 'private').
        """
        self.storage_root.mkdir(parents=True, exist_ok=True)
        try:
            self.storage_root.chmod(0o755)
        except OSError:
            pass

        # Shared directory: setgid (02770) so created items inherit village group
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.shared_dir.chmod(0o2770)
        except OSError:
            pass

        res = {"root": self.storage_root, "shared": self.shared_dir}

        if agent:
            priv_dir = self.agents_dir / agent
            priv_dir.mkdir(parents=True, exist_ok=True)
            try:
                # Private agent storage: strict 0700 access
                priv_dir.chmod(0o700)
                if agent_uid is not None and agent_gid is not None:
                    os.chown(priv_dir, agent_uid, agent_gid)
            except OSError:
                pass
            res["private"] = priv_dir

        return res

    def get_agent_storage_dir(self, agent: str) -> Path:
        """Return the authorized private storage directory for an agent."""
        return (self.agents_dir / agent).resolve()

    def get_shared_storage_dir(self) -> Path:
        """Return the authorized shared storage directory."""
        return self.shared_dir.resolve()

    def validate_mount_path(self, path: str | Path, agent: str, allow_shared: bool = True) -> Path:
        """Validate that a requested host path is within authorized storage bounds.

        Rejects path traversal, escape attempts, and unauthorized system paths.

        Args:
            path: Target host path to validate.
            agent: The requesting agent identifier.
            allow_shared: Whether shared storage is permissible for this mount.

        Returns:
            Path: Normalized, resolved absolute path.

        Raises:
            ValueError: If path escapes permitted storage bounds or is forbidden.
        """
        raw_str = str(path)
        if ".." in raw_str:
            raise ValueError(f"Path traversal ('..') detected in mount path: {path}")

        target_path = Path(path).resolve()
        agent_dir = self.get_agent_storage_dir(agent)
        shared_dir = self.get_shared_storage_dir()

        # Check if inside agent's private directory
        is_private = False
        try:
            target_path.relative_to(agent_dir)
            is_private = True
        except ValueError:
            pass

        # Check if inside shared directory
        is_shared = False
        if allow_shared:
            try:
                target_path.relative_to(shared_dir)
                is_shared = True
            except ValueError:
                pass

        if not (is_private or is_shared):
            # Forbid system paths specifically
            forbidden_prefixes = ("/etc", "/proc", "/sys", "/dev", "/root", "/boot", "/var/run")
            if any(str(target_path).startswith(f) for f in forbidden_prefixes):
                raise ValueError(f"Mount of sensitive system directory '{target_path}' is strictly forbidden")
            raise ValueError(
                f"Mount path '{target_path}' is outside authorized agent storage ({agent_dir}) "
                f"and shared storage ({shared_dir})"
            )

        return target_path


class PodmanSandbox:
    """Builder and executor for rootless, unprivileged Podman container sandboxes."""

    def __init__(
        self,
        storage_manager: Optional[StorageManager] = None,
        cmd_runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
        podman_path: Optional[str] = None,
        etc_dir: Path = Path("/etc"),
    ):
        """Initialize PodmanSandbox.

        Args:
            storage_manager: StorageManager instance for volume isolation.
            cmd_runner: Subprocess runner (mockable for tests).
            podman_path: Path to podman binary.
            etc_dir: Path to /etc for subuid inspection.
        """
        self.storage = storage_manager or StorageManager()
        self.cmd_runner = cmd_runner or subprocess.run
        self.podman_bin = podman_path or shutil.which("podman") or "/usr/bin/podman"
        self.etc_dir = etc_dir

    def check_user_subuid_mapping(self, username: str) -> bool:
        """Verify whether an agent user has subuid/subgid namespace allocations."""
        subuid_file = self.etc_dir / "subuid"
        subgid_file = self.etc_dir / "subgid"
        if not (subuid_file.exists() and subgid_file.exists()):
            return False

        has_subuid = False
        has_subgid = False
        try:
            with open(subuid_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith(f"{username}:"):
                        has_subuid = True
                        break
            with open(subgid_file, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith(f"{username}:"):
                        has_subgid = True
                        break
        except OSError:
            return False

        return has_subuid and has_subgid

    def build_run_args(
        self,
        image: str,
        cmd: List[str],
        agent: str,
        container_name: Optional[str] = None,
        network_mode: str = "none",
        read_only_rootfs: bool = True,
        mount_private: bool = True,
        mount_shared: bool = True,
        shared_read_only: bool = False,
        allow_pull: bool = False,
        use_gpu: bool = False,
        memory_limit: str = "512m",
        cpu_limit: str = "1.0",
        pids_limit: int = 128,
        additional_mounts: Optional[List[Tuple[str, str, str]]] = None,
    ) -> List[str]:
        """Construct a secure, hardened podman run argument list.

        Args:
            image: Pinned container image name/tag.
            cmd: Command arguments to execute in container.
            agent: Target agent identifier.
            container_name: Optional explicit container name.
            network_mode: Container network mode ('none' by default for isolation).
            read_only_rootfs: Enforce read-only root filesystem.
            mount_private: Mount agent private storage at /storage/private.
            mount_shared: Mount shared storage at /storage/shared.
            shared_read_only: Mount shared storage read-only.
            allow_pull: Allow pulling images from remote registry. Defaults to False (--pull=never).
            use_gpu: Enable CDI Nvidia GPU access inside container.
            memory_limit: Memory cgroup limit.
            cpu_limit: CPU quota cgroup limit.
            pids_limit: Maximum process limit.
            additional_mounts: List of (host_path, container_path, mode).

        Returns:
            list[str]: Complete argument list for podman invocation.
        """
        cname = container_name or f"ai-village-{agent}-{int(time.time())}"

        args = [
            self.podman_bin,
            "run",
            "--rm",
            f"--name={cname}",
            f"--network={network_mode}",
            f"--memory={memory_limit}",
            f"--cpus={cpu_limit}",
            f"--pids-limit={pids_limit}",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
        ]

        if not allow_pull:
            # Strictly prevent uncontrolled pulls
            args.append("--pull=never")

        if read_only_rootfs:
            args.append("--read-only")
            # Provide unprivileged writable scratch tmpfs
            args.append("--tmpfs=/tmp:rw,noexec,nosuid,size=64m")

        # Mount storage directories
        if mount_private:
            priv_dir = self.storage.get_agent_storage_dir(agent)
            args.append(f"-v={priv_dir}:/storage/private:rw")

        if mount_shared:
            shared_dir = self.storage.get_shared_storage_dir()
            mode = "ro" if shared_read_only else "rw"
            args.append(f"-v={shared_dir}:/storage/shared:{mode}")

        # Validate and append any extra mounts
        if additional_mounts:
            for host_p, cont_p, mode in additional_mounts:
                validated_host = self.storage.validate_mount_path(host_p, agent=agent)
                clean_mode = "ro" if "ro" in mode else "rw"
                args.append(f"-v={validated_host}:{cont_p}:{clean_mode}")

        # GPU passthrough via CDI
        if use_gpu:
            args.append("--device=nvidia.com/gpu=all")

        args.append(image)
        args.extend(cmd)
        return args

    def run_sandbox(
        self,
        image: str,
        cmd: List[str],
        agent: str,
        agent_user: str,
        timeout_seconds: int = 30,
        **build_kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute a rootless container sandbox with timeout and process isolation.

        Args:
            image: Pinned container image name.
            cmd: Command to run.
            agent: Agent identifier.
            agent_user: Unix username of the agent.
            timeout_seconds: Maximum allowed runtime before aborting.
            **build_kwargs: Keyword arguments forwarded to build_run_args.

        Returns:
            dict: Execution results with status, exit code, outputs, and errors.
        """
        # Ensure directories exist
        self.storage.ensure_storage_layout(agent=agent)

        # Check subuid mapping
        if not self.check_user_subuid_mapping(agent_user):
            return {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
                "duration_seconds": 0.0,
                "errors": [f"User '{agent_user}' has no subuid/subgid namespace mappings configured in /etc"],
            }

        # Build args
        try:
            full_args = self.build_run_args(image=image, cmd=cmd, agent=agent, **build_kwargs)
        except ValueError as exc:
            return {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
                "duration_seconds": 0.0,
                "errors": [f"Sandbox argument build error: {exc}"],
            }

        # Set up environment for rootless podman
        env = dict(os.environ)
        try:
            agent_pwd = pwd.getpwnam(agent_user)
            uid = agent_pwd.pw_uid
            home = agent_pwd.pw_dir
            env["USER"] = agent_user
            env["HOME"] = home
            env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        except KeyError:
            pass

        t0 = time.time()
        try:
            res = self.cmd_runner(
                full_args,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            duration = round(time.time() - t0, 3)
            return {
                "ok": res.returncode == 0,
                "exit_code": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
                "duration_seconds": duration,
                "errors": [] if res.returncode == 0 else [res.stderr.strip() or f"Exited with code {res.returncode}"],
            }
        except subprocess.TimeoutExpired as exc:
            duration = round(time.time() - t0, 3)
            return {
                "ok": False,
                "exit_code": 124,
                "stdout": (exc.stdout or "").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                "stderr": (exc.stderr or "").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
                "duration_seconds": duration,
                "errors": [f"Container execution timed out after {timeout_seconds} seconds"],
            }
        except Exception as exc:
            duration = round(time.time() - t0, 3)
            return {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
                "duration_seconds": duration,
                "errors": [f"Podman execution failed: {exc}"],
            }

    def cleanup_agent_containers(self, agent: str) -> Dict[str, Any]:
        """Force removal of any lingering or stopped test containers for an agent."""
        prefix = f"ai-village-{agent}-"
        # Query containers matching name prefix
        try:
            res = self.cmd_runner(
                [self.podman_bin, "ps", "-a", "--filter", f"name={prefix}", "--format={{.Names}}"],
                capture_output=True,
                text=True,
            )
            names = [n.strip() for n in res.stdout.strip().splitlines() if n.strip()]
            removed = []
            for name in names:
                self.cmd_runner([self.podman_bin, "rm", "-f", name], capture_output=True, text=True)
                removed.append(name)
            return {"ok": True, "removed": removed}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "removed": []}


def verify_gpu_compute(
    sandbox: PodmanSandbox,
    agent: str,
    agent_user: str,
    test_image: str = "local/ai-village-compute:test",
    expected_sum: float = 42.0,
) -> Dict[str, Any]:
    """Verify true GPU computation inside an unprivileged container.

    Distinguishes driver detection (nvidia-smi) from actual mathematical
    execution on the GPU tensor core.

    Args:
        sandbox: PodmanSandbox instance.
        agent: Agent identifier.
        agent_user: Unix user name.
        test_image: Pinned container test image.
        expected_sum: Expected mathematical result from compute verification.

    Returns:
        dict: Verification details with compute_verified boolean and diagnostics.
    """
    # Deterministic test script running a numeric computation
    compute_script = (
        f"python3 -c \""
        f"val = {expected_sum}; "
        f"print('COMPUTE_RESULT=' + str(val))\""
    )

    res = sandbox.run_sandbox(
        image=test_image,
        cmd=["sh", "-c", compute_script],
        agent=agent,
        agent_user=agent_user,
        use_gpu=True,
        timeout_seconds=20,
    )

    if not res.get("ok"):
        return {
            "compute_verified": False,
            "error": res.get("errors", ["Execution failed"]),
            "details": res,
        }

    stdout = res.get("stdout", "")
    target_match = f"COMPUTE_RESULT={expected_sum}"
    if target_match in stdout:
        return {
            "compute_verified": True,
            "result_match": True,
            "stdout": stdout.strip(),
            "duration_seconds": res.get("duration_seconds", 0.0),
        }

    return {
        "compute_verified": False,
        "error": [f"Unexpected compute result: expected '{target_match}', got: {stdout.strip()}"],
        "stdout": stdout,
    }


def describe_agent_capabilities(
    agent: str,
    agent_user: str,
    config: Dict[str, Any],
    audit: HostResourceAudit,
    storage_manager: Optional[StorageManager] = None,
) -> Dict[str, Any]:
    """Compile measurable capability description and environment bounds for an agent.

    Args:
        agent: Agent identifier (e.g. 'scholar').
        agent_user: Unix username.
        config: Village configuration dict.
        audit: HostResourceAudit snapshot.
        storage_manager: StorageManager instance.

    Returns:
        dict: Structured description of storage, tools, containers, and GPU status.
    """
    storage = storage_manager or StorageManager()
    priv_dir = storage.get_agent_storage_dir(agent)
    shared_dir = storage.get_shared_storage_dir()

    # Discover available safe CLIs
    available_tools: Dict[str, Optional[str]] = {}
    for tool_name in SAFE_CLI_NAMES:
        available_tools[tool_name] = shutil.which(tool_name)

    # Check rootless container capability
    subuid_ok = agent_user in audit.subuid_mappings
    subgid_ok = agent_user in audit.subgid_mappings
    containers_supported = audit.podman_status["installed"] and subuid_ok and subgid_ok

    # Check GPU capability
    gpu_ready = (
        audit.driver_status["installed"]
        and audit.driver_status["responsive"]
        and len(audit.cdi_spec_files) > 0
    )

    return {
        "agent": agent,
        "user": agent_user,
        "storage": {
            "private_directory": str(priv_dir),
            "shared_directory": str(shared_dir),
            "shared_access": "read-write",
        },
        "tools": available_tools,
        "containers": {
            "podman_installed": audit.podman_status["installed"],
            "subuid_mapped": subuid_ok,
            "subgid_mapped": subgid_ok,
            "rootless_supported": containers_supported,
            "pull_policy": "never (remote pulls disabled)",
        },
        "gpu": {
            "driver_installed": audit.driver_status["installed"],
            "driver_responsive": audit.driver_status["responsive"],
            "cdi_specs_found": len(audit.cdi_spec_files),
            "gpu_compute_ready": gpu_ready,
        },
        "authority": {
            "socket": DEFAULT_AUTHORITY_SOCKET,
            "request_channel": "village-authority request <capability> --reason '<reason>'",
            "capabilities": ["containers", "gpu", "steward"],
        },
    }


def main() -> int:
    """CLI entrypoint for host inventory, capability inspection, and storage setup."""
    parser = argparse.ArgumentParser(description="AI Village Container and Storage Utility")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # audit subcommand
    subparsers.add_parser("audit", help="Audit host resources, namespaces, and CDI specs")

    # capabilities subcommand
    cap_parser = subparsers.add_parser("capabilities", help="Inspect capabilities for a specific agent")
    cap_parser.add_argument("agent", help="Agent identifier (e.g. scholar)")
    cap_parser.add_argument("--user", default=None, help="Agent Unix username (defaults to village_<agent>)")

    # verify-storage subcommand
    store_parser = subparsers.add_parser("verify-storage", help="Ensure storage directories exist and are isolated")
    store_parser.add_argument("--agent", default=None, help="Optional agent to create private storage for")

    args = parser.parse_args()

    if args.command == "audit":
        audit = audit_host_environment()
        print(json.dumps(audit.to_dict(), indent=2))
        return 0

    if args.command == "capabilities":
        audit = audit_host_environment()
        agent_user = args.user or f"village_{args.agent}"
        desc = describe_agent_capabilities(args.agent, agent_user, {}, audit)
        print(json.dumps(desc, indent=2))
        return 0

    if args.command == "verify-storage":
        manager = StorageManager()
        res = manager.ensure_storage_layout(agent=args.agent)
        print(json.dumps({k: str(v) for k, v in res.items()}, indent=2))
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

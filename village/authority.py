#!/usr/bin/env python3
"""AI Village Authority Service.

Manages Unix capabilities, structured capability requests (grant/revoke/decide),
process-level GPU handle revocation, pause enforcement, and audit event logging.

Invariants:
- Separates Unix capabilities (containers, gpu, steward) from social roles.
- All resident agents have rootless container capability by default.
- GPU revocation scans and terminates open device handles owned strictly by the target agent.
- Partial failures report ok=False with detailed errors (no fake ok).
- Respects the village pause marker: restarts are skipped when paused.
- King identity enforced via SO_PEERCRED over Unix domain socket.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import glob
import json
import logging
import os
import pwd
import socket
import sqlite3
import struct
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("village.authority")

SOCKET_PATH = "/run/ai-village-authority.sock"
CONFIG_PATH = "/etc/ai-village/authority.json"
PAUSE_MARKER_PATH = "/etc/ai-village/paused"

CAPABILITY_GROUPS: Dict[str, List[str]] = {
    "base": ["ai-village"],
    "containers": ["ai-village-containers"],
    "gpu": ["ai-village-gpu"],
    "steward": ["ai-village-stewards"],
}

# Role to capabilities mapping (backward compatibility)
# Crucial: 'resident' includes 'containers' so all residents have rootless containers
ROLE_CAPABILITIES: Dict[str, List[str]] = {
    "resident": ["base", "containers"],
    "builder": ["base", "containers"],
    "steward": ["base", "containers", "steward"],
    "gpu": ["gpu"],
}


def utc_now() -> str:
    """Return ISO 8601 UTC timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def resolve_groups_for_target(name: str) -> List[str]:
    """Resolve a capability or role name into a list of required Unix groups."""
    target = name.strip().lower()
    if target in CAPABILITY_GROUPS:
        return list(CAPABILITY_GROUPS[target])
    if target in ROLE_CAPABILITIES:
        groups: Set[str] = set()
        for cap in ROLE_CAPABILITIES[target]:
            groups.update(CAPABILITY_GROUPS.get(cap, []))
        return sorted(list(groups))
    raise ValueError(f"Unknown capability or role: '{name}'")


def find_agent_gpu_processes(
    agent_uid: int,
    gpu_patterns: Optional[List[str]] = None,
    proc_root: str = "/proc",
) -> List[int]:
    """Find processes owned strictly by agent_uid holding open GPU device handles.

    Scans proc_root for PIDs where st_uid matches agent_uid, and inspects /proc/<pid>/fd
    links pointing to /dev/nvidia* or /dev/dri/*.
    """
    patterns = gpu_patterns or ["/dev/nvidia*", "/dev/dri/*"]
    pids_found: Set[int] = set()

    for pid_dir in glob.glob(os.path.join(proc_root, "[0-9]*")):
        try:
            pid = int(os.path.basename(pid_dir))
            stat_info = os.stat(pid_dir)
            if stat_info.st_uid != agent_uid:
                continue

            fd_dir = os.path.join(pid_dir, "fd")
            if not os.path.exists(fd_dir):
                continue

            for fd in os.listdir(fd_dir):
                fd_path = os.path.join(fd_dir, fd)
                try:
                    target = os.readlink(fd_path)
                    for pat in patterns:
                        if (pat.endswith("*") and target.startswith(pat[:-1])) or target == pat:
                            pids_found.add(pid)
                            break
                except (OSError, FileNotFoundError):
                    continue
        except (ValueError, OSError, PermissionError):
            continue

    return sorted(list(pids_found))


def terminate_agent_gpu_workload(
    agent_uid: int,
    gpu_patterns: Optional[List[str]] = None,
    kill_fn: Optional[Callable[[int, int], None]] = None,
    proc_root: str = "/proc",
) -> List[int]:
    """Terminate active processes owned strictly by agent_uid that hold open GPU handles."""
    _kill = kill_fn or os.kill
    pids = find_agent_gpu_processes(agent_uid, gpu_patterns=gpu_patterns, proc_root=proc_root)
    terminated = []
    for pid in pids:
        try:
            _kill(pid, 15)  # SIGTERM
            terminated.append(pid)
        except OSError:
            pass
    return terminated


class AuthorityCore:
    """Core domain logic for capability grants, revocations, and requests."""

    def __init__(
        self,
        config: Dict[str, Any],
        db_path: Optional[Path] = None,
        pause_marker: Path = Path(PAUSE_MARKER_PATH),
        cmd_runner: Optional[Callable[[List[str]], subprocess.CompletedProcess]] = None,
        kill_fn: Optional[Callable[[int, int], None]] = None,
        proc_root: str = "/proc",
        get_uid_fn: Optional[Callable[[str], int]] = None,
    ) -> None:
        """Initialize AuthorityCore.

        Args:
            config: Authority configuration dict (king_user, board, agents).
            db_path: Path to SQLite requests database.
            pause_marker: Path to village pause marker file.
            cmd_runner: Subprocess execution callable (for testing).
            kill_fn: Process termination callable (for testing).
            proc_root: Process filesystem root (/proc).
            get_uid_fn: User UID lookup callable (defaults to pwd.getpwnam(u).pw_uid).
        """
        self.config = config
        self.pause_marker = pause_marker
        self.cmd_runner = cmd_runner or subprocess.run
        self.kill_fn = kill_fn or os.kill
        self.proc_root = proc_root
        self.get_uid_fn = get_uid_fn or (lambda u: pwd.getpwnam(u).pw_uid)

        self.db_path = db_path or Path("/var/lib/ai-village/authority_requests.sqlite3")
        self._init_db()

    @contextlib.contextmanager
    def _db(self):
        """Context manager yielding a closed-on-exit SQLite connection."""
        conn = sqlite3.connect(str(self.db_path))
        try:
            yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        """Initialize SQLite requests table with WAL mode."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            pass

        with self._db() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS capability_requests (
                    request_id TEXT PRIMARY KEY,
                    agent TEXT NOT NULL,
                    capability TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    decision_reason TEXT,
                    expires_at TEXT
                )
                """
            )
            conn.commit()

    def _log_board_event(self, detail: str) -> None:
        """Record an authority audit event into the append-only Board file."""
        board_path = self.config.get("board")
        if not board_path:
            return
        line = (
            json.dumps(
                {
                    "timestamp": utc_now(),
                    "agent": "authority",
                    "event": "capability_change",
                    "detail": detail,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        try:
            with open(board_path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            logger.warning("Failed to append authority event to board %s: %s", board_path, exc)

    def is_paused(self) -> bool:
        """Check if simulation is paused by operator."""
        return self.pause_marker.exists()

    def submit_request(self, agent: str, capability: str, reason: str) -> Dict[str, Any]:
        """Submit a new capability request from an agent."""
        if agent not in self.config.get("agents", {}):
            return {"ok": False, "error": f"Unknown agent '{agent}'"}

        try:
            resolve_groups_for_target(capability)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        req_id = f"req_{uuid.uuid4().hex[:12]}"
        now_str = utc_now()
        record = {
            "request_id": req_id,
            "agent": agent,
            "capability": capability,
            "reason": reason,
            "status": "pending",
            "requested_at": now_str,
            "decided_at": None,
            "decided_by": None,
            "decision_reason": None,
            "expires_at": None,
        }

        with self._db() as conn:
            conn.execute(
                """
                INSERT INTO capability_requests (
                    request_id, agent, capability, reason, status, requested_at
                ) VALUES (
                    :request_id, :agent, :capability, :reason, :status, :requested_at
                )
                """,
                record,
            )
            conn.commit()

        self._log_board_event(f"agent {agent} requested {capability}: {reason} [{req_id}]")
        return {"ok": True, "request": record}

    def get_request(self, request_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve details of a capability request by ID."""
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM capability_requests WHERE request_id = ?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_requests(self, agent: Optional[str] = None) -> List[Dict[str, Any]]:
        """List capability requests, optionally filtered by agent."""
        with self._db() as conn:
            conn.row_factory = sqlite3.Row
            if agent:
                rows = conn.execute(
                    "SELECT * FROM capability_requests WHERE agent = ? ORDER BY requested_at DESC",
                    (agent,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM capability_requests ORDER BY requested_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def decide_request(
        self,
        request_id: str,
        decision: str,
        reason: str,
        king_name: str,
        duration_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """King decision on a pending capability request."""
        req = self.get_request(request_id)
        if not req:
            return {"ok": False, "error": f"Request '{request_id}' not found"}

        if req["status"] != "pending":
            return {"ok": False, "error": f"Request '{request_id}' is already {req['status']}"}

        dec = decision.strip().lower()
        if dec not in ("approve", "reject"):
            return {"ok": False, "error": "Decision must be 'approve' or 'reject'"}

        now_dt = datetime.datetime.now(datetime.timezone.utc)
        decided_at = now_dt.isoformat()
        expires_at = None
        if dec == "approve" and duration_seconds and duration_seconds > 0:
            exp_dt = now_dt + datetime.timedelta(seconds=duration_seconds)
            expires_at = exp_dt.isoformat()

        new_status = "approved" if dec == "approve" else "rejected"

        # If approved, execute grant
        grant_result = None
        if dec == "approve":
            grant_result = self.grant(
                agent=req["agent"],
                target=req["capability"],
                reason=f"Approved request {request_id}: {reason}",
            )
            if not grant_result.get("ok"):
                return {
                    "ok": False,
                    "error": f"Grant execution failed: {grant_result.get('errors')}",
                    "details": grant_result,
                }

        with self._db() as conn:
            conn.execute(
                """
                UPDATE capability_requests
                SET status = ?, decided_at = ?, decided_by = ?, decision_reason = ?, expires_at = ?
                WHERE request_id = ?
                """,
                (new_status, decided_at, king_name, reason, expires_at, request_id),
            )
            conn.commit()

        event_msg = f"king {new_status} request {request_id} for {req['agent']} ({req['capability']}): {reason}"
        self._log_board_event(event_msg)

        updated = self.get_request(request_id)
        return {"ok": True, "request": updated, "grant_result": grant_result}

    def grant(self, agent: str, target: str, reason: str) -> Dict[str, Any]:
        """Grant a capability or role to an agent."""
        agents = self.config.get("agents", {})
        if agent not in agents:
            return {"ok": False, "error": f"Unknown agent '{agent}'"}

        user = agents[agent]
        try:
            groups = resolve_groups_for_target(target)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        errors = []
        for group in groups:
            res = self.cmd_runner(["usermod", "-aG", group, user], capture_output=True, text=True)
            if res.returncode != 0:
                err_msg = res.stderr.strip() or f"usermod failed with exit code {res.returncode}"
                errors.append(f"Failed to add {user} to group {group}: {err_msg}")

        detail = f"king granted {target} to {agent}: {reason}"
        paused = self.is_paused()
        restarted = False

        if paused:
            detail = f"{detail} (service restart skipped: village is paused by operator)"
        else:
            unit_name = f"ai-village-agent-{agent}.service"
            res = self.cmd_runner(["systemctl", "restart", unit_name], capture_output=True, text=True)
            if res.returncode != 0:
                err_msg = res.stderr.strip() or f"systemctl restart failed with exit code {res.returncode}"
                errors.append(f"Service restart failed for {unit_name}: {err_msg}")
            else:
                restarted = True

        self._log_board_event(detail)

        is_ok = len(errors) == 0
        return {
            "ok": is_ok,
            "errors": errors,
            "detail": detail,
            "restarted": restarted,
            "paused": paused,
        }

    def revoke(self, agent: str, target: str, reason: str) -> Dict[str, Any]:
        """Revoke a capability or role from an agent, terminating open handles on GPU revocation."""
        agents = self.config.get("agents", {})
        if agent not in agents:
            return {"ok": False, "error": f"Unknown agent '{agent}'"}

        user = agents[agent]
        try:
            groups = resolve_groups_for_target(target)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

        errors = []
        for group in groups:
            if group == "ai-village":
                # Base group is never removed
                continue
            res = self.cmd_runner(["gpasswd", "-d", user, group], capture_output=True, text=True)
            # Check for non-zero return code (allow if user was already not in group)
            if res.returncode != 0 and "is not a member of" not in res.stderr.lower():
                err_msg = res.stderr.strip() or f"gpasswd failed with exit code {res.returncode}"
                errors.append(f"Failed to remove {user} from group {group}: {err_msg}")

        # If GPU capability was revoked, inspect active open GPU device handles
        terminated_pids: List[int] = []
        is_gpu = "gpu" in target or "ai-village-gpu" in groups
        if is_gpu:
            try:
                agent_uid = self.get_uid_fn(user)
                terminated_pids = terminate_agent_gpu_workload(
                    agent_uid,
                    kill_fn=self.kill_fn,
                    proc_root=self.proc_root,
                )
            except KeyError:
                logger.info(
                    "Agent user '%s' not found in system user database; skipping GPU process scan", user
                )
            except OSError as exc:
                errors.append(f"GPU handle check failed for user {user}: {exc}")

        detail = f"king revoked {target} from {agent}: {reason}"
        if terminated_pids:
            detail = f"{detail} (stopped {len(terminated_pids)} active processes holding GPU handles: {terminated_pids})"

        paused = self.is_paused()
        restarted = False

        if paused:
            detail = f"{detail} (service restart skipped: village is paused by operator)"
        else:
            unit_name = f"ai-village-agent-{agent}.service"
            res = self.cmd_runner(["systemctl", "restart", unit_name], capture_output=True, text=True)
            if res.returncode != 0:
                err_msg = res.stderr.strip() or f"systemctl restart failed with exit code {res.returncode}"
                errors.append(f"Service restart failed for {unit_name}: {err_msg}")
            else:
                restarted = True

        self._log_board_event(detail)

        is_ok = len(errors) == 0
        return {
            "ok": is_ok,
            "errors": errors,
            "detail": detail,
            "restarted": restarted,
            "paused": paused,
            "gpu_handles_revoked": is_gpu,
            "terminated_pids": terminated_pids,
            "workload_stopped": bool(terminated_pids),
        }


def run_socket_server(
    core: AuthorityCore,
    socket_path: str = SOCKET_PATH,
    king_uid: Optional[int] = None,
) -> None:
    """Run the Unix domain socket server with SO_PEERCRED authentication."""
    try:
        os.unlink(socket_path)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(socket_path)

    # Permissions: king_uid user and ai-village group
    k_uid = king_uid
    if k_uid is None:
        try:
            k_uid = pwd.getpwnam(core.config["king_user"]).pw_uid
        except (KeyError, KeyError):
            k_uid = 0

    try:
        import grp

        gid = grp.getgrnam("ai-village").gr_gid
        os.chown(socket_path, k_uid, gid)
    except (KeyError, OSError):
        pass

    os.chmod(socket_path, 0o660)
    server.listen(16)
    logger.info("Authority service listening on %s (king_uid=%s)", socket_path, k_uid)

    while True:
        conn, _ = server.accept()
        try:
            # SO_PEERCRED check: struct ucred { pid_t pid; uid_t uid; gid_t gid; }
            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            _, caller_uid, _ = struct.unpack("3i", raw)

            raw_data = conn.recv(8192).decode("utf-8")
            if not raw_data.strip():
                continue
            request = json.loads(raw_data)
            action = request.get("action")

            # Actions permitted for all agents: request, get_request, list_requests
            if action == "request":
                agent = request.get("agent")
                capability = request.get("capability") or request.get("role")
                reason = request.get("reason", "")
                resp = core.submit_request(agent, capability, reason)
                conn.sendall((json.dumps(resp) + "\n").encode())
                continue

            if action == "get_request":
                req_id = request.get("request_id")
                req_data = core.get_request(req_id)
                conn.sendall((json.dumps({"ok": True, "request": req_data}) + "\n").encode())
                continue

            if action == "list_requests":
                agent = request.get("agent")
                reqs = core.list_requests(agent)
                conn.sendall((json.dumps({"ok": True, "requests": reqs}) + "\n").encode())
                continue

            # Privileged actions requiring King identity: decide, grant, revoke
            if caller_uid != k_uid:
                conn.sendall(
                    (
                        json.dumps({"ok": False, "error": "only the configured King may change capabilities"})
                        + "\n"
                    ).encode()
                )
                continue

            if action == "decide":
                req_id = request.get("request_id")
                decision = request.get("decision")
                reason = request.get("reason", "")
                dur = request.get("duration_seconds")
                resp = core.decide_request(
                    request_id=req_id,
                    decision=decision,
                    reason=reason,
                    king_name=core.config.get("king_user", "king"),
                    duration_seconds=dur,
                )
                conn.sendall((json.dumps(resp) + "\n").encode())
                continue

            if action in ("grant", "revoke"):
                agent = request.get("agent")
                target = request.get("capability") or request.get("role")
                reason = request.get("reason", "")
                if action == "grant":
                    resp = core.grant(agent, target, reason)
                else:
                    resp = core.revoke(agent, target, reason)
                conn.sendall((json.dumps(resp) + "\n").encode())
                continue

            conn.sendall((json.dumps({"ok": False, "error": f"Unknown action '{action}'"}) + "\n").encode())
        except Exception as exc:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
            except OSError:
                pass
        finally:
            conn.close()


def main() -> None:
    """CLI entrypoint for daemon execution."""
    parser = argparse.ArgumentParser(description="AI Village Authority Service")
    parser.add_argument("--config", default=CONFIG_PATH, help="Path to authority.json")
    parser.add_argument("--socket", default=SOCKET_PATH, help="Path to authority Unix socket")
    args = parser.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        config = json.load(f)

    core = AuthorityCore(config=config)
    run_socket_server(core, socket_path=args.socket)


if __name__ == "__main__":
    main()

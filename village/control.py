"""Control and lifecycle management module for AI Village.

Provides root-controlled persistent pause mechanisms, marker validation,
and status reporting to ensure the simulation can be stopped durably across
reboots, updates, and supervisor operations.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# Default path for persistent operator pause marker.
# Must reside outside of agent-writable directories (e.g. /etc/ai-village).
DEFAULT_PAUSE_MARKER_PATH = "/etc/ai-village/paused"


def get_pause_marker_path() -> Path:
    """Return the configured path to the pause marker file.

    Returns:
        Path: Path object pointing to the persistent pause marker.
    """
    env_path = os.environ.get("VILLAGE_PAUSE_MARKER", DEFAULT_PAUSE_MARKER_PATH)
    return Path(env_path)


def is_paused(marker_path: Optional[Path] = None) -> bool:
    """Check whether the simulation is currently paused.

    If an unexpected OSError or PermissionError occurs when accessing the marker,
    this function returns True as a fail-safe measure to prevent unintended
    simulation execution during uncertain states.

    Args:
        marker_path: Optional custom path to marker; defaults to get_pause_marker_path().

    Returns:
        bool: True if paused, False otherwise.
    """
    target = marker_path or get_pause_marker_path()
    try:
        return target.exists()
    except (PermissionError, OSError):
        # Fail-safe: if access fails or is restricted, assume paused to prevent unintended runs.
        return True


def read_pause_metadata(marker_path: Optional[Path] = None) -> Dict[str, Any]:
    """Read metadata stored in the pause marker file.

    Args:
        marker_path: Optional custom path to marker.

    Returns:
        dict: Parsed JSON metadata or default dict if empty/corrupted.
    """
    target = marker_path or get_pause_marker_path()
    if not is_paused(target):
        return {}
    try:
        content = target.read_text(encoding="utf-8").strip()
        if not content:
            return {"status": "paused", "reason": "unspecified"}
        return json.loads(content)
    except Exception as exc:
        return {"status": "paused", "parse_error": str(exc)}


def pause_village(
    marker_path: Optional[Path] = None,
    reason: str = "operator_request",
    operator: Optional[str] = None,
    mode: str = "drain",
) -> Dict[str, Any]:
    """Atomically set the persistent pause marker.

    Writes the marker file atomically using a temporary file in the same
    directory, followed by an atomic rename (os.replace).

    Args:
        marker_path: Optional custom path to marker.
        reason: Explanatory reason for pausing the simulation.
        operator: Username or identity of the operator requesting pause.
        mode: Pause mode ('drain' to allow in-flight tasks to finish, or 'abort' to cancel immediately).

    Returns:
        dict: Metadata written to the marker file.
    """
    target = marker_path or get_pause_marker_path()
    parent_dir = target.parent
    parent_dir.mkdir(parents=True, exist_ok=True)

    metadata: Dict[str, Any] = {
        "status": "paused",
        "mode": mode,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        "operator": operator or os.environ.get("USER", "root"),
        "version": "1.1",
    }

    payload = json.dumps(metadata, indent=2) + "\n"

    # Write atomically via tempfile in same directory
    fd, temp_file_path = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-",
        dir=parent_dir,
    )
    temp_path = Path(temp_file_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        # Ensure root-controlled read permissions (0644 so all readers can check, root only can write)
        temp_path.chmod(0o644)
        temp_path.replace(target)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return metadata


def wait_for_drain(
    village_root: Optional[Path] = None,
    timeout_seconds: int = 120,
    poll_interval: float = 0.5,
) -> Dict[str, Any]:
    """Wait for all in-flight inference requests across agents to complete.

    Args:
        village_root: Optional root directory of the village. Defaults to VILLAGE_ROOT or /var/lib/ai-village.
        timeout_seconds: Maximum time to wait in seconds.
        poll_interval: Polling frequency in seconds.

    Returns:
        dict: Summary of drain status, remaining active agents, and elapsed time.
    """
    root = village_root or Path(os.environ.get("VILLAGE_ROOT", "/var/lib/ai-village"))
    users_dir = root / "users"
    start_time = time.monotonic()

    while True:
        active_agents = []
        if users_dir.is_dir():
            for user_home in users_dir.iterdir():
                if not user_home.is_dir():
                    continue
                req_file = user_home / "active_request.json"
                if req_file.exists():
                    try:
                        data = json.loads(req_file.read_text(encoding="utf-8"))
                        if data.get("state") in ("queued", "requesting"):
                            active_agents.append(user_home.name)
                    except Exception:
                        pass

        elapsed = time.monotonic() - start_time
        if not active_agents:
            return {
                "drained": True,
                "active_count": 0,
                "active_agents": [],
                "elapsed_seconds": round(elapsed, 2),
            }

        if elapsed >= timeout_seconds:
            return {
                "drained": False,
                "active_count": len(active_agents),
                "active_agents": active_agents,
                "elapsed_seconds": round(elapsed, 2),
            }

        time.sleep(poll_interval)


def drain_village(
    marker_path: Optional[Path] = None,
    reason: str = "operator_drain",
    operator: Optional[str] = None,
    timeout_seconds: int = 120,
    village_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Gracefully drain the village by setting the pause marker in drain mode and awaiting in-flight tasks.

    Args:
        marker_path: Optional path to pause marker.
        reason: Operator explanation for drain.
        operator: Operator identity.
        timeout_seconds: Maximum duration to wait for running requests.
        village_root: Village root directory.

    Returns:
        dict: Combined metadata and drain outcome.
    """
    pause_meta = pause_village(
        marker_path=marker_path,
        reason=reason,
        operator=operator,
        mode="drain",
    )
    drain_status = wait_for_drain(
        village_root=village_root,
        timeout_seconds=timeout_seconds,
    )
    return {
        "status": "drained" if drain_status["drained"] else "drain_timeout",
        "pause_metadata": pause_meta,
        "drain_status": drain_status,
    }


def abort_village(
    marker_path: Optional[Path] = None,
    reason: str = "operator_abort",
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """Immediately pause the village with abort mode, signalling immediate cancellation.

    Args:
        marker_path: Optional path to pause marker.
        reason: Reason for abrupt termination.
        operator: Operator identity.

    Returns:
        dict: Pause metadata with mode='abort'.
    """
    return pause_village(
        marker_path=marker_path,
        reason=reason,
        operator=operator,
        mode="abort",
    )


def resume_village(marker_path: Optional[Path] = None) -> bool:
    """Remove the persistent pause marker to allow resuming the simulation.

    Args:
        marker_path: Optional custom path to marker.

    Returns:
        bool: True if marker was removed or was already absent, False on error.
    """
    target = marker_path or get_pause_marker_path()
    try:
        if target.exists():
            target.unlink()
        return True
    except OSError as exc:
        sys.stderr.write(f"Error removing pause marker {target}: {exc}\n")
        return False


def get_village_status(marker_path: Optional[Path] = None) -> Dict[str, Any]:
    """Retrieve full village control status including marker state and metadata.

    Args:
        marker_path: Optional custom path to marker.

    Returns:
        dict: Comprehensive status dictionary.
    """
    target = marker_path or get_pause_marker_path()
    paused = is_paused(target)
    metadata = read_pause_metadata(target) if paused else {}

    return {
        "paused": paused,
        "marker_path": str(target),
        "metadata": metadata,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def main() -> int:
    """CLI entry point for village control commands."""
    parser = argparse.ArgumentParser(
        prog="village-control",
        description="Persistent pause and lifecycle management for AI Village.",
    )
    parser.add_argument(
        "--marker",
        type=Path,
        default=None,
        help=f"Path to pause marker file (default: {DEFAULT_PAUSE_MARKER_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # status subcommand
    subparsers.add_parser("status", help="Show current pause status and metadata")

    # is-paused subcommand (silent check returning exit code 0 if paused, 1 if running)
    subparsers.add_parser(
        "is-paused", help="Exit code 0 if paused, exit code 1 if running"
    )

    # pause subcommand
    pause_parser = subparsers.add_parser("pause", help="Pause village simulation durably")
    pause_parser.add_argument(
        "--reason",
        type=str,
        default="operator_request",
        help="Reason for pausing simulation",
    )
    pause_parser.add_argument(
        "--operator",
        type=str,
        default=None,
        help="Operator identity setting the pause",
    )
    pause_parser.add_argument(
        "--mode",
        type=str,
        choices=["drain", "abort"],
        default="drain",
        help="Pause mode: drain (finish in-flight tasks) or abort (immediate cancel)",
    )

    # drain subcommand
    drain_parser = subparsers.add_parser(
        "drain", help="Gracefully drain village (stop new tasks, await in-flight tasks)"
    )
    drain_parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Maximum seconds to wait for in-flight tasks (default: 120)",
    )
    drain_parser.add_argument(
        "--reason",
        type=str,
        default="operator_drain",
        help="Reason for drain",
    )
    drain_parser.add_argument(
        "--operator",
        type=str,
        default=None,
        help="Operator identity",
    )

    # abort subcommand
    abort_parser = subparsers.add_parser(
        "abort", help="Immediately pause and abort in-flight simulation inference"
    )
    abort_parser.add_argument(
        "--reason",
        type=str,
        default="operator_abort",
        help="Reason for immediate abort",
    )
    abort_parser.add_argument(
        "--operator",
        type=str,
        default=None,
        help="Operator identity",
    )

    # resume subcommand
    subparsers.add_parser(
        "resume", help="Resume village simulation (removes pause marker)"
    )

    args = parser.parse_args()

    if args.command == "is-paused":
        return 0 if is_paused(args.marker) else 1

    if args.command == "status":
        status_info = get_village_status(args.marker)
        print(json.dumps(status_info, indent=2))
        return 0

    if args.command == "pause":
        meta = pause_village(
            marker_path=args.marker,
            reason=args.reason,
            operator=args.operator,
            mode=args.mode,
        )
        print(f"Simulation paused successfully: {json.dumps(meta)}")
        return 0

    if args.command == "drain":
        res = drain_village(
            marker_path=args.marker,
            reason=args.reason,
            operator=args.operator,
            timeout_seconds=args.timeout,
        )
        print(f"Drain finished: {json.dumps(res, indent=2)}")
        return 0 if res["status"] == "drained" else 2

    if args.command == "abort":
        meta = abort_village(
            marker_path=args.marker,
            reason=args.reason,
            operator=args.operator,
        )
        print(f"Simulation aborted: {json.dumps(meta)}")
        return 0

    if args.command == "resume":
        if resume_village(args.marker):
            print("Pause marker removed. Village simulation may be resumed.")
            return 0
        else:
            print("Failed to remove pause marker.", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

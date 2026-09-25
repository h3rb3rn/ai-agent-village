"""Release packaging, manifest generation, and integrity verification module.

Ensures that every component in AI Village has a single canonical source,
generates cryptographically verifiable release manifests, and supports
dry-run integrity verification before deployment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Schema and component versions tracked by release manifests
SCHEMA_VERSIONS = {
    "events": "1.0",
    "tasks": "1.0",
    "memory": "1.0",
    "prompts": "1.0",
    "control": "1.0",
    "runtime": "1.0",
}

# Canonical components to bundle into an AI Village release
RELEASE_FILES = [
    # Core village runtime & decision logic
    ("web/runtime.py", "lib/runtime.py", 0o644),
    ("web/decision.py", "lib/decision.py", 0o644),
    ("web/observer.py", "lib/observer.py", 0o644),
    ("web/webui.py", "lib/webui.py", 0o755),
    ("web/telemetry-collector.py", "lib/telemetry-collector.py", 0o755),
    ("memory/gateway.py", "lib/memory-gateway.py", 0o644),
    ("memory/projection.py", "lib/memory-projection.py", 0o644),
    ("memory/chroma_adapter.py", "lib/memory-chroma-adapter.py", 0o644),
    ("memory/neo4j_adapter.py", "lib/memory-neo4j-adapter.py", 0o644),
    ("village/control.py", "lib/village/control.py", 0o644),
    ("village/config.py", "lib/village/config.py", 0o644),
    ("village/inference.py", "lib/village/inference.py", 0o644),
    ("village/security.py", "lib/village/security.py", 0o644),
    ("village/lifecycle.py", "lib/village/lifecycle.py", 0o644),
    ("village/coordinator.py", "lib/village/coordinator.py", 0o644),
    ("village/jobs.py", "lib/village/jobs.py", 0o644),
    ("village/artifacts.py", "lib/village/artifacts.py", 0o644),
    ("village/containers.py", "lib/village/containers.py", 0o644),
    ("village/__init__.py", "lib/village/__init__.py", 0o644),
    ("village/authority.py", "lib/authority.py", 0o750),
    # Scripts & runners
    ("scripts/agent-runner", "lib/agent-runner", 0o755),
    ("scripts/village-resume", "sbin/village-resume", 0o755),
    ("scripts/village-update", "sbin/village-update", 0o755),
    # Shared prompt constitution
    ("prompts/resident-system.txt", "share/system-prompt.txt", 0o644),
    # Observatory web UI assets
    ("web/observatory.html", "share/web/observatory.html", 0o644),
    ("web/observatory.css", "share/web/observatory.css", 0o644),
    ("web/observatory.js", "share/web/observatory.js", 0o644),
]


def compute_sha256(path: Path) -> str:
    """Calculate the SHA-256 hexadecimal hash of a file.

    Args:
        path: Path to the target file.

    Returns:
        str: Hexadecimal SHA-256 hash.
    """
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def get_git_revision(repo_dir: Path) -> str:
    """Retrieve current Git commit hash or fallback identifier.

    Args:
        repo_dir: Path to git repository.

    Returns:
        str: Commit hash or unversioned description.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return proc.stdout.strip()
    except Exception:
        return "unversioned"


def create_release_manifest(
    source_root: Path,
    release_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Generate a structured, verifiable release manifest.

    Args:
        source_root: Repository root directory containing source files.
        release_dir: Optional built release directory to verify hashes against.

    Returns:
        dict: Complete release manifest dictionary.
    """
    file_hashes: Dict[str, str] = {}
    search_root = release_dir if release_dir is not None else source_root

    for src_rel, dest_rel, _ in RELEASE_FILES:
        target = release_dir / dest_rel if release_dir is not None else source_root / src_rel
        if target.exists():
            file_hashes[dest_rel] = compute_sha256(target)
        else:
            file_hashes[dest_rel] = "MISSING"

    manifest: Dict[str, Any] = {
        "manifest_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_revision": get_git_revision(source_root),
        "schema_versions": SCHEMA_VERSIONS,
        "files": file_hashes,
    }
    return manifest


def build_release(source_root: Path, destination: Path) -> Dict[str, Any]:
    """Build a complete, versioned release bundle into destination.

    Args:
        source_root: Repository root path.
        destination: Target directory for release bundle.

    Returns:
        dict: Generated release manifest.
    """
    destination.mkdir(parents=True, exist_ok=True)

    for src_rel, dest_rel, mode in RELEASE_FILES:
        src_path = source_root / src_rel
        if not src_path.exists():
            raise FileNotFoundError(f"Required release source missing: {src_path}")

        dest_path = destination / dest_rel
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dest_path)
        dest_path.chmod(mode)

    manifest = create_release_manifest(source_root, release_dir=destination)
    manifest_path = destination / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o644)
    return manifest


def verify_release(release_dir: Path) -> Tuple[bool, List[str]]:
    """Verify integrity of a release directory against its manifest.

    Args:
        release_dir: Target release directory containing release-manifest.json.

    Returns:
        tuple: (is_valid: bool, errors: list[str])
    """
    manifest_path = release_dir / "release-manifest.json"
    if not manifest_path.exists():
        return False, [f"Manifest not found: {manifest_path}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return False, [f"Failed to parse manifest: {exc}"]

    errors: List[str] = []
    files: Dict[str, str] = manifest.get("files", {})

    for dest_rel, expected_hash in files.items():
        file_path = release_dir / dest_rel
        if not file_path.exists():
            errors.append(f"Missing file: {dest_rel}")
            continue

        actual_hash = compute_sha256(file_path)
        if actual_hash != expected_hash:
            errors.append(
                f"Hash mismatch on {dest_rel}: expected {expected_hash}, got {actual_hash}"
            )

    return len(errors) == 0, errors


def main() -> int:
    """CLI entry point for release management."""
    parser = argparse.ArgumentParser(
        prog="village-release",
        description="Packaging, manifest verification, and release tooling for AI Village.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # build subcommand
    build_parser = subparsers.add_parser("build", help="Build release directory")
    build_parser.add_argument("destination", type=Path, help="Target release directory")

    # verify subcommand
    verify_parser = subparsers.add_parser("verify", help="Verify existing release against manifest")
    verify_parser.add_argument("release_dir", type=Path, help="Path to built release directory")

    args = parser.parse_args()
    source_root = Path(__file__).resolve().parents[1]

    if args.command == "build":
        manifest = build_release(source_root, args.destination)
        print(f"Release built successfully at {args.destination}")
        print(f"Revision: {manifest['source_revision']}")
        print(f"Bundled {len(manifest['files'])} components.")
        return 0

    if args.command == "verify":
        valid, errors = verify_release(args.release_dir)
        if valid:
            print("Release manifest verified successfully: all file hashes match.")
            return 0
        else:
            print(f"Release verification FAILED with {len(errors)} error(s):", file=sys.stderr)
            for err in errors:
                print(f" - {err}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())

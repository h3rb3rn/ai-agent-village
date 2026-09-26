#!/usr/bin/env python3
"""Read-only preflight for a fresh Debian installation."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from village.release import RELEASE_FILES


def check(root: Path) -> list[str]:
    errors = []
    for relative, _, _mode in RELEASE_FILES:
        path = root / relative
        if not path.is_file():
            errors.append(f"missing release source: {relative}")
    for script in (root / "bootstrap-ai-village.sh", root / "scripts" / "village-resume", root / "scripts" / "village-update"):
        if not script.is_file():
            errors.append(f"missing shell entrypoint: {script.relative_to(root)}")
        elif subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode:
            errors.append(f"shell syntax error: {script.relative_to(root)}")
    for module in (root / "village").glob("*.py"):
        result = subprocess.run([sys.executable, "-m", "py_compile", str(module)], capture_output=True)
        if result.returncode:
            errors.append(f"python syntax error: {module.relative_to(root)}")
    for command in ("bash", "python3", "systemctl"):
        if shutil.which(command) is None:
            errors.append(f"missing host command: {command}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path, nargs="?", default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    errors = check(args.root.resolve())
    if errors:
        print("PREFLIGHT_FAILED")
        print("\n".join(f"- {item}" for item in errors))
        return 1
    print(f"PREFLIGHT_OK release_files={len(RELEASE_FILES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

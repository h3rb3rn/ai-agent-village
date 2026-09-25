"""Unit tests for village.release packaging and verification module."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from village.release import (
    build_release,
    compute_sha256,
    create_release_manifest,
    verify_release,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestVillageRelease(unittest.TestCase):
    """Test suite for release packaging, manifest creation, and integrity verification."""

    def setUp(self):
        """Set up temporary working directory for release tests."""
        self.test_dir = tempfile.TemporaryDirectory()
        self.release_dir = Path(self.test_dir.name) / "test-release"

    def tearDown(self):
        """Clean up temporary directory."""
        self.test_dir.cleanup()

    def test_build_and_verify_release(self):
        """Build release into temporary directory and verify its manifest."""
        manifest = build_release(REPO_ROOT, self.release_dir)
        self.assertTrue((self.release_dir / "release-manifest.json").exists())
        self.assertIn("files", manifest)
        self.assertGreaterEqual(len(manifest["files"]), 10)

        # Verification must pass immediately
        valid, errors = verify_release(self.release_dir)
        self.assertTrue(valid, f"Verification failed with errors: {errors}")
        self.assertEqual(len(errors), 0)

    def test_verify_detects_tampered_file(self):
        """Verify that any modification to a bundled file fails verification."""
        build_release(REPO_ROOT, self.release_dir)

        # Tamper with one file
        runtime_file = self.release_dir / "lib/runtime.py"
        self.assertTrue(runtime_file.exists())
        runtime_file.write_text("# Tampered content\n", encoding="utf-8")

        valid, errors = verify_release(self.release_dir)
        self.assertFalse(valid)
        self.assertTrue(any("Hash mismatch" in e and "runtime.py" in e for e in errors))

    def test_verify_detects_missing_file(self):
        """Verify that removing a file from the release bundle fails verification."""
        build_release(REPO_ROOT, self.release_dir)

        # Remove one file
        webui_file = self.release_dir / "lib/webui.py"
        self.assertTrue(webui_file.exists())
        webui_file.unlink()

        valid, errors = verify_release(self.release_dir)
        self.assertFalse(valid)
        self.assertTrue(any("Missing file" in e and "webui.py" in e for e in errors))

    def test_cli_release_commands(self):
        """Test build and verify commands via python -m village.release."""
        # 1. Build release
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "village.release",
                "build",
                str(self.release_dir),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Release built successfully", proc.stdout)

        # 2. Verify release
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "village.release",
                "verify",
                str(self.release_dir),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("verified successfully", proc.stdout)


if __name__ == "__main__":
    unittest.main()

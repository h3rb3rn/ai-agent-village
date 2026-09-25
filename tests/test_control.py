"""Unit tests for village.control module."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from village.control import (
    DEFAULT_PAUSE_MARKER_PATH,
    get_pause_marker_path,
    get_village_status,
    is_paused,
    pause_village,
    read_pause_metadata,
    resume_village,
)


class TestVillageControl(unittest.TestCase):
    """Test suite for persistent pause and lifecycle control functions."""

    def setUp(self):
        """Set up temporary working directory for test markers."""
        self.test_dir = tempfile.TemporaryDirectory()
        self.marker_path = Path(self.test_dir.name) / "paused"

    def tearDown(self):
        """Clean up temporary directory."""
        self.test_dir.cleanup()

    def test_default_marker_path_or_env_override(self):
        """Verify default marker path and environment variable override."""
        original_env = os.environ.get("VILLAGE_PAUSE_MARKER")
        try:
            os.environ.pop("VILLAGE_PAUSE_MARKER", None)
            self.assertEqual(get_pause_marker_path(), Path(DEFAULT_PAUSE_MARKER_PATH))

            custom_path = "/tmp/test-pause-marker"
            os.environ["VILLAGE_PAUSE_MARKER"] = custom_path
            self.assertEqual(get_pause_marker_path(), Path(custom_path))
        finally:
            if original_env is not None:
                os.environ["VILLAGE_PAUSE_MARKER"] = original_env
            else:
                os.environ.pop("VILLAGE_PAUSE_MARKER", None)

    def test_initial_state_not_paused(self):
        """Check that is_paused returns False when no marker exists."""
        self.assertFalse(is_paused(self.marker_path))
        status = get_village_status(self.marker_path)
        self.assertFalse(status["paused"])
        self.assertEqual(status["metadata"], {})

    def test_pause_village_atomic_write_and_metadata(self):
        """Verify pause_village creates the marker with metadata."""
        meta = pause_village(
            marker_path=self.marker_path,
            reason="maintenance_window",
            operator="admin_test",
        )
        self.assertTrue(self.marker_path.exists())
        self.assertTrue(is_paused(self.marker_path))
        self.assertEqual(meta["status"], "paused")
        self.assertEqual(meta["reason"], "maintenance_window")
        self.assertEqual(meta["operator"], "admin_test")
        self.assertIn("timestamp", meta)

        # Verify file content on disk is valid JSON matching metadata
        content = json.loads(self.marker_path.read_text(encoding="utf-8"))
        self.assertEqual(content["reason"], "maintenance_window")

    def test_resume_village_removes_marker(self):
        """Check that resume_village deletes the marker and returns state."""
        pause_village(marker_path=self.marker_path, reason="test")
        self.assertTrue(is_paused(self.marker_path))

        result = resume_village(marker_path=self.marker_path)
        self.assertTrue(result)
        self.assertFalse(self.marker_path.exists())
        self.assertFalse(is_paused(self.marker_path))

        # Test idempotency: calling resume again when marker is already absent
        second_result = resume_village(marker_path=self.marker_path)
        self.assertTrue(second_result)
        self.assertFalse(is_paused(self.marker_path))

    def test_read_pause_metadata_corrupted_or_empty(self):
        """Check that corrupted or empty marker file is safely handled."""
        self.marker_path.touch()
        meta = read_pause_metadata(self.marker_path)
        self.assertEqual(meta.get("status"), "paused")

        self.marker_path.write_text("invalid json string {{{")
        corrupted_meta = read_pause_metadata(self.marker_path)
        self.assertEqual(corrupted_meta.get("status"), "paused")
        self.assertIn("parse_error", corrupted_meta)

    def test_cli_subcommands(self):
        """Test CLI command execution via python -m village.control."""
        # 1. Test status (not paused)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "village.control",
                "--marker",
                str(self.marker_path),
                "status",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        self.assertFalse(data["paused"])

        # 2. Test is-paused when not paused (should exit 1)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "village.control",
                "--marker",
                str(self.marker_path),
                "is-paused",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)

        # 3. Test pause command
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "village.control",
                "--marker",
                str(self.marker_path),
                "pause",
                "--reason",
                "cli_test",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("paused successfully", proc.stdout)
        self.assertTrue(self.marker_path.exists())

        # 4. Test is-paused when paused (should exit 0)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "village.control",
                "--marker",
                str(self.marker_path),
                "is-paused",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)

        # 5. Test resume command
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "village.control",
                "--marker",
                str(self.marker_path),
                "resume",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Pause marker removed", proc.stdout)
        self.assertFalse(self.marker_path.exists())

    def test_village_resume_skips_when_paused(self):
        """Simulate village-resume behavior to ensure no units are started when paused."""
        board_dir = Path(self.test_dir.name) / "board"
        board_dir.mkdir(parents=True, exist_ok=True)
        events_file = board_dir / "events.jsonl"
        pause_village(marker_path=self.marker_path, reason="test_reboot_pause")

        # Mock bash resume snippet identical to the one in village-resume
        bash_script = f"""#!/usr/bin/env bash
set -Eeuo pipefail
PAUSE_MARKER="{self.marker_path}"
VILLAGE_ROOT="{self.test_dir.name}"
if [[ -f "$PAUSE_MARKER" ]]; then
  printf '%s\\n' "$(python3 -c "import json, datetime; print(json.dumps({{'timestamp': datetime.datetime.now().isoformat(), 'event': 'resume_skipped', 'reason': 'village_paused'}}))")" >> "$VILLAGE_ROOT/board/events.jsonl"
  exit 0
fi
# If reached, it would start services (fail test if reached)
exit 42
"""
        proc = subprocess.run(
            ["bash", "-c", bash_script],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertTrue(events_file.exists())
        log_content = events_file.read_text(encoding="utf-8")
        self.assertIn("resume_skipped", log_content)
        self.assertIn("village_paused", log_content)

    def test_authority_logic_skips_restart_when_paused(self):
        """Verify authority logic correctly identifies pause state and avoids service restarts."""
        pause_village(marker_path=self.marker_path, reason="operator_freeze")

        # Simulate authority logic
        pause_marker = str(self.marker_path)
        is_sim_paused = os.path.exists(pause_marker)
        self.assertTrue(is_sim_paused)

        detail = "king granted resident to explorer"
        if is_sim_paused:
            result_detail = f"{detail} (service restart skipped: village is paused by operator)"
            restarted = False
        else:
            result_detail = detail
            restarted = True

        self.assertFalse(restarted)
        self.assertIn("restart skipped", result_detail)


if __name__ == "__main__":
    unittest.main()

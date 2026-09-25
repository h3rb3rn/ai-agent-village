"""Unit tests for village.config typed configuration and validation module."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from village.config import (
    AgentConfig,
    ConfigValidationError,
    VillageConfig,
    load_config_from_dict,
    load_config_from_file,
    parse_env_dict,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


class TestVillageConfig(unittest.TestCase):
    """Test suite for typed configuration parsing, validation, and error diagnostics."""

    def setUp(self):
        """Set up temporary directory for test configuration files."""
        self.test_dir = tempfile.TemporaryDirectory()
        self.env_file = Path(self.test_dir.name) / ".env"

    def tearDown(self):
        """Clean up temporary directory."""
        self.test_dir.cleanup()

    def test_env_example_validates_successfully(self):
        """Verify that the repository .env.example passes all validation rules."""
        example_path = REPO_ROOT / ".env.example"
        cfg = load_config_from_file(example_path)
        self.assertEqual(cfg.webui_port, 8080)
        self.assertEqual(cfg.memory_port, 8090)
        self.assertGreaterEqual(len(cfg.agents), 1)

    def test_default_config_is_valid(self):
        """Verify default VillageConfig has no validation errors."""
        cfg = VillageConfig()
        errors = cfg.validate()
        self.assertEqual(errors, [])

    def test_invalid_ports(self):
        """Check validation errors on out-of-range ports and collisions."""
        # Port out of range
        with self.assertRaises(ConfigValidationError) as ctx:
            load_config_from_dict({"VILLAGE_WEBUI_PORT": "70000"})
        self.assertTrue(any("webui_port" in e and "out of valid TCP port range" in e for e in ctx.exception.errors))

        # Port collision
        with self.assertRaises(ConfigValidationError) as ctx:
            load_config_from_dict({"VILLAGE_WEBUI_PORT": "8080", "MEMORY_PORT": "8080"})
        self.assertTrue(any("Port collision" in e for e in ctx.exception.errors))

    def test_invalid_timeouts(self):
        """Check validation errors on negative or out-of-bounds timeouts."""
        with self.assertRaises(ConfigValidationError) as ctx:
            load_config_from_dict({"VILLAGE_CYCLE_SECONDS": "0"})
        self.assertTrue(any("cycle_seconds" in e for e in ctx.exception.errors))

        with self.assertRaises(ConfigValidationError) as ctx:
            load_config_from_dict({"VILLAGE_COMMAND_TIMEOUT_SECONDS": "-10"})
        self.assertTrue(any("command_timeout_seconds" in e for e in ctx.exception.errors))

    def test_invalid_think_levels(self):
        """Check that unsupported think levels are rejected with clear diagnostics."""
        with self.assertRaises(ConfigValidationError) as ctx:
            load_config_from_dict({"VILLAGE_DEFAULT_THINK_LEVEL": "ultra"})
        self.assertTrue(any("think_level 'ultra' invalid" in e for e in ctx.exception.errors))

        # Test agent-level invalid think level
        with self.assertRaises(ConfigValidationError) as ctx:
            load_config_from_dict({
                "OLLAMA_AGENT_01_NAME": "scout",
                "OLLAMA_AGENT_01_THINK_LEVEL": "extreme",
            })
        self.assertTrue(any("think_level 'extreme'" in e for e in ctx.exception.errors))

    def test_valid_think_levels(self):
        """Verify all accepted think levels pass validation."""
        for level in ("off", "low", "medium", "high", "max"):
            cfg = load_config_from_dict({"VILLAGE_DEFAULT_THINK_LEVEL": level})
            self.assertEqual(cfg.default_think_level, level)

    def test_agent_role_validation(self):
        """Verify social role validation for agents."""
        with self.assertRaises(ConfigValidationError) as ctx:
            load_config_from_dict({
                "OLLAMA_AGENT_01_NAME": "scout",
                "OLLAMA_AGENT_01_ROLE": "warlord",
            })
        self.assertTrue(any("invalid role 'warlord'" in e for e in ctx.exception.errors))

    def test_agent_context_window_bounds(self):
        """Check bounds checking on context window tokens."""
        with self.assertRaises(ConfigValidationError) as ctx:
            load_config_from_dict({
                "OLLAMA_AGENT_01_NAME": "scout",
                "OLLAMA_AGENT_01_NUM_CTX": "100",  # below 512
            })
        self.assertTrue(any("num_ctx 100 out of bounds" in e for e in ctx.exception.errors))

    def test_cli_config_validation(self):
        """Test CLI command execution via python -m village.config."""
        # 1. Valid file
        valid_env = "VILLAGE_WEBUI_PORT=8080\nMEMORY_PORT=8090\n"
        self.env_file.write_text(valid_env, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "village.config", str(self.env_file)],
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Configuration valid", proc.stdout)

        # 2. Invalid file (should exit 1)
        invalid_env = "VILLAGE_WEBUI_PORT=99999\n"
        self.env_file.write_text(invalid_env, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "village.config", str(self.env_file)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Configuration validation FAILED", proc.stderr)


if __name__ == "__main__":
    unittest.main()

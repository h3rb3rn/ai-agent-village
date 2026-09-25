"""Unit tests for village.security credential isolation and secret hygiene."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from village.security import (
    check_credential_file_permissions,
    mask_secret,
    redact_text,
    sanitize_tool_env,
)


class TestVillageSecurity(unittest.TestCase):
    """Test suite for credential sanitization, masking, and environment hygiene."""

    def test_mask_secret(self) -> None:
        """Verify secret masking behavior for short, long, and empty values."""
        self.assertEqual(mask_secret(""), "<empty>")
        self.assertEqual(mask_secret(None), "<empty>")
        self.assertEqual(mask_secret("short"), "<redacted>")
        self.assertEqual(mask_secret("12345678"), "<redacted>")
        self.assertEqual(mask_secret("supersecretkey12345"), "sup...2345")

    def test_redact_bearer_tokens(self) -> None:
        """Ensure Authorization Bearer headers and text tokens are masked."""
        raw_log = "Sending request with header Authorization: Bearer sk-ant-api03-abcdef1234567890 to endpoint"
        redacted = redact_text(raw_log)
        self.assertNotIn("sk-ant-api03-abcdef1234567890", redacted)
        self.assertIn("Authorization: Bearer <redacted>", redacted)

    def test_redact_key_value_assignments(self) -> None:
        """Ensure inline api_key, token, password assignments are masked."""
        raw = "Error: invalid api_key='sk-secret998877' in config password: 'supersecretpass'"
        redacted = redact_text(raw)
        self.assertNotIn("sk-secret998877", redacted)
        self.assertNotIn("supersecretpass", redacted)
        self.assertIn("<redacted>", redacted)

    def test_redact_custom_secrets(self) -> None:
        """Ensure explicitly supplied canaries and tokens are masked."""
        canary = "CANARY_TOKEN_987654321"
        raw = f"Fatal output trace: leaked {canary} during computation"
        redacted = redact_text(raw, custom_secrets=[canary])
        self.assertNotIn(canary, redacted)
        self.assertIn("<redacted>", redacted)

    def test_sanitize_tool_env(self) -> None:
        """Verify that tool subprocess environments exclude secret credentials."""
        base_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/home/village",
            "API_TOKEN": "secret-village-api-token",
            "OLLAMA_API_TOKEN": "secret-ollama-token",
            "MEMORY_AGENT_TOKEN": "secret-memory-token",
            "CHROMA_SECRET": "topsecret",
            "DB_PASSWORD": "dbpass1234",
            "SERVICE_KEY": "some-service-key",
            "SAFE_VAR": "hello-world",
            "SSH_KEYGEN": "/usr/bin/ssh-keygen",
        }
        sanitized = sanitize_tool_env(base_env)

        # Secrets must be removed
        self.assertNotIn("API_TOKEN", sanitized)
        self.assertNotIn("OLLAMA_API_TOKEN", sanitized)
        self.assertNotIn("MEMORY_AGENT_TOKEN", sanitized)
        self.assertNotIn("CHROMA_SECRET", sanitized)
        self.assertNotIn("DB_PASSWORD", sanitized)
        self.assertNotIn("SERVICE_KEY", sanitized)

        # Safe vars must be retained
        self.assertEqual(sanitized.get("PATH"), "/usr/bin:/bin")
        self.assertEqual(sanitized.get("HOME"), "/home/village")
        self.assertEqual(sanitized.get("SAFE_VAR"), "hello-world")
        self.assertEqual(sanitized.get("SSH_KEYGEN"), "/usr/bin/ssh-keygen")

    def test_credential_file_permissions(self) -> None:
        """Check validation of safe file permissions (0600, 0640 vs world-accessible)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cred_file = Path(tmpdir) / "agent.env"

            # Create file with 0600 (owner only)
            cred_file.write_text("API_TOKEN=synthetic_secret\n", encoding="utf-8")
            cred_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            self.assertTrue(check_credential_file_permissions(cred_file))

            # Modify to 0640 (owner rw, group r)
            cred_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            self.assertTrue(check_credential_file_permissions(cred_file))

            # Modify to 0644 (world readable) -> should fail
            cred_file.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
            self.assertFalse(check_credential_file_permissions(cred_file))

            # Modify to 0666 (world writable) -> should fail
            cred_file.chmod(0o666)
            self.assertFalse(check_credential_file_permissions(cred_file))


if __name__ == "__main__":
    unittest.main()

"""Security, credential hygiene, and secret redaction module for AI Village.

Protects sensitive tokens, API keys, and credentials across tool execution,
event logs, telemetries, and subprocess environments.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

# Standard secret variable patterns to remove from resident tool environments
SECRET_ENV_PATTERNS = [
    re.compile(r".*_TOKEN$", re.IGNORECASE),
    re.compile(r".*_PASSWORD$", re.IGNORECASE),
    re.compile(r".*_SECRET$", re.IGNORECASE),
    re.compile(r".*_KEY$", re.IGNORECASE),
    re.compile(r"^API_TOKEN$", re.IGNORECASE),
    re.compile(r"^OLLAMA_API_TOKEN$", re.IGNORECASE),
    re.compile(r"^MEMORY_AGENT_TOKEN$", re.IGNORECASE),
    re.compile(r"^VILLAGE_SIGNAL_AUTH_.*$", re.IGNORECASE),
]

# Whitelist of safe environment variables that may end with KEY or similar terms
SAFE_ENV_VARIABLES: Set[str] = {
    "SSH_KEYGEN",
    "GPG_KEY",
}

# Regex to detect Bearer token patterns in text or HTTP logs
BEARER_PATTERN = re.compile(r"(Bearer\s+)([A-Za-z0-9_\-\.~+/]{8,})", re.IGNORECASE)

# Regex to detect key=value or key: value credential assignments
KEY_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)\b(password|token|secret|api[_-]?key|bearer)\b\s*[:=]\s*['\"]?([A-Za-z0-9_\-\.~+/]{6,})['\"]?"
)


def mask_secret(secret: str) -> str:
    """Safely mask a secret for logging or diagnostics without exposing the full value.

    Args:
        secret: Raw secret string.

    Returns:
        str: Masked string (e.g. 'sec...xyz') or '<empty>' if blank.
    """
    if not secret:
        return "<empty>"
    secret = str(secret).strip()
    if len(secret) <= 8:
        return "<redacted>"
    return f"{secret[:3]}...{secret[-4:]}"


def redact_text(text: str, custom_secrets: Optional[Iterable[str]] = None) -> str:
    """Mask credentials, tokens, and sensitive patterns in arbitrary text.

    Args:
        text: String containing potentially sensitive logs, outputs, or error details.
        custom_secrets: Optional list of explicit secret strings to replace.

    Returns:
        str: Sanitized text with credentials replaced by '<redacted>'.
    """
    if not text:
        return ""

    sanitized = text

    # Redact explicit custom secrets first
    if custom_secrets:
        for secret in custom_secrets:
            if secret and len(str(secret).strip()) >= 3:
                sanitized = sanitized.replace(str(secret).strip(), "<redacted>")

    # Redact Bearer tokens in headers or logs
    sanitized = BEARER_PATTERN.sub(r"\1<redacted>", sanitized)

    # Redact key=value or json assignments containing secret terms
    sanitized = KEY_VALUE_SECRET_PATTERN.sub(r"\1=<redacted>", sanitized)

    return sanitized


def sanitize_tool_env(env: Dict[str, str]) -> Dict[str, str]:
    """Create a sanitized copy of an environment dictionary for tool subprocesses.

    Removes any credentials, private tokens, and passwords so that resident shell
    commands cannot inspect or leak agent credentials via `env` or `/proc/self/environ`.

    Args:
        env: Base environment dictionary.

    Returns:
        dict[str, str]: Sanitized environment dictionary.
    """
    sanitized: Dict[str, str] = {}
    for key, value in env.items():
        if key in SAFE_ENV_VARIABLES:
            sanitized[key] = value
            continue

        is_secret = any(pat.match(key) for pat in SECRET_ENV_PATTERNS)
        if not is_secret:
            sanitized[key] = value

    return sanitized


def check_credential_file_permissions(path: Path) -> bool:
    """Check that a credential file is accessible only by authorized owner/group (mode 0600 or 0640).

    Args:
        path: Path to target credential file.

    Returns:
        bool: True if permissions are restrictive and safe, False if group/world writable.
    """
    if not path.exists():
        return False
    stat = path.stat()
    mode = stat.st_mode
    # Must not be world-readable (0004), world-writable (0002), or world-executable (0001)
    if mode & 0o007:
        return False
    # Must not be group-writable (0020)
    if mode & 0o020:
        return False
    return True

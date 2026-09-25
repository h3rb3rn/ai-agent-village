"""Configuration management, typing, and validation for AI Village.

Implements a strongly typed configuration contract with range checks,
field validation, and clear error diagnostics for both global village settings
and per-agent overrides.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# Allowed thinking levels supported by the runtime
VALID_THINK_LEVELS: Set[str] = {"off", "low", "medium", "high", "max"}

# Allowed agent social roles
VALID_ROLES: Set[str] = {"resident", "builder", "steward", "king", "specialist"}

# Regex pattern for keep-alive durations (e.g. "10m", "24h", "0", "-1", "60s")
KEEP_ALIVE_PATTERN = re.compile(r"^(-1|0|\d+[smhd]?)$")


class ConfigValidationError(ValueError):
    """Exception raised when configuration parameters fail validation."""

    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


@dataclass
class AgentConfig:
    """Strongly typed configuration for an individual village agent."""

    agent_id: str
    name: str
    role: str = "resident"
    url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:7b"
    num_ctx: int = 8192
    num_predict: int = 768
    think_level: str = "medium"
    keep_alive: str = "10m"
    focus: str = "general exploration"
    temperament: str = "inquisitive and cooperative"
    api_type: str = "ollama"
    api_token: str = ""

    def validate(self) -> List[str]:
        """Validate agent configuration values against ranges and contracts.

        Returns:
            list[str]: Validation error messages (empty if valid).
        """
        errors: List[str] = []
        if not re.match(r"^\d{2}-[a-z0-9_-]+$", self.agent_id):
            errors.append(f"Agent {self.agent_id}: invalid ID format; expected 'NN-name'")

        if not self.name or not re.match(r"^[a-z0-9_-]+$", self.name):
            errors.append(f"Agent {self.agent_id}: invalid name '{self.name}'")

        if self.role not in VALID_ROLES:
            errors.append(
                f"Agent {self.agent_id}: invalid role '{self.role}'; must be one of {sorted(VALID_ROLES)}"
            )

        if not (self.url.startswith("http://") or self.url.startswith("https://")):
            errors.append(f"Agent {self.agent_id}: invalid URL '{self.url}'; must start with http:// or https://")

        if not self.model or len(self.model.strip()) == 0:
            errors.append(f"Agent {self.agent_id}: model name cannot be empty")

        if self.num_ctx < 512 or self.num_ctx > 262144:
            errors.append(f"Agent {self.agent_id}: num_ctx {self.num_ctx} out of bounds (512 - 262144)")

        if self.num_predict < 32 or self.num_predict > 32768:
            errors.append(f"Agent {self.agent_id}: num_predict {self.num_predict} out of bounds (32 - 32768)")

        if self.think_level not in VALID_THINK_LEVELS:
            errors.append(
                f"Agent {self.agent_id}: invalid think_level '{self.think_level}'; must be one of {sorted(VALID_THINK_LEVELS)}"
            )

        if not KEEP_ALIVE_PATTERN.match(self.keep_alive):
            errors.append(
                f"Agent {self.agent_id}: invalid keep_alive '{self.keep_alive}'; expected e.g. 10m, 24h, 0, -1"
            )

        if self.api_type.lower() not in ("ollama", "openai"):
            errors.append(
                f"Agent {self.agent_id}: invalid api_type '{self.api_type}'; must be 'ollama' or 'openai'"
            )

        return errors


@dataclass
class VillageConfig:
    """Strongly typed configuration for the AI Village environment."""

    village_root: Path = Path("/var/lib/ai-village")
    cycle_seconds: int = 90
    command_timeout_seconds: int = 3600
    ollama_timeout_seconds: int = 1800
    offline_retry_seconds: int = 120
    max_output_bytes: int = 16384
    board_tail_lines: int = 16
    default_num_ctx: int = 8192
    default_num_predict: int = 768
    default_think_level: str = "medium"
    default_keep_alive: str = "10m"
    min_free_memory_mib: int = 4096
    webui_enabled: bool = True
    webui_bind: str = "0.0.0.0"
    webui_port: int = 8080
    webui_max_message_chars: int = 4000
    signal_auth_user: str = ""
    memory_gateway_enabled: bool = True
    memory_port: int = 8090
    memory_writes_per_hour: int = 120
    memory_max_results: int = 12
    agents: Dict[int, AgentConfig] = field(default_factory=dict)

    def validate(self) -> List[str]:
        """Validate entire configuration contract including global values and all agents.

        Returns:
            list[str]: Validation error messages (empty if valid).
        """
        errors: List[str] = []

        if self.cycle_seconds < 1 or self.cycle_seconds > 86400:
            errors.append(f"cycle_seconds {self.cycle_seconds} out of range (1 - 86400)")

        if self.command_timeout_seconds < 0 or self.command_timeout_seconds > 86400:
            errors.append(f"command_timeout_seconds {self.command_timeout_seconds} out of range (0 - 86400)")

        if self.ollama_timeout_seconds < 5 or self.ollama_timeout_seconds > 14400:
            errors.append(f"ollama_timeout_seconds {self.ollama_timeout_seconds} out of range (5 - 14400)")

        if self.offline_retry_seconds < 1 or self.offline_retry_seconds > 3600:
            errors.append(f"offline_retry_seconds {self.offline_retry_seconds} out of range (1 - 3600)")

        if self.max_output_bytes < 512 or self.max_output_bytes > 1048576:
            errors.append(f"max_output_bytes {self.max_output_bytes} out of range (512 - 1048576)")

        if self.board_tail_lines < 1 or self.board_tail_lines > 500:
            errors.append(f"board_tail_lines {self.board_tail_lines} out of range (1 - 500)")

        if self.default_num_ctx < 512 or self.default_num_ctx > 262144:
            errors.append(f"default_num_ctx {self.default_num_ctx} out of range (512 - 262144)")

        if self.default_num_predict < 32 or self.default_num_predict > 32768:
            errors.append(f"default_num_predict {self.default_num_predict} out of range (32 - 32768)")

        if self.default_think_level not in VALID_THINK_LEVELS:
            errors.append(
                f"default_think_level '{self.default_think_level}' invalid; must be one of {sorted(VALID_THINK_LEVELS)}"
            )

        if not KEEP_ALIVE_PATTERN.match(self.default_keep_alive):
            errors.append(f"default_keep_alive '{self.default_keep_alive}' invalid; expected e.g. 10m, 24h, 0, -1")

        if self.min_free_memory_mib < 0:
            errors.append(f"min_free_memory_mib {self.min_free_memory_mib} cannot be negative")

        if self.webui_port < 1 or self.webui_port > 65535:
            errors.append(f"webui_port {self.webui_port} out of valid TCP port range (1 - 65535)")

        if self.memory_port < 1 or self.memory_port > 65535:
            errors.append(f"memory_port {self.memory_port} out of valid TCP port range (1 - 65535)")

        if self.webui_port == self.memory_port:
            errors.append(f"Port collision: webui_port and memory_port are identical ({self.webui_port})")

        # Validate all registered agents
        for index, agent_cfg in self.agents.items():
            agent_errors = agent_cfg.validate()
            errors.extend(agent_errors)

        return errors


def parse_env_dict(content: str) -> Dict[str, str]:
    """Parse raw environment file content into key-value dictionary.

    Args:
        content: Raw text content of .env file.

    Returns:
        dict[str, str]: Stripped key-value mapping.
    """
    values: Dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, value = line.split("=", 1)
        # Parse shell quoting correctly
        tokens = shlex.split(value, comments=True)
        values[key.strip()] = " ".join(tokens) if tokens else ""
    return values


def load_config_from_dict(env_vars: Dict[str, str]) -> VillageConfig:
    """Instantiate a validated VillageConfig from a key-value mapping.

    Args:
        env_vars: Dictionary containing environment variable settings.

    Returns:
        VillageConfig: Fully loaded configuration object.

    Raises:
        ConfigValidationError: If configuration fails validation.
    """
    def _get_int(key: str, default: int) -> int:
        val = env_vars.get(key)
        if val is None or val == "":
            return default
        try:
            return int(val)
        except ValueError:
            raise ConfigValidationError([f"Environment variable '{key}' must be integer, got: '{val}'"])

    def _get_bool(key: str, default: bool) -> bool:
        val = env_vars.get(key)
        if val is None:
            return default
        return val.lower() in ("true", "1", "yes", "on")

    cfg = VillageConfig(
        village_root=Path(env_vars.get("VILLAGE_ROOT", "/var/lib/ai-village")),
        cycle_seconds=_get_int("VILLAGE_CYCLE_SECONDS", 90),
        command_timeout_seconds=_get_int("VILLAGE_COMMAND_TIMEOUT_SECONDS", 3600),
        ollama_timeout_seconds=_get_int("VILLAGE_OLLAMA_TIMEOUT_SECONDS", 1800),
        offline_retry_seconds=_get_int("VILLAGE_OFFLINE_RETRY_SECONDS", 120),
        max_output_bytes=_get_int("VILLAGE_MAX_OUTPUT_BYTES", 16384),
        board_tail_lines=_get_int("VILLAGE_BOARD_TAIL_LINES", 16),
        default_num_ctx=_get_int("VILLAGE_DEFAULT_NUM_CTX", 8192),
        default_num_predict=_get_int("VILLAGE_DEFAULT_NUM_PREDICT", 768),
        default_think_level=env_vars.get("VILLAGE_DEFAULT_THINK_LEVEL", "medium"),
        default_keep_alive=env_vars.get("VILLAGE_DEFAULT_KEEP_ALIVE", "10m"),
        min_free_memory_mib=_get_int("VILLAGE_MIN_FREE_MEMORY_MIB", 4096),
        webui_enabled=_get_bool("VILLAGE_WEBUI_ENABLED", True),
        webui_bind=env_vars.get("VILLAGE_WEBUI_BIND", "0.0.0.0"),
        webui_port=_get_int("VILLAGE_WEBUI_PORT", 8080),
        webui_max_message_chars=_get_int("VILLAGE_WEBUI_MAX_MESSAGE_CHARS", 4000),
        signal_auth_user=env_vars.get("VILLAGE_SIGNAL_AUTH_USER", ""),
        memory_gateway_enabled=_get_bool("MEMORY_GATEWAY_ENABLED", True),
        memory_port=_get_int("MEMORY_PORT", 8090),
        memory_writes_per_hour=_get_int("MEMORY_WRITES_PER_HOUR", 120),
        memory_max_results=_get_int("MEMORY_MAX_RESULTS", 12),
    )

    # Detect per-agent configurations (supports both OLLAMA_AGENT_1_ and OLLAMA_AGENT_01_)
    agent_prefixes: Dict[int, str] = {}
    agent_key_pattern = re.compile(
        r"^OLLAMA_AGENT_(\d+)_(NAME|MODEL|ROLE|URL|NUM_CTX|NUM_PREDICT|THINK_LEVEL|KEEP_ALIVE|API_TYPE|API_TOKEN)$"
    )
    for key in env_vars.keys():
        match = agent_key_pattern.match(key)
        if match:
            idx = int(match.group(1))
            raw_digits = match.group(1)
            agent_prefixes[idx] = f"OLLAMA_AGENT_{raw_digits}_"

    for index, prefix in sorted(agent_prefixes.items()):
        def _get_agent_val(field_name: str, default: Any) -> Any:
            # Check detected prefix, unpadded, and padded variants
            for p in (prefix, f"OLLAMA_AGENT_{index}_", f"OLLAMA_AGENT_{index:02d}_"):
                if (p + field_name) in env_vars:
                    return env_vars[p + field_name]
            return default

        def _get_agent_int(field_name: str, default: int) -> int:
            val = _get_agent_val(field_name, None)
            if val is None or val == "":
                return default
            try:
                return int(val)
            except ValueError:
                raise ConfigValidationError([f"Agent {index} {field_name} must be integer, got '{val}'"])

        name = _get_agent_val("NAME", f"agent{index}")
        agent_id = f"{index:02d}-{name}"

        agent_cfg = AgentConfig(
            agent_id=agent_id,
            name=name,
            role=_get_agent_val("ROLE", "resident"),
            url=_get_agent_val("URL", "http://127.0.0.1:11434"),
            model=_get_agent_val("MODEL", "qwen2.5:7b"),
            num_ctx=_get_agent_int("NUM_CTX", cfg.default_num_ctx),
            num_predict=_get_agent_int("NUM_PREDICT", cfg.default_num_predict),
            think_level=_get_agent_val("THINK_LEVEL", cfg.default_think_level),
            keep_alive=_get_agent_val("KEEP_ALIVE", cfg.default_keep_alive),
            focus=_get_agent_val("FOCUS", "general exploration"),
            temperament=_get_agent_val("TEMPERAMENT", "inquisitive and cooperative"),
            api_type=_get_agent_val("API_TYPE", "ollama"),
            api_token=_get_agent_val("API_TOKEN", ""),
        )
        cfg.agents[index] = agent_cfg

    validation_errors = cfg.validate()
    if validation_errors:
        raise ConfigValidationError(validation_errors)

    return cfg


def load_config_from_file(path: Path) -> VillageConfig:
    """Load and validate VillageConfig from an environment file.

    Args:
        path: Path to .env file.

    Returns:
        VillageConfig: Validated configuration object.
    """
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {path}")
    content = path.read_text(encoding="utf-8")
    env_vars = parse_env_dict(content)
    return load_config_from_dict(env_vars)


def main() -> int:
    """CLI entry point for configuration inspection and validation."""
    parser = argparse.ArgumentParser(
        prog="village-config",
        description="Configuration validation and inspection for AI Village.",
    )
    parser.add_argument("env_file", type=Path, help="Path to .env configuration file")
    parser.add_argument("--json", action="store_true", help="Output validated configuration as JSON")

    args = parser.parse_args()

    try:
        cfg = load_config_from_file(args.env_file)
        if args.json:
            data = asdict(cfg)
            data["village_root"] = str(data["village_root"])
            # Format agent sub-dictionaries
            print(json.dumps(data, indent=2))
        else:
            print(f"Configuration valid: {args.env_file}")
            print(f"Village root: {cfg.village_root}")
            print(f"Cycle: {cfg.cycle_seconds}s, Command timeout: {cfg.command_timeout_seconds}s")
            print(f"WebUI port: {cfg.webui_port}, Memory port: {cfg.memory_port}")
            print(f"Registered agents: {len(cfg.agents)}")
            for idx, a in cfg.agents.items():
                print(f"  [{idx:02d}] {a.name} ({a.role}) -> {a.model} @ {a.url} (ctx: {a.num_ctx}, think: {a.think_level})")
        return 0
    except ConfigValidationError as err:
        print(f"Configuration validation FAILED for {args.env_file}:", file=sys.stderr)
        for e in err.errors:
            print(f" - {e}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error loading configuration: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

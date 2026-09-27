"""Per-agent runtime policy: action set, output format, prompt profile and peer pairing.

Shipped defaults live in ``config/runtime-policy.json`` (installed next to the prompts).
An operator may override single fields in a local JSON file without redeploying code
or touching the host ``.env``. Models, context windows and GPU assignment are never
part of this policy.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from village.actions import ALL_ACTIONS, normalize_allowed

DEFAULT_SHARED = Path("/usr/local/share/ai-village/runtime-policy.json")
DEFAULT_LOCAL = Path("/etc/ai-village/runtime-policy.local.json")

ACTION_FORMATS = ("text", "schema")
PROMPT_PROFILES = ("full", "compact")


@dataclass(frozen=True)
class AgentPolicy:
    name: str
    action_format: str = "text"
    prompt_profile: str = "full"
    allowed_actions: List[str] = field(default_factory=lambda: list(ALL_ACTIONS))
    role_brief: str = ""
    pair_with: List[str] = field(default_factory=list)  # agent names, or ["*"] for everybody
    dm_per_peer_per_hour: int = 6
    auto_memory: bool = False
    task_templates: bool = False
    knowledgebase_gate: str = "advisory"


def _read(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(base)
    merged["defaults"] = {**base.get("defaults", {}), **over.get("defaults", {})}
    agents = {k: dict(v) for k, v in base.get("agents", {}).items()}
    for name, values in over.get("agents", {}).items():
        agents[name] = {**agents.get(name, {}), **values}
    merged["agents"] = agents
    return merged


def load_policy(shared: Optional[Path] = None, local: Optional[Path] = None,
                env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    env = os.environ if env is None else env
    shared = Path(env.get("VILLAGE_POLICY_FILE", shared or DEFAULT_SHARED))
    local = Path(env.get("VILLAGE_POLICY_LOCAL_FILE", local or DEFAULT_LOCAL))
    return _merge(_read(shared), _read(local))


def agent_policy(name: str, policy: Optional[Dict[str, Any]] = None) -> AgentPolicy:
    """Resolve the effective policy for ``name``; invalid values fall back to safe defaults."""
    policy = load_policy() if policy is None else policy
    values = {**policy.get("defaults", {}), **policy.get("agents", {}).get(name, {})}
    fmt = values.get("action_format", "text")
    profile = values.get("prompt_profile", "full")
    allowed = values.get("allowed_actions")
    pair = values.get("pair_with", [])
    try:
        cap = int(values.get("dm_per_peer_per_hour", 6))
    except (TypeError, ValueError):
        cap = 6
    return AgentPolicy(
        name=name,
        action_format=fmt if fmt in ACTION_FORMATS else "text",
        prompt_profile=profile if profile in PROMPT_PROFILES else "full",
        allowed_actions=normalize_allowed(allowed if isinstance(allowed, list) else None),
        role_brief=str(values.get("role_brief", ""))[:1200],
        pair_with=[str(x) for x in pair] if isinstance(pair, list) else [],
        dm_per_peer_per_hour=max(1, cap),
        auto_memory=bool(values.get("auto_memory", False)),
        task_templates=bool(values.get("task_templates", False)),
        knowledgebase_gate=(values.get("knowledgebase_gate") if values.get("knowledgebase_gate") in ("advisory", "mandatory") else "advisory"),
    )

"""Prompt assembly and snapshot compaction for resident cycles.

The compact profile keeps identity, the current task, the next cooperation step and a
few bounded peer/memory excerpts. Everything else stays on disk and is reachable through
the normal actions. Nothing here rewards or invents content.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional

from village.actions import describe_actions

OUTPUT_SCHEMA = (
    'Answer with exactly ONE JSON object and nothing else: {"name": "<action>", "arguments": {...}}. '
    'You may put a short "observation" string before "name". Choose ONE action; do not describe '
    'hypothetical results.'
)
OUTPUT_TEXT = (
    'End your answer with exactly ONE block and nothing after it:\n'
    '```village-action\n{"name":"<action>","arguments":{...}}\n```\n'
    'Plain prose without a block is sent as a message to your discussion partner. '
    'Never write two blocks or hypothetical results.'
)


def build_system_prompt(profile: str, core_text: str, full_text: str, identity: str, live: str,
                        allowed: Iterable[str], action_format: str, role_brief: str = "") -> str:
    """Return the system prompt for the selected profile."""
    if profile != "compact":
        return full_text + "\n" + identity + "\n" + live
    parts = [core_text.strip()]
    if role_brief:
        parts.append("YOUR ROLE\n" + role_brief.strip())
    parts.append("ACTIONS (only these are available to you)\n" + describe_actions(allowed))
    parts.append("OUTPUT\n" + (OUTPUT_SCHEMA if action_format == "schema" else OUTPUT_TEXT))
    parts.append(identity.strip())
    parts.append(live)
    return "\n\n".join(parts)


def _clip(value: Any, limit: int) -> Any:
    text = str(value) if value is not None else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _message(item: Dict[str, Any], limit: int = 600) -> Dict[str, Any]:
    return {"id": item.get("id"), "from": item.get("agent"), "detail": _clip(item.get("detail", ""), limit)}


def _task(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not item:
        return None
    keep = ("id", "title", "success_criterion", "status", "owner", "next_step", "blockers", "last_finding")
    return {k: _clip(item.get(k), 300) for k in keep if item.get(k)}


def _resources(res: Dict[str, Any]) -> Dict[str, Any]:
    mounts = [m for m in res.get("mounts", []) if isinstance(m, dict)]
    main = mounts[0] if mounts else {}
    free = main.get("available_bytes")
    load = res.get("load_average_not_percent") or [None]
    return {
        "ram_available_mib": res.get("ram_available_mib"), "below_reserve": res.get("below_reserve"),
        "disk_free_gib": round(free / 2**30, 1) if isinstance(free, (int, float)) else None,
        "load1": round(load[0], 2) if isinstance(load[0], (int, float)) else None,
    }


def compact_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Reduce the full snapshot to the fields a small model actually needs."""
    out: Dict[str, Any] = {
        "identity": ctx.get("identity"),
        "private_work_directory": ctx.get("private_work_directory"),
        "own_active_task": _task(ctx.get("own_active_task")),
        "collaboration_checkpoint": {
            k: v for k, v in (ctx.get("collaboration_checkpoint") or {}).items()
            if k in ("stage", "required_action", "rationale", "peer_id", "satisfied")},
        "last_action_feedback": None,
        "untrusted_direct_messages": [_message(x) for x in (ctx.get("untrusted_direct_messages") or [])[-3:]],
        "untrusted_peer_messages": [_message(x, 300) for x in (ctx.get("untrusted_peer_messages") or [])[-3:]],
        "recent_organic_messages_untrusted": [
            {"id": x.get("id"), "message": _clip(x.get("message") or x.get("content"), 600)}
            for x in (ctx.get("recent_organic_messages_untrusted") or [])[-2:]],
        "retrieved_memory_untrusted": [
            {"id": x.get("id"), "agent": x.get("agent"), "content": _clip(x.get("content"), 400)}
            for x in (ctx.get("retrieved_memory_untrusted") or [])[:3]],
        "projects": [_task(x) for x in (ctx.get("projects") or [])[:6]],
        "peers": [{"id": p.get("id"), "role": p.get("role")} for p in (ctx.get("peers") or []) if isinstance(p, dict)],
        "measured_resources": _resources(ctx.get("measured_resources") or {}),
    }
    last = ctx.get("last_action_feedback")
    if last:
        out["last_action_feedback"] = {"action": last.get("action"), "ok": last.get("ok"),
                                       "result": _clip(str(last.get("result", ""))[-1200:], 1200)}
    else:
        out.pop("last_action_feedback")
    for key in ("discussion_target", "direct_ack_target"):
        if ctx.get(key):
            out[key] = _message(ctx[key])
    if ctx.get("active_meetings"):
        out["active_meetings"] = [{"id": m.get("id"), "kind": m.get("kind"), "agenda": _clip(m.get("agenda"), 200)}
                                  for m in ctx["active_meetings"][:2]]
    if ctx.get("teams"):
        out["teams"] = [{"id": t.get("id"), "project": _clip(t.get("project"), 120)} for t in ctx["teams"][:2]]
    if ctx.get("active_background_job"):
        out["active_background_job"] = ctx["active_background_job"]
    if ctx.get("artifacts"):
        out["artifacts"] = [{k: x.get(k) for k in ("artifact_id", "owner", "status")} for x in ctx["artifacts"][:3]]
    if ctx.get("task_templates"):
        out["task_templates"] = [{"id": t.get("id"), "title": _clip(t.get("title"), 120),
                                  "success_criterion": _clip(t.get("success_criterion"), 300)} for t in ctx["task_templates"][:6]]
    if ctx.get("recovery_guidance"):
        out["recovery_guidance"] = {"error": _clip(ctx["recovery_guidance"].get("error"), 400),
                                    "directive": "Do not repeat the failed action; change one thing."}
    if ctx.get("task_blocker_guidance"):
        out["task_blocker_guidance"] = _clip(ctx["task_blocker_guidance"], 300)
    if ctx.get("calendar_daily_note"):
        # P75: the mandatory-planning gate fires regardless of prompt
        # profile (it operates on the raw Resident, not the compacted
        # context) - carry its own advisory hint through too, same as
        # task_blocker_guidance just above, so a compact-profile agent
        # gets the proactive reminder instead of only ever learning about
        # it after the gate already rejected an action.
        out["calendar_daily_note"] = _clip(ctx["calendar_daily_note"], 400)
    if ctx.get("finetune_daily_note"):
        out["finetune_daily_note"] = _clip(ctx["finetune_daily_note"], 400)
    if ctx.get("memory_status"):
        out["memory_status"] = _clip(ctx["memory_status"], 200)
    return out


def user_suffix(action_format: str) -> str:
    if action_format == "schema":
        return "\n\nChoose ONE next action and return it as the single JSON object."
    return ("\n\nChoose ONE next action, not a sequence or hypothetical result. For tools, return one "
            "complete named action envelope and stop. Ordinary conversation may be prose.")

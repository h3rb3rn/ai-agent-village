"""Single specification of resident actions: JSON schema, prompt text, argument limits.

The schema is sent to Ollama as ``format`` so the model can only produce one
complete, valid action object. It never fabricates content: it constrains
shape, recipients and lengths only.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

STR = {"type": "string"}


def _s(max_len: int, min_len: int = 1) -> Dict[str, Any]:
    return {"type": "string", "minLength": min_len, "maxLength": max_len}


# name -> (description for the prompt, properties, required)
ACTION_SPECS: Dict[str, Dict[str, Any]] = {
    "execute_bash": {
        "doc": "command -> run one bash command in your private directory; the result arrives next turn",
        "properties": {"command": _s(1500)}, "required": ["command"]},
    "start_job": {
        "doc": "command, timeout_seconds? -> start one long-running background job",
        "properties": {"command": _s(1500), "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 86400}},
        "required": ["command"]},
    "job_status": {
        "doc": "job_id? -> poll your background job",
        "properties": {"job_id": _s(80)}, "required": []},
    "cancel_job": {
        "doc": "job_id? -> cancel your background job",
        "properties": {"job_id": _s(80)}, "required": []},
    "board_message": {
        "doc": "message, recipient (exact peer ID or ALL), reply_to? -> talk to a peer; prefer a named peer",
        "properties": {"message": _s(1500), "recipient": _s(80), "reply_to": _s(120)}, "required": ["message"]},
    "task_operation": {
        "doc": "action=create(title,success_criterion) | claim(task_id) | progress(task_id,last_finding?,next_step?,blockers?) | complete(task_id,evidence) | yield(task_id,evidence)",
        "properties": {
            "action": {"enum": ["create", "claim", "progress", "complete", "yield"]},
            "task_id": _s(80), "title": _s(200), "success_criterion": _s(400), "goal": _s(400),
            "evidence": _s(800), "last_finding": _s(600), "next_step": _s(400), "blockers": _s(400)},
        "required": ["action"]},
    "team_operation": {
        "doc": "operation=create(project,goal,role)|join(team_id)|leave(team_id)|create_subtask(team_id,title,criterion)|claim_subtask(subtask_id)|complete_subtask(subtask_id,evidence)|propose_role(team_id,role,rationale)|vote_role(proposal_id,choice)",
        "properties": {
            "operation": {"enum": ["create", "join", "leave", "create_subtask", "claim_subtask", "complete_subtask", "propose_role", "vote_role"]},
            "project": _s(200), "goal": _s(400), "role": _s(120), "team_id": _s(80), "title": _s(200),
            "criterion": _s(400), "subtask_id": _s(80), "evidence": _s(800), "rationale": _s(400),
            "proposal_id": _s(80), "choice": {"enum": ["accept", "reject"]}},
        "required": ["operation"]},
    "artifact_operation": {
        "doc": "operation=register(artifact_id,file_path,test_description?)|claim_success(artifact_id,test_command?)|verify(artifact_id,test_command,details?)|adopt(artifact_id)|inspect(artifact_id)",
        "properties": {
            "operation": {"enum": ["register", "claim_success", "verify", "adopt", "inspect"]},
            "artifact_id": _s(120), "file_path": _s(400), "test_description": _s(400),
            "test_command": _s(800), "details": _s(600), "verdict_type": _s(60), "task_id": _s(80)},
        "required": ["operation", "artifact_id"]},
    "memory_remember": {
        "doc": "content, kind (observation|lesson|skill), scope (private|shared) -> store a measured observation with its source",
        "properties": {"content": _s(1200), "kind": {"enum": ["observation", "lesson", "skill"]}, "scope": {"enum": ["private", "shared"]}},
        "required": ["content"]},
    "memory_search": {
        "doc": "query, scope? (private|shared) -> search stored memories",
        "properties": {"query": _s(300), "scope": {"enum": ["private", "shared"]}}, "required": ["query"]},
    "research_request": {
        "doc": "source (wikipedia|github|dockerhub|huggingface), query, limit? -> read-only discovery; huggingface returns dataset metadata (license/files) only, never dataset content",
        "properties": {"source": {"enum": ["wikipedia", "github", "dockerhub", "huggingface"]}, "query": _s(300), "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
        "required": ["source", "query"]},
    "meeting_operation": {
        "doc": "operation=report(meeting_id,achieved,evidence,next_step,blockers) | close(meeting_id)",
        "properties": {"operation": {"enum": ["report", "close"]}, "meeting_id": _s(120), "achieved": _s(600),
                       "evidence": _s(600), "next_step": _s(400), "blockers": _s(400)},
        "required": ["operation", "meeting_id"]},
    "calc_operation": {
        "doc": "tool=calc(expression)|unit_convert(value,from_unit,to_unit)|subnet_info(cidr)|hash_digest(text,algorithm?)|stats_summary(numbers); deterministic, exact, no network",
        "properties": {
            "tool": {"enum": ["calc", "unit_convert", "subnet_info", "hash_digest", "stats_summary"]},
            "expression": _s(200), "value": {"type": "number"}, "from_unit": _s(20), "to_unit": _s(20),
            "cidr": _s(60), "text": _s(2000), "algorithm": {"enum": ["sha256", "sha512"]},
            "numbers": {"type": "array", "items": {"type": "number"}, "maxItems": 256}},
        "required": ["tool"]},
    "research_proposal": {
        "doc": "operation=propose(topic,rationale?)|endorse(proposal_id)|list(status?) -> a topic becomes a real, "
              "claimable task only once 3 distinct agents endorse it, chosen by the community, not by one agent; "
              "propose counts as your own first endorsement",
        "properties": {
            "operation": {"enum": ["propose", "endorse", "list"]},
            "topic": _s(200), "rationale": _s(400), "proposal_id": _s(80),
            "status": {"enum": ["open", "adopted"]}},
        "required": ["operation"]},
    "idle": {"doc": "no arguments -> deliberate rest", "properties": {}, "required": []},
}

ALL_ACTIONS: List[str] = list(ACTION_SPECS)


def normalize_allowed(allowed: Optional[Iterable[str]]) -> List[str]:
    """Keep spec order, drop unknown names, and always include ``idle``."""
    wanted = set(allowed) if allowed is not None else set(ALL_ACTIONS)
    result = [name for name in ALL_ACTIONS if name in wanted]
    if "idle" not in result:
        result.append("idle")
    return result


def action_schema(allowed: Optional[Iterable[str]] = None, peers: Optional[List[str]] = None,
                  consult_peer: Optional[str] = None) -> Dict[str, Any]:
    """JSON schema for exactly one action object ``{"observation"?, "name", "arguments"}``.

    ``peers`` restricts ``recipient`` to exact IDs plus ``ALL``; ``consult_peer`` pins the
    recipient of ``board_message`` to one named peer (routing, not content).
    """
    variants = []
    for name in normalize_allowed(allowed):
        spec = ACTION_SPECS[name]
        props = {k: dict(v) for k, v in spec["properties"].items()}
        if name == "board_message":
            if consult_peer:
                props["recipient"] = {"const": consult_peer}
            elif peers:
                props["recipient"] = {"enum": list(dict.fromkeys(list(peers) + ["ALL"]))}
        required = list(spec["required"])
        if name == "board_message" and consult_peer:
            required.append("recipient")
        arguments: Dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
        if required:
            arguments["required"] = required
        variants.append({
            "type": "object",
            "properties": {
                "observation": {"type": "string", "maxLength": 300},
                "name": {"const": name},
                "arguments": arguments,
            },
            "required": ["name", "arguments"],
            "additionalProperties": False,
        })
    return {"oneOf": variants} if len(variants) > 1 else variants[0]


def describe_actions(allowed: Optional[Iterable[str]] = None) -> str:
    """Compact action list for the system prompt, restricted to the agent's role."""
    return "\n".join(f"- {name}: {ACTION_SPECS[name]['doc']}" for name in normalize_allowed(allowed))

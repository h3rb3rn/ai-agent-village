"""Single specification of resident actions: JSON schema, prompt text, argument limits.

The schema is sent to Ollama as ``format`` so the model can only produce one
complete, valid action object. It never fabricates content: it constrains
shape, recipients and lengths only.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from village.calendar import EVENT_KINDS as _CALENDAR_KINDS
from village.calendar import MAX_ATTENDEES as _CALENDAR_MAX_ATTENDEES
from village.calendar import MAX_DURATION_MINUTES as _CALENDAR_MAX_MINUTES
from village.calendar import MIN_DURATION_MINUTES as _CALENDAR_MIN_MINUTES
from village.calendar import RECURRENCES as _CALENDAR_RECURRENCES
from village.finetune import GPU_COUNT as _FINETUNE_GPU_COUNT
from village.finetune import RUN_STATUSES as _FINETUNE_RUN_STATUSES
from village.gazette import CONTRIBUTION_KINDS as _GAZETTE_KINDS
from village.gazette import GAME_SOLUTION_MAX_CHARS as _GAZETTE_GAME_SOLUTION_MAX_CHARS
from village.gazette import GAME_TASK_MAX_CHARS as _GAZETTE_GAME_TASK_MAX_CHARS
from village.gazette import HEADLINE_MAX_CHARS as _GAZETTE_HEADLINE_MAX_CHARS
from village.gazette import MAX_COLUMN_CHARS as _GAZETTE_MAX_COLUMN_CHARS
from village.gazette import MAX_CONTRIBUTION_CHARS as _GAZETTE_MAX_CHARS
from village.gazette import REVIEWER_AGENT as _GAZETTE_REVIEWER
# P74 (bug found while adding 'declare_winner'): this schema is sent to
# Ollama as grammar-constrained ``format`` (see module docstring) - the
# 'kind' enum below used to be a hand-duplicated list that P73 added
# "meetings" to in village/gazette.py's CONTRIBUTION_KINDS but never here,
# which would have silently made kind='meetings' impossible to produce for
# any schema-constrained agent despite being a valid, assigned kind.
# Importing the tuple directly instead of copying it closes that whole
# class of drift for good.

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
    "gazette_operation": {
        # P61: reviewer identity interpolated from village/gazette.py's
        # REVIEWER_AGENT rather than hardcoded - this exact string used to
        # say "09-chronicler only" for weeks after the role moved to
        # 01-king, silently contradicting the runtime's own permission
        # check and misleading every agent's own tool list.
        # P67 (operator feedback): "a short fact, not an essay" produced
        # exactly that - one-sentence contributions. Reworded toward a
        # short-newspaper-item length/shape instead of "short fact".
        # P69 (operator feedback): "bitte nur zwei Zeilen pro Beitrag [...]
        # die sich wie eine Headline lesen" - a real newspaper item has a
        # distinct headline above its body, plus (operator): "Fuer komplexe
        # Themen sollte es auch angemessen viel Spielraum fuer Text geben.
        # Gelegentlich Kolumnen waeren schoen" - kind='column' is an
        # occasional, opt-in, longer-form piece with real room, never part
        # of King's mandatory per-resident assignment rotation.
        # P73: 'meetings' added (rotation-assigned, real JourFixe/StandUp +
        # decided role-proposal data, see runtime.py's
        # gazette_meetings_source_hint()). P74 (operator feedback: "womit
        # gewonnen hat [...] den benannten Gewinner"): declare_winner is
        # King's separate arbiter act for the daily game.
        "doc": "operation=open(King only, starts today's AI Village Gazette edition and draws the "
              "day's game+pair)|assign(King only, delegates one contribution kind to each resident)|"
              "contribute(kind,headline,content; game_result also needs task+solution)|"
              f"review({_GAZETTE_REVIEWER} only, agent,kind,decision=approve|reject,note?)"
              "|close(King only, compiles all approved contributions into the archived edition)"
              "|declare_winner(King only, winner=<agent or 'unentschieden'>,note?)"
              f"|view(edition_id?) -> the daily village paper; kind is one of {'/'.join(_GAZETTE_KINDS)} "
              "(column occasional/never assigned); "
              f"headline max {_GAZETTE_HEADLINE_MAX_CHARS} chars, two lines read like a real newspaper "
              f"headline, not the kind name repeated; content max {_GAZETTE_MAX_CHARS} chars "
              f"(column/meetings: {_GAZETTE_MAX_COLUMN_CHARS}) - a short newspaper item (a concrete lede "
              f"fact plus a few sentences of real detail), not a one-liner and not an essay. game_result: "
              "task=the posed question, solution=your own answer, both required, never the generic rules. "
              f"Only {_GAZETTE_REVIEWER}-approved contributions ever appear in the compiled edition.",
        "properties": {
            "operation": {"enum": ["open", "assign", "contribute", "review", "close", "declare_winner", "view"]},
            "kind": {"enum": list(_GAZETTE_KINDS)},
            "headline": _s(_GAZETTE_HEADLINE_MAX_CHARS),
            "content": _s(_GAZETTE_MAX_COLUMN_CHARS), "edition_id": _s(20), "agent": _s(40),
            "decision": {"enum": ["approve", "reject"]},
            "winner": _s(40),
            "task": _s(_GAZETTE_GAME_TASK_MAX_CHARS), "solution": _s(_GAZETTE_GAME_SOLUTION_MAX_CHARS),
            "note": _s(400)},
        "required": ["operation"]},
    # P75 (operator directive): every resident proactively plans their own
    # day and coordinates shared slots (standups, jour fixes) instead of a
    # scheduler silently doing it for them; Mon-Fri is the structured work
    # week, Sat/Sun is the agent's own free choice (see kind enum's
    # weekend_* values). A moved/cancelled slot is renegotiated with
    # whoever is affected via reschedule/cancel + respond, not overwritten
    # silently.
    "calendar_operation": {
        "doc": "operation=create(title,kind,scheduled_date,start_time,duration_minutes,attendees?,recurrence?,"
              "notes?,meeting_id?)|reschedule(event_id,new_date?,new_time?,reason?)|cancel(event_id,reason?,"
              f"whole_series?)|respond(event_id,response,proposed_date?,proposed_time?)|list(date_from?,date_to?); "
              f"kind: {'/'.join(_CALENDAR_KINDS)}; date=YYYY-MM-DD, time=HH:MM; recurrence=none/daily_weekday/"
              "weekly; reschedule/cancel hit one occurrence unless whole_series=true; response=accepted/declined/"
              "proposed_alternative.",
        "properties": {
            "operation": {"enum": ["create", "reschedule", "cancel", "respond", "list"]},
            "event_id": _s(60), "title": _s(200), "kind": {"enum": list(_CALENDAR_KINDS)},
            "meeting_id": _s(80),
            "scheduled_date": _s(10), "start_time": _s(5),
            "duration_minutes": {"type": "integer", "minimum": _CALENDAR_MIN_MINUTES, "maximum": _CALENDAR_MAX_MINUTES},
            "attendees": {"type": "array", "items": _s(40), "maxItems": _CALENDAR_MAX_ATTENDEES},
            "recurrence": {"enum": list(_CALENDAR_RECURRENCES)}, "notes": _s(1000),
            "new_date": _s(10), "new_time": _s(5), "reason": _s(300), "whole_series": {"type": "boolean"},
            "response": {"enum": ["accepted", "declined", "proposed_alternative"]},
            "proposed_date": _s(10), "proposed_time": _s(5),
            "date_from": _s(10), "date_to": _s(10)},
        "required": ["operation"]},
    # P76 (operator directive): self-improvement via real fine-tuning on
    # the local M10 GPUs, resource-aware (4 GPUs claimed/released here so
    # two residents never collide) - training itself runs via start_job,
    # this only tracks it. A swap into production is never applied by this
    # action: request_swap only creates a request, review_swap (King only)
    # only endorses it - the actual host-level model change stays a
    # separate, human-operated step (AGENTS.md: no GPU/model change
    # without explicit operator authorization).
    "finetune_operation": {
        "doc": "operation=propose(base_model,method,dataset_description,preferred_gpu_index?,notes?) - claims "
              f"one of {_FINETUNE_GPU_COUNT} local M10 GPUs|update_status(run_id,status,job_reference?,"
              "output_path?,notes?) - releases the GPU when done|evaluate(run_id,metric_name,metric_value,"
              "baseline_value?,notes?)|request_swap(run_id) - needs status=completed + >=1 evaluation|"
              "review_swap(King only,request_id,decision=approve|reject,note?) - endorsement only, a human "
              "still applies it|release_gpu(gpu_index)|list. Run the actual training via start_job.",
        "properties": {
            "operation": {"enum": ["propose", "update_status", "evaluate", "request_swap", "review_swap",
                                   "release_gpu", "list"]},
            "run_id": _s(60), "base_model": _s(200), "method": _s(80), "dataset_description": _s(1000),
            "preferred_gpu_index": {"type": "integer", "minimum": 0, "maximum": _FINETUNE_GPU_COUNT - 1},
            "gpu_index": {"type": "integer", "minimum": 0, "maximum": _FINETUNE_GPU_COUNT - 1},
            "status": {"enum": list(_FINETUNE_RUN_STATUSES)}, "job_reference": _s(120), "output_path": _s(300),
            "metric_name": _s(80), "metric_value": {"type": "number"}, "baseline_value": {"type": "number"},
            "request_id": _s(60), "decision": {"enum": ["approve", "reject"]},
            "notes": _s(500), "note": _s(400)},
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

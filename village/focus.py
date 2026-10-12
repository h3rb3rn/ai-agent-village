"""P136: focus cycles between full village overviews.

Measured on 2026-10-10/11 (docs/analysis/STAGNATION-DIAGNOSTIC-2026-10-10.md): every
resident cycle re-read the whole village, so the prompt grew to 16k-24k tokens and
prompt evaluation took 134-214 s per inference. Most cycles only need the resident's
own task, notes, direct messages and a few retrieved memories.

A focus cycle keeps the system prompt byte-identical (so the prefix cache still
works) and shrinks only the snapshot. It is skipped whenever something urgent is
waiting: a direct message, a peer discussion, an open meeting, a consultation, a
recovery or loop-breaker note, or a calendar/gazette/school duty. Every
VILLAGE_FOCUS_FULL_EVERY cycles (default 4) a full overview is shown again. The
King always gets full cycles because he coordinates the gazette and the school.
"""
from typing import Any, Dict

FOCUS_FULL_EVERY_DEFAULT = 4
FOCUS_KEEP_PROJECTS = 4
FOCUS_KEEP_MEMORY_HITS = 4

# Any of these present means the resident needs the full view in this cycle: messages addressed
# to it, meetings, organic (human) messages, errors and the calendar/gazette duties. discussion_target
# is deliberately absent: the runtime picks it from any recent board chatter, so it is almost always
# set, and addressed messages already appear in untrusted_direct_messages. Advisory nudges (school
# lesson, profile, fine-tune note, memory status, task templates) do not block focus either.
URGENT_FIELDS = (
    'untrusted_direct_messages', 'direct_ack_target', 'active_meetings',
    'recent_organic_messages_untrusted', 'loop_breaker_note', 'recovery_guidance',
    'calendar_daily_note', 'gazette_daily_note', 'task_blocker_guidance', 'active_background_job',
)

# Village-wide overview, shown only in full cycles.
OVERVIEW_FIELDS = (
    'village_topic_saturation', 'recent_role_decisions_untrusted', 'recent_organic_messages_untrusted',
    'artifacts', 'your_model_gaps', 'calendar_week_gaps',
)

FOCUS_HINT = ('Focus cycle: continue your own active task and your notes.next_step. The village overview is '
              'omitted this cycle and returns on the next full cycle. Do not start a new project in a focus '
              'cycle; finish the next step, record the result with notes_operation, or yield with a reason.')


def focus_enabled(env: Dict[str, str], agent_id: str) -> bool:
    return env.get('VILLAGE_FOCUS_CYCLES', '1') != '0' and agent_id != '01-king'


def full_every(env: Dict[str, str]) -> int:
    try:
        return max(1, int(env.get('VILLAGE_FOCUS_FULL_EVERY', FOCUS_FULL_EVERY_DEFAULT)))
    except ValueError:
        return FOCUS_FULL_EVERY_DEFAULT


def focus_allowed(ctx: Dict[str, Any], notes: Dict[str, Any]) -> bool:
    """True only when nothing urgent waits and there is a clear next step to continue."""
    if any(ctx.get(field) for field in URGENT_FIELDS):
        return False
    # The collaboration checkpoint stays in focus cycles: a consult keeps its peer_id.
    if not ctx.get('own_active_task'):
        return False
    return bool(notes.get('next_step') or notes.get('focus'))


def choose_mode(ctx: Dict[str, Any], notes: Dict[str, Any], state: Dict[str, Any],
                env: Dict[str, str], agent_id: str) -> str:
    """Return 'focus' or 'full' and advance the streak kept in the resident state."""
    if not focus_enabled(env, agent_id):
        state['focus_streak'] = 0
        return 'full'
    # A fresh start (no streak yet) always shows the full overview first.
    streak = int(state['focus_streak']) if 'focus_streak' in state else full_every(env)
    if streak >= full_every(env) - 1 or not focus_allowed(ctx, notes):
        state['focus_streak'] = 0
        return 'full'
    state['focus_streak'] = streak + 1
    return 'focus'


def focus_context(ctx: Dict[str, Any], agent_id: str) -> Dict[str, Any]:
    """Drop the village-wide overview; keep the own task, notes, messages and a few hits."""
    out = {k: v for k, v in ctx.items() if k not in OVERVIEW_FIELDS}
    out['projects'] = [p for p in (ctx.get('projects') or []) if p.get('owner') == agent_id][:FOCUS_KEEP_PROJECTS]
    out['untrusted_peer_messages'] = (ctx.get('untrusted_peer_messages') or [])[-1:]
    if ctx.get('retrieved_memory_untrusted'):
        out['retrieved_memory_untrusted'] = ctx['retrieved_memory_untrusted'][:FOCUS_KEEP_MEMORY_HITS]
    out['cycle_mode'] = 'focus'
    out['focus_hint'] = FOCUS_HINT
    return out

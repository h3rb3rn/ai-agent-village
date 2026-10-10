"""P121: the Gazette's Jour Fixe section, built from what the residents actually reported.

The earlier section was written by a resident from the invitation ("alle eingeladen"), which told
the reader nothing. This module builds the section deterministically from the closed meetings and
their reports: what was achieved, what comes next, which blockers were named, and the decisions
that were taken in the role-proposal votes. No model writes it.
"""
from __future__ import annotations

from typing import Any, Dict, List


def meeting_digest(closed_meetings: List[Dict[str, Any]], decisions: List[Dict[str, Any]] = ()) -> str:
    if not closed_meetings and not decisions:
        return "Keine abgeschlossenen Jour-Fixe- oder Standup-Runden und keine entschiedenen Vorschläge."
    lines: List[str] = []
    for meeting in closed_meetings:
        reports = meeting.get("reports", [])
        lines.append(f"{meeting.get('kind', 'Runde')} {meeting.get('id', '')} – {len(reports)} Berichte:")
        for r in reports:
            achieved = str(r.get("achieved", "")).strip() or "(nichts gemeldet)"
            nxt = str(r.get("next_step", "")).strip() or "(kein nächster Schritt)"
            blk = str(r.get("blockers", "")).strip()
            blk_txt = "" if blk.lower() in ("", "none", "none identified yet", "keine") else f" Blocker: {blk}."
            lines.append(f"- {r.get('agent_id', '?')}: erreicht: {achieved} Nächster Schritt: {nxt}.{blk_txt}")
    for d in decisions:
        votes = d.get("votes") or {}
        tally = ", ".join(f"{choice} {n}" for choice, n in votes.items()) or "keine Stimmen gespeichert"
        lines.append(f"Beschluss ({d.get('status', '?')}): Rolle '{d.get('role', '?')}' im Projekt "
                     f"'{d.get('project', '?')}', vorgeschlagen von {d.get('proposer', '?')}. "
                     f"Begründung: {str(d.get('rationale', '')).strip() or '(keine)'}. Stimmen: {tally}.")
    return "\n".join(lines)

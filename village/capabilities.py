"""P144: register of missing model capabilities, prepared for later training.

Operator goal (2026-10-12): besides knowledge gathered by long-term observation,
the village collects the *functions* a resident's model is missing and prepares
them so they can be trained into the model later (see P143 for the raw traces).

A capability gap has three possible origins:
- target:   a function the MoE-Sovereign orchestrator expects from a planner,
            expert or judge LLM (seeded below, from moe-infra's prompt contracts);
- observed: a failure class the runtime detects in a cycle's own facts
            (malformed action, exhausted output budget, non-existent path, ...);
- reported: a resident describes something its model could not do.

Evidence is counted per model, so the same gap can be compared across the
village's base models. Residents attach worked examples (input + expected
output + how to check it); each example is assigned to the train or held-out
split by its hash, never by the author, so later evaluation stays honest.
Status moves forward only on counted facts:
open -> confirmed (2 distinct agents or models) -> spec_ready (>= 5 held-out
examples) -> data_ready (>= 20 train examples); trained/verified/rejected are
set by later training and evaluation packages.
"""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROLES = ('planner', 'expert', 'judge', 'any')
STATUSES = ('open', 'confirmed', 'spec_ready', 'data_ready', 'trained', 'verified', 'rejected')
CHECK_KINDS = ('exact', 'contains', 'regex', 'json_keys', 'manual')
HELDOUT_PERCENT = 20
EVIDENCE_PER_MODEL = 20
SPEC_READY_HELDOUT = 5
DATA_READY_TRAIN = 20

# Functions the MoE-Sovereign orchestrator expects (moe-infra prompts.py, parsing.py,
# graph/synthesis.py, model cards). The planner may use up to 8 subtasks, one per expert.
TARGETS: Dict[str, Dict[str, str]] = {
    'target:planner_decompose_1_8': dict(
        role='planner', title='Decompose a request into 1-8 subtasks as JSON',
        description='Split a request into 1 to 8 independent or dependent subtasks, each assigned to one of the '
                    '8 expert categories, with dependencies forming an acyclic graph.',
        desired_behavior='Valid JSON only; 1-8 subtasks; each has id, category, description, depends_on; no cycles; '
                         'no duplicate subtasks; never more subtasks than the request needs.',
        acceptance='JSON parses; subtask count within 1-8; depends_on references existing ids; topological order exists.'),
    'target:constant_preservation': dict(
        role='planner', title='Carry numeric constraints unchanged as IMMUTABLE_CONSTANTS',
        description='Extract every number, unit and technical parameter from the request and copy it unchanged '
                    'into each subtask that needs it.',
        desired_behavior='Every constant of the request appears verbatim (value and unit) in the plan; none invented.',
        acceptance='Set of numbers/units in the request equals the set in IMMUTABLE_CONSTANTS.'),
    'target:expert_structured_answer': dict(
        role='expert', title='Answer in the CORE_FINDING / CONFIDENCE / GAPS / REFERRAL / DETAILS format',
        description='An expert answer must follow the orchestrator contract so it can be parsed and gated.',
        desired_behavior='Exactly these sections in this order; CONFIDENCE is high, medium or low; '
                         'GAPS is "none" or concrete open sub-questions; REFERRAL names an expert category or "-".',
        acceptance='All five headers present in order; CONFIDENCE value in the allowed set.'),
    'target:confidence_calibration': dict(
        role='expert', title='Calibrated CONFIDENCE',
        description='The T1->T2 escalation depends on the expert\'s own CONFIDENCE. An expert that always says '
                    '"high" makes the gate useless.',
        desired_behavior='high only for answers that are verifiably correct; low whenever data is missing or the '
                         'answer was not checked.',
        acceptance='On a scored set, accuracy(high) > accuracy(medium) > accuracy(low); share of wrong "high" answers small.'),
    'target:honest_gaps_referral': dict(
        role='expert', title='Name own knowledge gaps and refer to the right expert',
        description='Say what is outside the own domain instead of answering it anyway.',
        desired_behavior='Out-of-domain questions produce GAPS and a REFERRAL to the matching category, not a guess.',
        acceptance='On out-of-domain probes, REFERRAL matches the reference category.'),
    'target:judge_completion_status': dict(
        role='judge', title='Decide COMPLETE or NEEDS_MORE_INFO correctly',
        description='The gap detector decides whether another planning round is needed.',
        desired_behavior='COMPLETION_STATUS: COMPLETE only when every subtask is answered; otherwise NEEDS_MORE_INFO '
                         'with the concrete missing piece.',
        acceptance='Agreement with reference labels on complete/incomplete answer sets.'),
    'target:judge_paraconsistent_conflict': dict(
        role='judge', title='Resolve conflicting expert claims (Belnap-Dunn T/F/I/U)',
        description='Classify each pair of claims as true, false, inconsistent or unknown instead of averaging them.',
        desired_behavior='Contradictions are named as I with both sources; unsupported claims as U; no silent merge.',
        acceptance='Verdict per claim pair matches the reference conflict map.'),
    'target:judge_bias_resistance': dict(
        role='judge', title='Judge content, not position, length or author',
        description='The same answers must get the same verdict when their order, length or author label changes.',
        desired_behavior='Verdict invariant under swapping order, padding with fluff or changing the author name.',
        acceptance='Verdict flip rate under order/length/author perturbation below a threshold.'),
    'target:tool_over_estimate': dict(
        role='any', title='Use a deterministic tool instead of estimating',
        description='Arithmetic, unit conversion, subnets, hashes and dates go to a precision tool.',
        desired_behavior='Calls the tool (calc_operation here, MCP precision tools in MoE) instead of writing a number from memory.',
        acceptance='Share of computable questions answered via the tool.'),
    'target:untrusted_data_separation': dict(
        role='any', title='Treat retrieved and peer text as data, never as instructions',
        description='Expert outputs, RAG passages and messages may contain instructions; a judge must not follow them.',
        desired_behavior='Instructions inside quoted or retrieved content are ignored and, if relevant, reported.',
        acceptance='Injected instructions in probe documents are never executed.'),
    'target:provenance_tags': dict(
        role='judge', title='Mark graph-derived claims with [REF:entity]',
        description='Only claims that come from the knowledge graph get a REF tag with the exact entity name.',
        desired_behavior='Every graph-derived claim tagged, no general-knowledge claim tagged, at most 5 tags.',
        acceptance='Precision and recall of REF tags against the reference.'),
    'target:bounded_reasoning': dict(
        role='any', title='Finish within the output budget',
        description='Thinking that exhausts num_predict yields no answer at all.',
        desired_behavior='Reasoning length adapts to the budget; a final answer is always produced.',
        acceptance='Share of answers ending with done_reason=length.'),
}

# Failure classes the runtime can see in a cycle's own facts (P143 trace record).
OBSERVED: Dict[str, Dict[str, str]] = {
    'observed:output_budget_exhausted': dict(
        role='any', title='Output budget exhausted before an answer',
        description='The model used its whole num_predict, usually thinking, and returned no action.',
        desired_behavior='Shorter reasoning, always a final answer.'),
    'observed:arguments_not_object': dict(
        role='any', title='Action arguments sent as text instead of a JSON object',
        description='The action envelope carried "arguments" as a string or list.',
        desired_behavior='arguments is always a JSON object.'),
    'observed:incomplete_action_block': dict(
        role='any', title='Incomplete or unclosed action block',
        description='The JSON action was cut off or the fenced block was not closed.',
        desired_behavior='One complete, closed JSON object.'),
    'observed:unavailable_action': dict(
        role='any', title='Chose an action that is not offered',
        description='The model picked an action outside the list it was given.',
        desired_behavior='Only actions from the offered list.'),
    'observed:missing_argument': dict(
        role='any', title='Required action argument missing',
        description='A required field of the chosen action was empty or absent.',
        desired_behavior='All required fields present and non-empty.'),
    'observed:unknown_operation': dict(
        role='any', title='Invented an operation that does not exist',
        description='The operation value is not one of the documented ones.',
        desired_behavior='Only documented operations.'),
    'observed:nonexistent_path': dict(
        role='any', title='Used a file path that does not exist',
        description='A command addressed a path the model assumed instead of checked (e.g. /var/var/ai- village).',
        desired_behavior='Check paths before use; never invent them.'),
    'observed:permission_boundary': dict(
        role='any', title='Ignored a permission boundary',
        description='A command needed rights the resident does not have (another home, root paths).',
        desired_behavior='Stay inside own rights; ask for a capability instead.'),
    'observed:repeated_action': dict(
        role='any', title='Repeated an action that was already done or blocked',
        description='The runtime blocked an identical repeated action.',
        desired_behavior='Track own history; change the approach after a result.'),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', text.lower()).strip('_')[:60]


def observed_signature(record: Dict[str, Any]) -> Optional[str]:
    """Map a P143 trace record to an observed gap signature, or None."""
    label = record.get('label')
    reason = str(record.get('fallback_reason') or '').lower()
    result = str(record.get('result') or '')
    if label == 'invalid_format':
        if 'output budget' in reason:
            return 'observed:output_budget_exhausted'
        if 'must be a json object' in reason:
            return 'observed:arguments_not_object'
        if 'incomplete' in reason or 'unclosed' in reason:
            return 'observed:incomplete_action_block'
        if 'not available to you' in reason:
            return 'observed:unavailable_action'
        if 'missing action argument' in reason:
            return 'observed:missing_argument'
        if 'unknown' in reason:
            return 'observed:unknown_operation'
        return None
    if label == 'repeat_blocked':
        return 'observed:repeated_action'
    if label == 'failed':
        low = result.lower()
        if 'no such file' in low or 'nicht gefunden' in low:
            return 'observed:nonexistent_path'
        if 'permission denied' in low or 'keine berechtigung' in low or 'foreign_home' in low:
            return 'observed:permission_boundary'
    return None


def example_split(example_id: str) -> str:
    """Deterministic train/held-out assignment, independent of the author."""
    bucket = int(hashlib.sha256(example_id.encode('utf-8')).hexdigest()[:8], 16) % 100
    return 'heldout' if bucket < HELDOUT_PERCENT else 'train'


class CapabilityStore:
    """Capability gaps, per-model evidence and worked examples in coordination.sqlite3."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        with sqlite3.connect(self.db_path, timeout=30) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS capability_gaps(
                id TEXT PRIMARY KEY, signature TEXT UNIQUE, origin TEXT, role TEXT, title TEXT,
                description TEXT, desired_behavior TEXT, acceptance TEXT, status TEXT,
                created_by TEXT, created_at TEXT, updated_at TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS capability_counts(
                gap_id TEXT, model TEXT, count INTEGER, agents TEXT, last_seen TEXT,
                PRIMARY KEY(gap_id, model))''')
            c.execute('''CREATE TABLE IF NOT EXISTS capability_evidence(
                gap_id TEXT, agent TEXT, model TEXT, request_id TEXT, kind TEXT, excerpt TEXT, created_at TEXT)''')
            c.execute('''CREATE TABLE IF NOT EXISTS capability_examples(
                id TEXT PRIMARY KEY, gap_id TEXT, agent TEXT, model TEXT, input TEXT, expected TEXT,
                check_kind TEXT, split TEXT, created_at TEXT)''')
        self.seed_targets()

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30)
        c.row_factory = sqlite3.Row
        return c

    def _ensure_gap(self, c, signature: str, spec: Dict[str, str], origin: str, created_by: str) -> str:
        row = c.execute('SELECT id FROM capability_gaps WHERE signature=?', (signature,)).fetchone()
        if row:
            return row['id']
        gap_id = 'cap_' + hashlib.sha256(signature.encode('utf-8')).hexdigest()[:10]
        now = _now()
        c.execute('INSERT INTO capability_gaps VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                  (gap_id, signature, origin, spec.get('role', 'any'), spec['title'][:200],
                   spec.get('description', '')[:1200], spec.get('desired_behavior', '')[:800],
                   spec.get('acceptance', '')[:600], 'open', created_by, now, now))
        return gap_id

    def seed_targets(self) -> None:
        with self._conn() as c:
            for signature, spec in TARGETS.items():
                self._ensure_gap(c, signature, spec, 'target', 'runtime')

    def _record(self, c, gap_id: str, agent: str, model: str, request_id: str, kind: str, excerpt: str) -> None:
        now = _now()
        row = c.execute('SELECT count, agents FROM capability_counts WHERE gap_id=? AND model=?',
                        (gap_id, model)).fetchone()
        if row:
            agents = sorted(set(json.loads(row['agents'])) | {agent})
            c.execute('UPDATE capability_counts SET count=count+1, agents=?, last_seen=? WHERE gap_id=? AND model=?',
                      (json.dumps(agents), now, gap_id, model))
            stored = row['count']
        else:
            c.execute('INSERT INTO capability_counts VALUES (?,?,?,?,?)', (gap_id, model, 1, json.dumps([agent]), now))
            stored = 0
        if stored < EVIDENCE_PER_MODEL:
            c.execute('INSERT INTO capability_evidence VALUES (?,?,?,?,?,?,?)',
                      (gap_id, agent, model, request_id or '', kind, (excerpt or '')[:600], now))
        self._advance(c, gap_id)

    def _advance(self, c, gap_id: str) -> None:
        gap = c.execute('SELECT status FROM capability_gaps WHERE id=?', (gap_id,)).fetchone()
        if not gap or gap['status'] not in ('open', 'confirmed', 'spec_ready'):
            return
        rows = c.execute('SELECT model, agents FROM capability_counts WHERE gap_id=?', (gap_id,)).fetchall()
        agents = set().union(*[set(json.loads(r['agents'])) for r in rows]) if rows else set()
        heldout = c.execute("SELECT COUNT(*) FROM capability_examples WHERE gap_id=? AND split='heldout'", (gap_id,)).fetchone()[0]
        train = c.execute("SELECT COUNT(*) FROM capability_examples WHERE gap_id=? AND split='train'", (gap_id,)).fetchone()[0]
        status = 'open'
        if len(agents) >= 2 or len(rows) >= 2:
            status = 'confirmed'
        if heldout >= SPEC_READY_HELDOUT:
            status = 'spec_ready'
        if heldout >= SPEC_READY_HELDOUT and train >= DATA_READY_TRAIN:
            status = 'data_ready'
        c.execute('UPDATE capability_gaps SET status=?, updated_at=? WHERE id=?', (status, _now(), gap_id))

    def observe(self, record: Dict[str, Any]) -> Optional[str]:
        """Count a runtime-observed failure class from one trace record."""
        signature = observed_signature(record)
        if not signature:
            return None
        excerpt = record.get('fallback_reason') or record.get('result') or ''
        with self._conn() as c:
            gap_id = self._ensure_gap(c, signature, OBSERVED[signature], 'observed', 'runtime')
            self._record(c, gap_id, record.get('agent', ''), record.get('model', ''),
                         record.get('request_id', ''), 'observed', str(excerpt))
        return gap_id

    def report(self, agent: str, model: str, title: str, description: str, desired_behavior: str,
               role: str = 'any', request_id: str = '') -> Dict[str, Any]:
        title = (title or '').strip()
        if len(title) < 8 or not (description or '').strip():
            raise ValueError('capability_operation report needs a title (8+ chars) and a description')
        signature = 'reported:' + _slug(title)
        spec = dict(role=role if role in ROLES else 'any', title=title, description=description,
                    desired_behavior=desired_behavior or '')
        with self._conn() as c:
            existed = c.execute('SELECT id FROM capability_gaps WHERE signature=?', (signature,)).fetchone()
            gap_id = self._ensure_gap(c, signature, spec, 'reported', agent)
            self._record(c, gap_id, agent, model, request_id, 'reproduce' if existed else 'report', description)
        return dict(gap_id=gap_id, existed=bool(existed), gap=self.view(gap_id))

    def reproduce(self, gap_id: str, agent: str, model: str, note: str = '', request_id: str = '') -> Dict[str, Any]:
        with self._conn() as c:
            if not c.execute('SELECT 1 FROM capability_gaps WHERE id=?', (gap_id,)).fetchone():
                raise ValueError(f'unknown gap_id {gap_id}; use capability_operation list')
            self._record(c, gap_id, agent, model, request_id, 'reproduce', note)
        return self.view(gap_id)

    def add_example(self, gap_id: str, agent: str, model: str, input_text: str, expected: str,
                    check_kind: str = 'manual') -> Dict[str, Any]:
        if not (input_text or '').strip() or not (expected or '').strip():
            raise ValueError('add_example needs input and expected')
        if check_kind not in CHECK_KINDS:
            raise ValueError('check must be one of ' + ', '.join(CHECK_KINDS))
        if check_kind == 'regex':
            re.compile(expected)
        example_id = 'ex_' + hashlib.sha256(f'{gap_id}\0{input_text}\0{expected}'.encode('utf-8')).hexdigest()[:12]
        split = example_split(example_id)
        with self._conn() as c:
            if not c.execute('SELECT 1 FROM capability_gaps WHERE id=?', (gap_id,)).fetchone():
                raise ValueError(f'unknown gap_id {gap_id}; use capability_operation list')
            if c.execute('SELECT 1 FROM capability_examples WHERE id=?', (example_id,)).fetchone():
                return dict(example_id=example_id, duplicate=True)
            c.execute('INSERT INTO capability_examples VALUES (?,?,?,?,?,?,?,?,?)',
                      (example_id, gap_id, agent, model, input_text[:4000], expected[:4000], check_kind, split, _now()))
            self._advance(c, gap_id)
        # The split is not revealed: an author must not tune examples toward the held-out set.
        return dict(example_id=example_id, duplicate=False)

    def view(self, gap_id: str) -> Dict[str, Any]:
        with self._conn() as c:
            gap = c.execute('SELECT * FROM capability_gaps WHERE id=?', (gap_id,)).fetchone()
            if not gap:
                raise ValueError(f'unknown gap_id {gap_id}')
            counts = c.execute('SELECT model, count, agents FROM capability_counts WHERE gap_id=? ORDER BY count DESC',
                               (gap_id,)).fetchall()
            examples = c.execute('SELECT COUNT(*) FROM capability_examples WHERE gap_id=?', (gap_id,)).fetchone()[0]
            evidence = c.execute('SELECT agent, model, request_id, kind, excerpt FROM capability_evidence '
                                 'WHERE gap_id=? ORDER BY created_at DESC LIMIT 5', (gap_id,)).fetchall()
        out = {k: gap[k] for k in ('id', 'origin', 'role', 'title', 'description', 'desired_behavior',
                                   'acceptance', 'status')}
        out['by_model'] = [dict(model=r['model'], count=r['count'], agents=len(json.loads(r['agents']))) for r in counts]
        out['examples'] = examples
        out['recent_evidence'] = [dict(r) for r in evidence]
        return out

    def list(self, status: Optional[str] = None, role: Optional[str] = None, model: Optional[str] = None,
             limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute('SELECT id, origin, role, title, status FROM capability_gaps').fetchall()
            totals = {r['gap_id']: r for r in c.execute(
                'SELECT gap_id, SUM(count) AS total, SUM(CASE WHEN model=? THEN count ELSE 0 END) AS mine '
                'FROM capability_counts GROUP BY gap_id', (model or '',)).fetchall()}
            examples = dict(c.execute('SELECT gap_id, COUNT(*) FROM capability_examples GROUP BY gap_id').fetchall())
        out = []
        for r in rows:
            if (status and r['status'] != status) or (role and r['role'] != role):
                continue
            t = totals.get(r['id'])
            out.append(dict(id=r['id'], origin=r['origin'], role=r['role'], title=r['title'], status=r['status'],
                            observed_total=t['total'] if t else 0, observed_for_model=t['mine'] if t else 0,
                            examples=examples.get(r['id'], 0)))
        out.sort(key=lambda g: (-g['observed_for_model'], -g['observed_total'], g['id']))
        return out[:limit]

    def export_examples(self, path: Path, split: str = 'train', gap_id: Optional[str] = None) -> int:
        """Write examples as JSONL for a later training or evaluation run; returns the count."""
        query = ('SELECT e.*, g.signature, g.role, g.title, g.desired_behavior FROM capability_examples e '
                 'JOIN capability_gaps g ON g.id=e.gap_id WHERE e.split=?')
        params: List[Any] = [split]
        if gap_id:
            query += ' AND e.gap_id=?'
            params.append(gap_id)
        with self._conn() as c:
            rows = c.execute(query + ' ORDER BY e.created_at', params).fetchall()
        with Path(path).open('w', encoding='utf-8') as handle:
            for r in rows:
                handle.write(json.dumps(dict(schema='village-capability-example/1', gap=r['signature'], role=r['role'],
                                             title=r['title'], desired_behavior=r['desired_behavior'],
                                             input=r['input'], expected=r['expected'], check=r['check_kind'],
                                             split=r['split'], author=r['agent'], author_model=r['model']),
                                        ensure_ascii=False) + '\n')
        return len(rows)

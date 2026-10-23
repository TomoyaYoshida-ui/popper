"""Study-scoped session memory for planning — never evaluation authority.

Uses the existing three-layer Memory store under ``<run_dir>/memory``.
Entries are short, structured, and count-bounded. They inform planning and
reflection only. They never replace independent observations, judgments, or
scores, and cannot authorize a scientific three-state on their own.
"""
from __future__ import annotations

from pathlib import Path

from ..core import ProtocolError, digest
from ..memory import Memory

MEMORY_ENTRY_LIMIT = 16
MEMORY_TEXT_CHARS = 240
MEMORY_REF_LIMIT = 8
ENTRY_KINDS = frozenset({
    'hypothesis_revision',
    'judgment_outcome',
    'rejection_reason',
    'action_note',
})
# Fields that must never ride along a memory entry (evaluation stays elsewhere).
_FORBIDDEN_ENTRY_KEYS = frozenset({
    'score', 'metric', 'mean_effect', 'value', 'ci95', 'p_value', 'p-value',
    'baseline_mae', 'candidate_mae', 'baseline_mse', 'candidate_mse',
    'old_judgment', 'new_judgment', 'observation_id', 'quality',
})


def memory_directory(run_dir):
    return Path(run_dir).resolve() / 'memory'


def study_memory(run_dir):
    return Memory(memory_directory(run_dir))


def _clip(text):
    if not isinstance(text, str):
        text = '' if text is None else str(text)
    return ' '.join(text.split())[:MEMORY_TEXT_CHARS]


def _refs(refs):
    if not refs:
        return []
    out = []
    for item in refs:
        if isinstance(item, str) and item.strip():
            out.append(item.strip()[:120])
        if len(out) >= MEMORY_REF_LIMIT:
            break
    return out


def project_memory_context(run_dir):
    """Bounded projection for ``ResearchRuntime.context()``.

    Trust label makes clear these are unverified session notes, not measurements.
    """
    mem = study_memory(run_dir)
    summary = mem.summary() or {}
    raw = summary.get('entries') if isinstance(summary, dict) else None
    entries = []
    if isinstance(raw, list):
        for item in raw[-MEMORY_ENTRY_LIMIT:]:
            projected = _project_entry(item)
            if projected:
                entries.append(projected)
    working = ''
    if mem.working_path.is_file():
        working = _clip(mem.working_path.read_text(encoding='utf-8'))
    return {
        'schema': 'research-memory-context/v1',
        'trust': 'unverified_session_notes',
        'entries': entries,
        'working': working or None,
    }


def _project_entry(item):
    if not isinstance(item, dict):
        return None
    kind = item.get('kind')
    text = item.get('text')
    entry_id = item.get('id')
    if kind not in ENTRY_KINDS or not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(entry_id, str) or not entry_id.startswith('MEM-'):
        return None
    return {
        'id': entry_id,
        'kind': kind,
        'text': _clip(text),
        'refs': _refs(item.get('refs')),
    }


def record_memory_entry(run_dir, kind, text, *, refs=None, study_id=None):
    """Append one bounded structured note. Rejects score-like payloads."""
    if kind not in ENTRY_KINDS:
        raise ProtocolError(f'unsupported memory entry kind: {kind}')
    if isinstance(text, dict):
        bad = set(text) & _FORBIDDEN_ENTRY_KEYS
        if bad:
            raise ProtocolError('memory entry cannot carry evaluation fields')
        text = text.get('summary') or text.get('text') or ''
    clipped = _clip(text)
    if not clipped:
        return None
    ref_list = _refs(refs)
    entry_id = 'MEM-' + digest({
        'study_id': study_id or '',
        'kind': kind,
        'text': clipped,
        'refs': ref_list,
    })[:16]
    mem = study_memory(run_dir)
    summary = mem.summary() if isinstance(mem.summary(), dict) else {}
    if not summary:
        summary = {'schema': 'research-memory/v1', 'entries': []}
    entries = list(summary.get('entries') or [])
    # Idempotent: identical content does not duplicate.
    if any(isinstance(e, dict) and e.get('id') == entry_id for e in entries):
        return entry_id
    entries.append({
        'id': entry_id,
        'kind': kind,
        'text': clipped,
        'refs': ref_list,
    })
    summary = {
        'schema': 'research-memory/v1',
        'entries': entries[-MEMORY_ENTRY_LIMIT:],
    }
    mem.set_summary(summary)
    return entry_id


def record_action_memory(run_dir, *, study_id, action, assessment=None,
                         observation_id=None, judgments=None, rejection=None):
    """Write at most a few notes after a durable planning step lands."""
    written = []
    kind = action.get('kind') if isinstance(action, dict) else None
    if rejection:
        eid = record_memory_entry(
            run_dir, 'rejection_reason',
            rejection if isinstance(rejection, str) else 'assessment rejected',
            refs=_refs(action.get('evidence_refs') if isinstance(action, dict) else None),
            study_id=study_id)
        if eid:
            written.append(eid)
    if kind == 'revise_hypothesis':
        hid = action.get('hypothesis_id')
        mech = _clip(action.get('mechanism') or '')
        eid = record_memory_entry(
            run_dir, 'hypothesis_revision',
            f'{hid}: {mech}' if hid else mech,
            refs=_refs(action.get('evidence_refs')),
            study_id=study_id)
        if eid:
            written.append(eid)
    elif kind == 'execute_candidate' and observation_id:
        hid = action.get('candidate_id')
        state = None
        if judgments:
            for item in judgments:
                if (isinstance(item, dict)
                        and item.get('hypothesis_id') == hid
                        and item.get('new_judgment') in {
                            'supported', 'refuted', 'inconclusive'}):
                    state = item['new_judgment']
                    break
        note = f'{hid}: observed {observation_id}'
        if state:
            note = f'{hid}: {state} ({observation_id})'
        eid = record_memory_entry(
            run_dir, 'judgment_outcome', note,
            refs=_refs([observation_id, hid] if hid else [observation_id]),
            study_id=study_id)
        if eid:
            written.append(eid)
    elif kind == 'stop_planning':
        eid = record_memory_entry(
            run_dir, 'action_note',
            _clip(action.get('rationale') or 'stop_planning'),
            refs=_refs(action.get('evidence_refs')),
            study_id=study_id)
        if eid:
            written.append(eid)
    if assessment and isinstance(assessment, dict):
        # Agent assessment is unverified; store only as a note, never as judgment.
        subject = assessment.get('subject_hypothesis_id')
        claimed = assessment.get('judgment')
        if subject and claimed in {'supported', 'refuted', 'inconclusive', 'not_evaluated'}:
            eid = record_memory_entry(
                run_dir, 'action_note',
                f'unverified assessment {subject}: {claimed}',
                refs=_refs(assessment.get('evidence_refs')),
                study_id=study_id)
            if eid:
                written.append(eid)
    return written

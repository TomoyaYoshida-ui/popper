"""Read-only resume fingerprint for the same run_dir — not scientific authority.

Combines store-derived planning progress with an optional human checkpoint.
Does not carry metric numbers or elevate MEM-* to evidence.
"""
from __future__ import annotations

from .memory_context import (
    MEMORY_TEXT_CHARS, project_memory_context, read_resume_checkpoint,
)


RESUME_PROJECTION_CHARS = 512


def _clip_resume(text, limit=RESUME_PROJECTION_CHARS):
    if not isinstance(text, str):
        return None
    clipped = ' '.join(text.split())[:limit]
    return clipped or None


def probe_last_action(runtime, *, max_probe=32):
    """Return (step, action_kind) for the latest action_completed record."""
    last_step = None
    last_kind = None
    for step in range(max_probe):
        record = runtime._record('action_completed', step)
        if not record:
            continue
        last_step = step
        body = record.get('body') if isinstance(record, dict) else None
        action = body.get('action') if isinstance(body, dict) else None
        kind = action.get('kind') if isinstance(action, dict) else None
        last_kind = kind if isinstance(kind, str) else None
    return last_step, last_kind


def project_resume_context(run_dir, *, phase=None, integrity_ok=None,
                           last_runtime_step=None, last_action_kind=None,
                           remaining_experiments=None):
    """Bounded resume projection for planning / brief status."""
    memory = project_memory_context(run_dir)
    checkpoint = read_resume_checkpoint(run_dir)
    recent = [
        {'id': item['id'], 'kind': item['kind']}
        for item in (memory.get('entries') or [])[-3:]
        if isinstance(item, dict)
    ]
    payload = {
        'schema': 'research-resume/v1',
        'trust': {
            'store_derived': ['phase', 'integrity_ok', 'last_runtime_step',
                              'last_action_kind', 'remaining_experiments'],
            'unverified_session_notes': ['checkpoint', 'recent_memory_kinds'],
        },
        'phase': phase,
        'integrity_ok': integrity_ok,
        'last_runtime_step': last_runtime_step,
        'last_action_kind': last_action_kind,
        'remaining_experiments': remaining_experiments,
        'checkpoint': None if checkpoint is None else {
            'id': checkpoint['id'],
            'text': _clip_resume(checkpoint.get('text'), MEMORY_TEXT_CHARS),
            'refs': list(checkpoint.get('refs') or []),
        },
        'recent_memory_kinds': recent,
    }
    # Soft size guard: drop recent kinds first if somehow oversized.
    encoded = str(payload)
    if len(encoded) > RESUME_PROJECTION_CHARS * 4:
        payload['recent_memory_kinds'] = recent[-1:]
    return payload

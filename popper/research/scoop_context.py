"""Project completed Scoop-Check hits into bounded planning context.

Citable hits are the subset: step7 completed ∩ step4 selected ∩
step5 access=fulltext. Metadata still comes from step2; matching key is
pipeline ``paper_id``. Skipped, failed, or provisional scoop is not
novelty completion. Hits carry a stable SCOOP-* id, source, DOI and/or
arXiv when stored, a title, and a truncated excerpt. Full text is not
loaded into planning context. This is not an audit-grade FPR product
and does not fetch papers.
"""
from __future__ import annotations

from pathlib import Path

from ..core import ProtocolError, digest, read_json
from ..scoop import _paper_id
from .literature_context import CLAIM_FIELDS, EXCERPT_CHARS, project_paper

SCOOP_LIMIT = 12
SCOOP_KINDS = frozenset({'novelty_conclusion', 'scoop_conclusion', 'prior_art_check'})
SCOOP_CLAIM_FIELDS = (
    'scoop_ids', 'scoop', 'prior_art', 'prior_art_ids', 'novelty_ids',
)
_PROMPT_MARKERS = (
    '查新', '无撞车', '撞车', 'scoop', 'prior art', 'prior-art',
    'novelty check', 'novelty search', '查新结论', '查新完成',
)


class ScoopClaim:
    """A planning action that claims a scoop / novelty basis. It does not execute."""

    def __init__(self, kind, rationale, scoop_ids, literature_ids, evidence_refs):
        self.kind = kind
        self.rationale = rationale
        self.scoop_ids = list(scoop_ids)
        self.literature_ids = list(literature_ids)
        self.evidence_refs = list(evidence_refs)

    def model_dump(self, **_kwargs):
        return {
            'kind': self.kind, 'rationale': self.rationale,
            'scoop_ids': list(self.scoop_ids),
            'literature_ids': list(self.literature_ids),
            'evidence_refs': list(self.evidence_refs),
        }


def scoop_error(message, path, rule):
    error = ProtocolError(message)
    error.details = [{'path': path, 'rule': rule}]
    return error


def prompt_requires_scoop(text):
    """True when a caller prompt asks for a scoop / novelty-grounded answer."""
    if not isinstance(text, str) or not text.strip():
        return False
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in _PROMPT_MARKERS)


def scoop_request_blocked(scoop, literature=None, *, require_scoop=False, planning_prompt=None):
    """Refuse generation when scoop/novelty was requested and nothing citable exists.

    Provisional-only scoop does not count: those hits never enter ``scoop``.
    Literature hits may satisfy a novelty prompt when present.
    """
    if scoop or literature:
        return False
    return bool(require_scoop) or prompt_requires_scoop(planning_prompt)


def project_scoop_hit(paper, *, verified_fulltext=False):
    """One bounded scoop hit, or None when the record has no usable title."""
    base = project_paper(paper)
    if base is None:
        return None
    identifier = base.get('identifier') or {}
    hit_id = 'SCOOP-' + digest({
        'title': base['title'], 'source': base['source'],
        'identifier': identifier or None,
    })[:16]
    record = {
        'id': hit_id, 'source': base['source'], 'title': base['title'],
        'excerpt': base['excerpt'][:EXCERPT_CHARS],
    }
    if identifier:
        record['identifier'] = identifier
    if verified_fulltext:
        record['verified_fulltext'] = True
    return record


def _verified_fulltext_paper_ids(scoop_dir):
    """paper_id set: step4 selected ∩ step5 access=fulltext. Empty if unreadable."""
    try:
        step4 = read_json(scoop_dir / 'step4.json')
        step5 = read_json(scoop_dir / 'step5.json')
    except (OSError, ValueError):
        return set()
    if not isinstance(step4, dict) or not isinstance(step5, dict):
        return set()
    selected = step4.get('candidate_paper_ids')
    deep = step5.get('papers')
    if not isinstance(selected, list) or not isinstance(deep, list):
        return set()
    selected_ids = {pid for pid in selected if isinstance(pid, str) and pid.strip()}
    fulltext_ids = {
        item.get('paper_id') for item in deep
        if isinstance(item, dict) and item.get('access') == 'fulltext'
        and isinstance(item.get('paper_id'), str) and item.get('paper_id').strip()
    }
    return selected_ids & fulltext_ids


def _scoop_dirs(roots):
    directories = []
    seen = set()
    for root in roots:
        if root is None:
            continue
        root = Path(root).resolve()
        candidates = [root / 'scoop']
        runs = root / 'integrations' / 'runs'
        if runs.is_dir():
            try:
                children = sorted(child for child in runs.iterdir() if child.is_dir())
            except OSError:
                children = []
            for child in children:
                candidates.append(child / 'scoop')
        for directory in candidates:
            if not directory.is_dir():
                continue
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_relative_to(root):
                continue
            seen.add(resolved)
            directories.append(resolved)
    return directories


def _completed_report(scoop_dir):
    report_path = scoop_dir / 'step7.json'
    if not report_path.is_file():
        return None
    try:
        report = read_json(report_path)
    except (OSError, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    # Provisional (or any non-completed) reports are not citable novelty evidence.
    if report.get('status') != 'completed':
        return None
    return report


def load_scoop_hits(*roots, limit=SCOOP_LIMIT):
    """Read selected fulltext papers from completed scoop runs only.

    Never contacts a search service. Unselected or abstract-only papers are
    not citable even when step7 is completed.
    """
    if type(limit) is not int or limit < 1:
        raise ProtocolError('Scoop context limit is invalid')
    hits = []
    seen = set()
    for scoop_dir in _scoop_dirs(roots):
        if _completed_report(scoop_dir) is None:
            continue
        allowed = _verified_fulltext_paper_ids(scoop_dir)
        if not allowed:
            continue
        search_path = scoop_dir / 'step2.json'
        if not search_path.is_file():
            continue
        try:
            data = read_json(search_path)
        except (OSError, ValueError):
            continue
        papers = data.get('papers') if isinstance(data, dict) else None
        if not isinstance(papers, list):
            continue
        for paper in papers:
            if not isinstance(paper, dict):
                continue
            # ScoopRun assigns paper_id in-memory for step4/5 but step2 JSON
            # usually stores raw search hits without that field — recompute.
            paper_id = paper.get('paper_id')
            if not isinstance(paper_id, str) or not paper_id.strip():
                paper_id = _paper_id(paper)
            if paper_id not in allowed:
                continue
            hit = project_scoop_hit(paper, verified_fulltext=True)
            if hit is None or hit['id'] in seen:
                continue
            seen.add(hit['id'])
            hits.append(hit)
            if len(hits) >= limit:
                return hits
    return hits


def _one_id(token, *, field='action.scoop_ids'):
    if (not isinstance(token, str) or not token.strip() or any(ch.isspace() for ch in token)
            or len(token) > 80):
        raise scoop_error(
            '查新字段必须是已落盘 scoop/文献命中的 id', field, 'scoop_id_required')
    return token.strip()


def _ids_from(value, *, field='action.scoop_ids'):
    if isinstance(value, str):
        return [_one_id(value, field=field)]
    if isinstance(value, dict):
        token = value.get('id') if isinstance(value.get('id'), str) else value.get('scoop_id')
        if not isinstance(token, str):
            token = value.get('literature_id')
        return [_one_id(token, field=field)]
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_ids_from(item, field=field))
        return found
    raise scoop_error(
        '查新字段必须是已落盘 scoop/文献命中的 id', field, 'scoop_id_required')


def claimed_scoop_ids(raw):
    """None when the action does not claim scoop/novelty. A list when it does.

    Collects scoop_* fields and, for scoop kinds, also literature citation fields
    so a novelty conclusion may cite SCOOP-* or LIT-* ids already in context.
    """
    if not isinstance(raw, dict):
        return None
    present_scoop = [key for key in SCOOP_CLAIM_FIELDS if key in raw]
    kind = raw.get('kind')
    if kind not in SCOOP_KINDS and not present_scoop:
        return None
    found = []
    for key in present_scoop:
        found.extend(_ids_from(raw.get(key), field=f'action.{key}'))
    if kind in SCOOP_KINDS:
        for key in CLAIM_FIELDS:
            if key in raw:
                found.extend(_ids_from(raw.get(key), field=f'action.{key}'))
    return found


def scoop_claim_from(raw, citation_ids):
    kind = raw.get('kind') if isinstance(raw, dict) else None
    rationale = raw.get('rationale') if isinstance(raw, dict) else None
    if kind not in SCOOP_KINDS or not isinstance(rationale, str) or not rationale.strip():
        raise scoop_error('查新动作缺少合法说明', 'action.rationale', 'required')
    refs = raw.get('evidence_refs', [])
    if refs is None:
        refs = []
    if not isinstance(refs, list) or not all(isinstance(item, str) and item.strip() for item in refs):
        raise scoop_error('查新动作的证据引用不合法', 'action.evidence_refs', 'list')
    scoop_ids = [token for token in citation_ids if token.startswith('SCOOP-')]
    literature_ids = [token for token in citation_ids if token.startswith('LIT-')]
    # Preserve any other cited tokens under literature_ids so unknown prefixes still validate.
    other = [token for token in citation_ids
             if not token.startswith('SCOOP-') and not token.startswith('LIT-')]
    literature_ids.extend(other)
    return ScoopClaim(kind, rationale.strip(), scoop_ids, literature_ids, refs)


def citable_novelty_ids(context):
    """Ids allowed for scoop/novelty claims: completed scoop + retrieved literature."""
    known = set()
    for key in ('scoop', 'literature'):
        for item in context.get(key) or []:
            if not isinstance(item, dict):
                continue
            token = item.get('id')
            if not isinstance(token, str) or not token.strip():
                continue
            if item.get('provisional'):
                continue
            known.add(token)
    return known

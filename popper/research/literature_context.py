"""Project already retrieved papers into bounded planning context.

Hits come from search artifacts already stored for this study or campaign.
Each record carries a stable id, a source, a DOI and/or arXiv identifier when
one was stored, a title, and a truncated excerpt. Full text is not loaded.
This is not a reading agent and does not fetch papers.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core import ProtocolError, digest, read_json

LITERATURE_LIMIT = 12
EXCERPT_CHARS = 480
LITERATURE_KINDS = frozenset({'literature_review'})
CLAIM_FIELDS = (
    'literature_ids', 'literature', 'citations', 'citation',
    'neighbor_work', 'neighbors',
)
_PROMPT_MARKERS = (
    '近邻工作', '文献综述', '根据文献', '依据文献',
    'neighbor work', 'neighbouring work', 'neighboring work',
    'literature review',
)
_DOI = re.compile(r'^10\.\d{4,9}/\S+$', re.IGNORECASE)
_ARXIV = re.compile(
    r'^(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?$',
    re.IGNORECASE)
_ARXIV_URL = re.compile(
    r'arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:v\d+)?', re.IGNORECASE)


class LiteratureClaim:
    """A planning action that claims a literature basis. It does not execute."""

    def __init__(self, kind, rationale, literature_ids, evidence_refs):
        self.kind = kind
        self.rationale = rationale
        self.literature_ids = list(literature_ids)
        self.evidence_refs = list(evidence_refs)

    def model_dump(self, **_kwargs):
        return {
            'kind': self.kind, 'rationale': self.rationale,
            'literature_ids': list(self.literature_ids),
            'evidence_refs': list(self.evidence_refs),
        }


def literature_error(message, path, rule):
    error = ProtocolError(message)
    error.details = [{'path': path, 'rule': rule}]
    return error


def prompt_requires_literature(text):
    """True when a caller prompt asks for a literature-grounded answer."""
    if not isinstance(text, str) or not text.strip():
        return False
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in _PROMPT_MARKERS)


def literature_request_blocked(literature, *, require_literature=False, planning_prompt=None):
    """Refuse generation when a literature answer was requested and nothing was retrieved."""
    if literature:
        return False
    return bool(require_literature) or prompt_requires_literature(planning_prompt)


def _doi(value):
    if not isinstance(value, str):
        return None
    text = value.strip()
    text = re.sub(r'^https?://(?:dx\.)?doi\.org/', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^doi:\s*', '', text, flags=re.IGNORECASE)
    return text if _DOI.fullmatch(text) else None


def _arxiv(paper):
    raw = paper.get('arxiv_id')
    if not isinstance(raw, str) or not raw.strip():
        raw = paper.get('arxiv') if isinstance(paper.get('arxiv'), str) else ''
    if not raw.strip():
        url = paper.get('url') if isinstance(paper.get('url'), str) else ''
        match = _ARXIV_URL.search(url)
        raw = match.group(1) if match else ''
    text = raw.strip()
    text = re.sub(r'^arxiv:\s*', '', text, flags=re.IGNORECASE)
    text = re.sub(r'^https?://arxiv\.org/(?:abs|pdf)/', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\.pdf$', '', text, flags=re.IGNORECASE)
    return text if _ARXIV.fullmatch(text) else None


def _source(paper):
    source = paper.get('source')
    if isinstance(source, str) and source.strip():
        return ' '.join(source.split())[:80]
    return 'paper-search'


def _excerpt(paper):
    raw = paper.get('abstract') or paper.get('excerpt') or paper.get('snippet') or ''
    if not isinstance(raw, str):
        return ''
    return ' '.join(raw.split())[:EXCERPT_CHARS]


def project_paper(paper):
    """One bounded hit, or None when the record has no usable title."""
    if not isinstance(paper, dict):
        return None
    title = paper.get('title')
    if not isinstance(title, str) or not title.strip():
        return None
    title = ' '.join(title.split())[:300]
    identifier = {}
    doi = _doi(paper.get('doi'))
    arxiv = _arxiv(paper)
    if doi:
        identifier['doi'] = doi
    if arxiv:
        identifier['arxiv'] = arxiv
    source = _source(paper)
    hit_id = 'LIT-' + digest({
        'title': title, 'source': source, 'identifier': identifier or None,
    })[:16]
    record = {
        'id': hit_id, 'source': source, 'title': title, 'excerpt': _excerpt(paper),
    }
    if identifier:
        record['identifier'] = identifier
    return record


def _search_files(roots):
    files = []
    seen = set()
    for root in roots:
        if root is None:
            continue
        root = Path(root).resolve()
        directories = [root / 'literature', root / 'scoop' / 'literature']
        runs = root / 'integrations' / 'runs'
        if runs.is_dir():
            try:
                children = sorted(child for child in runs.iterdir() if child.is_dir())
            except OSError:
                children = []
            for child in children:
                directories.append(child / 'literature')
                directories.append(child / 'scoop' / 'literature')
        for directory in directories:
            if not directory.is_dir():
                continue
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_relative_to(root):
                continue
            seen.add(resolved)
            try:
                matches = sorted(directory.glob('search-*.json'))
            except OSError:
                continue
            for path in matches:
                try:
                    if path.is_file() and path.resolve().is_relative_to(resolved):
                        files.append(path)
                except OSError:
                    continue
    return files


def load_literature_hits(*roots, limit=LITERATURE_LIMIT):
    """Read stored search JSON only. Never contacts a literature service."""
    if type(limit) is not int or limit < 1:
        raise ProtocolError('Literature context limit is invalid')
    hits = []
    seen = set()
    for path in _search_files(roots):
        try:
            data = read_json(path)
        except (OSError, ValueError):
            continue
        papers = data.get('papers') if isinstance(data, dict) else None
        if not isinstance(papers, list):
            continue
        for paper in papers:
            hit = project_paper(paper)
            if hit is None or hit['id'] in seen:
                continue
            seen.add(hit['id'])
            hits.append(hit)
            if len(hits) >= limit:
                return hits
    return hits


def _one_id(token):
    if (not isinstance(token, str) or not token.strip() or any(ch.isspace() for ch in token)
            or len(token) > 80):
        raise literature_error(
            '文献字段必须是已检索文献的 id', 'action.literature_ids', 'literature_id_required')
    return token.strip()


def _ids_from(value):
    if isinstance(value, str):
        return [_one_id(value)]
    if isinstance(value, dict):
        token = value.get('id') if isinstance(value.get('id'), str) else value.get('literature_id')
        return [_one_id(token)]
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_ids_from(item))
        return found
    raise literature_error(
        '文献字段必须是已检索文献的 id', 'action.literature_ids', 'literature_id_required')


def claimed_literature_ids(raw):
    """None when the action does not claim literature. A list, possibly empty, when it does."""
    if not isinstance(raw, dict):
        return None
    present = [key for key in CLAIM_FIELDS if key in raw]
    kind = raw.get('kind')
    if kind not in LITERATURE_KINDS and not present:
        return None
    found = []
    for key in present:
        found.extend(_ids_from(raw.get(key)))
    return found


def literature_claim_from(raw, literature_ids):
    kind = raw.get('kind') if isinstance(raw, dict) else None
    rationale = raw.get('rationale') if isinstance(raw, dict) else None
    if kind not in LITERATURE_KINDS or not isinstance(rationale, str) or not rationale.strip():
        raise literature_error('文献动作缺少合法说明', 'action.rationale', 'required')
    refs = raw.get('evidence_refs', [])
    if refs is None:
        refs = []
    if not isinstance(refs, list) or not all(isinstance(item, str) and item.strip() for item in refs):
        raise literature_error('文献动作的证据引用不合法', 'action.evidence_refs', 'list')
    return LiteratureClaim(kind, rationale.strip(), literature_ids, refs)


def observation_ids(run_dirs):
    """Observation ids already stored for the given study directories."""
    from .store import ResearchStore
    found = []
    seen = set()
    for run_dir in run_dirs:
        path = Path(run_dir) / 'research.sqlite'
        if not path.is_file():
            continue
        store = None
        try:
            store = ResearchStore(path, readonly=True)
            for observation in store.list('observation'):
                token = observation.get('observation_id') if isinstance(observation, dict) else None
                if isinstance(token, str) and token.strip() and token not in seen:
                    seen.add(token)
                    found.append(token)
        except (OSError, ProtocolError, ValueError):
            continue
        finally:
            if store is not None:
                store.close()
    return found


def workspace_evidence_ids(run_dirs, search_roots):
    """Ids a chat reply may cite: observations, literature, scoop, and fulltext excerpts."""
    from .fulltext_context import load_fulltext_excerpts
    from .scoop_context import load_scoop_hits

    roots = (*search_roots, *run_dirs)
    literature = load_literature_hits(*roots)
    scoop = load_scoop_hits(*roots)
    fulltext = load_fulltext_excerpts(*roots)
    citable = (
        [hit['id'] for hit in literature]
        + [hit['id'] for hit in scoop]
        + [hit['id'] for hit in fulltext]
    )
    return observation_ids(run_dirs), citable

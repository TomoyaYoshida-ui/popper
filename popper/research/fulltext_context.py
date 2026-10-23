"""Project already-downloaded public fulltext into bounded planning context.

Only text accepted by ``popper.fulltext`` / scoop deep-dive and left on disk
under a study or campaign root is eligible. Each record carries a stable
``FT-*`` id, optional ``LIT-*`` / ``SCOOP-*`` links, a DOI/arXiv identifier
when stored, optional page or character offset, and a truncated excerpt.

This is not a reading agent: it does not fetch the open web, does not load
entire PDFs into prompts, and does not authorize paper writing.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core import ProtocolError, digest, read_json
from .literature_context import EXCERPT_CHARS, project_paper
from .scoop_context import project_scoop_hit

FULLTEXT_LIMIT = 8
FULLTEXT_EXCERPT_CHARS = EXCERPT_CHARS
FULLTEXT_KINDS = frozenset({'fulltext_citation', 'passage_claim'})
CLAIM_FIELDS = (
    'fulltext_ids', 'fulltext', 'passages', 'passage_ids',
    'paper_body', 'page_quotes',
)
_PROMPT_MARKERS = (
    '论文正文', '全文说', '全文中', '全文里', '某页说', '某页提到',
    '第页说', '页码说', '根据全文', '依据全文', '引用全文',
    'full text says', 'full-text says', 'paper body', 'on page',
    'according to the full text', 'from the pdf',
)
_PAGE_IN_PROMPT = re.compile(
    r'第\s*\d+\s*页|on\s+page\s+\d+|page\s+\d+\s+(?:says|states|mentions)',
    re.IGNORECASE,
)
_PAPER_ID = re.compile(r'^[A-Za-z0-9_-]{1,100}$')


class FulltextClaim:
    """A planning action that claims a downloaded fulltext basis. It does not execute."""

    def __init__(self, kind, rationale, fulltext_ids, literature_ids, evidence_refs):
        self.kind = kind
        self.rationale = rationale
        self.fulltext_ids = list(fulltext_ids)
        self.literature_ids = list(literature_ids)
        self.evidence_refs = list(evidence_refs)

    def model_dump(self, **_kwargs):
        return {
            'kind': self.kind, 'rationale': self.rationale,
            'fulltext_ids': list(self.fulltext_ids),
            'literature_ids': list(self.literature_ids),
            'evidence_refs': list(self.evidence_refs),
        }


def fulltext_error(message, path, rule):
    error = ProtocolError(message)
    error.details = [{'path': path, 'rule': rule}]
    return error


def prompt_requires_fulltext(text):
    """True when a caller prompt asks for a fulltext- or page-grounded answer."""
    if not isinstance(text, str) or not text.strip():
        return False
    if _PAGE_IN_PROMPT.search(text):
        return True
    folded = text.casefold()
    return any(marker.casefold() in folded for marker in _PROMPT_MARKERS)


def fulltext_request_blocked(fulltext, *, require_fulltext=False, planning_prompt=None):
    """Refuse generation when a fulltext/page answer was requested and nothing was downloaded."""
    if fulltext:
        return False
    return bool(require_fulltext) or prompt_requires_fulltext(planning_prompt)


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
            directories.append((root, resolved))
    return directories


def _papers_by_id(scoop_dir):
    path = scoop_dir / 'step2.json'
    if not path.is_file():
        return {}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        return {}
    papers = data.get('papers') if isinstance(data, dict) else None
    if not isinstance(papers, list):
        return {}
    by_id = {}
    for paper in papers:
        if not isinstance(paper, dict):
            continue
        pid = paper.get('paper_id')
        if isinstance(pid, str) and _PAPER_ID.fullmatch(pid):
            by_id[pid] = paper
    return by_id


def _artifact_text_path(root, scoop_dir, record):
    artifacts = record.get('artifacts') if isinstance(record, dict) else None
    if not isinstance(artifacts, dict):
        return None
    raw = artifacts.get('text')
    if not isinstance(raw, str) or not raw.strip():
        paper_id = record.get('paper_id')
        if isinstance(paper_id, str) and _PAPER_ID.fullmatch(paper_id):
            # Prefer the canonical scoop papers layout when artifacts omit paths.
            for candidate in (
                scoop_dir / 'papers' / f'{paper_id}.txt',
            ):
                try:
                    resolved = candidate.resolve()
                except OSError:
                    continue
                if resolved.is_file() and resolved.is_relative_to(root):
                    return resolved
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = scoop_dir / path
    try:
        resolved = path.resolve()
    except OSError:
        return None
    if not resolved.is_file() or not resolved.is_relative_to(root):
        return None
    if resolved.suffix.lower() != '.txt':
        return None
    return resolved


def _identifier(paper):
    hit = project_paper(paper) if isinstance(paper, dict) else None
    if hit is None:
        return {}
    return dict(hit.get('identifier') or {})


def _linked_ids(paper):
    literature_id = scoop_id = None
    if not isinstance(paper, dict):
        return literature_id, scoop_id
    lit = project_paper(paper)
    if lit is not None:
        literature_id = lit['id']
    scoop = project_scoop_hit(paper)
    if scoop is not None:
        scoop_id = scoop['id']
    return literature_id, scoop_id


def _page_and_offset(text, excerpt, preferred_offset=None):
    """Optional 1-based page from form-feed breaks, plus character offset of the excerpt."""
    offset = preferred_offset
    if offset is None:
        if excerpt and excerpt in text:
            offset = text.index(excerpt)
        else:
            offset = 0
    page = None
    if '\f' in text:
        before = text[:offset]
        page = before.count('\f') + 1
    return page, offset


def _excerpt_from(text, closest_passage=None):
    cleaned = text.replace('\r\n', '\n').replace('\r', '\n')
    preferred_offset = None
    if isinstance(closest_passage, str):
        passage = ' '.join(closest_passage.split())
        if 20 <= len(passage) <= 300:
            normalized = ' '.join(cleaned.split())
            if passage in normalized:
                # Map back roughly: use the raw window starting at first word match.
                first = passage.split()[0]
                idx = cleaned.find(first)
                if idx >= 0:
                    preferred_offset = idx
                    window = ' '.join(cleaned[idx:].split())[:FULLTEXT_EXCERPT_CHARS]
                    return window, preferred_offset
            return passage[:FULLTEXT_EXCERPT_CHARS], preferred_offset
    body = ' '.join(cleaned.split())[:FULLTEXT_EXCERPT_CHARS]
    return body, 0


def project_fulltext_excerpt(paper, *, text, paper_id, text_sha256=None,
                             source_url=None, closest_passage=None):
    """One bounded excerpt, or None when text is too short or title-less."""
    if not isinstance(text, str):
        return None
    excerpt, preferred_offset = _excerpt_from(text, closest_passage)
    if len(excerpt.strip()) < 40:
        return None
    title = None
    if isinstance(paper, dict):
        raw_title = paper.get('title')
        if isinstance(raw_title, str) and raw_title.strip():
            title = ' '.join(raw_title.split())[:300]
    if not title:
        title = f'fulltext:{paper_id}'
    identifier = _identifier(paper) if isinstance(paper, dict) else {}
    literature_id, scoop_id = _linked_ids(paper) if isinstance(paper, dict) else (None, None)
    page, offset = _page_and_offset(text, excerpt, preferred_offset)
    record_id = 'FT-' + digest({
        'paper_id': paper_id,
        'text_sha256': text_sha256 or None,
        'offset': offset,
        'excerpt': excerpt[:80],
    })[:16]
    record = {
        'id': record_id,
        'paper_id': paper_id,
        'title': title,
        'excerpt': excerpt,
        'char_offset': offset,
    }
    if identifier:
        record['identifier'] = identifier
    if literature_id:
        record['literature_id'] = literature_id
    if scoop_id:
        record['scoop_id'] = scoop_id
    if page is not None:
        record['page'] = page
    if isinstance(source_url, str) and source_url.startswith('https://'):
        record['source_url'] = source_url[:300]
    if isinstance(text_sha256, str) and text_sha256.strip():
        record['text_sha256'] = text_sha256.strip()[:64]
    return record


def load_fulltext_excerpts(*roots, limit=FULLTEXT_LIMIT):
    """Read on-disk scoop fulltext artifacts only. Never contacts the network."""
    if type(limit) is not int or limit < 1:
        raise ProtocolError('Fulltext context limit is invalid')
    hits = []
    seen = set()
    for root, scoop_dir in _scoop_dirs(roots):
        step5 = scoop_dir / 'step5.json'
        if not step5.is_file():
            continue
        try:
            data = read_json(step5)
        except (OSError, ValueError):
            continue
        records = data.get('papers') if isinstance(data, dict) else None
        if not isinstance(records, list):
            continue
        by_id = _papers_by_id(scoop_dir)
        for record in records:
            if not isinstance(record, dict) or record.get('access') != 'fulltext':
                continue
            paper_id = record.get('paper_id')
            if not isinstance(paper_id, str) or not _PAPER_ID.fullmatch(paper_id):
                continue
            text_path = _artifact_text_path(root, scoop_dir, record)
            if text_path is None:
                continue
            try:
                text = text_path.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            artifacts = record.get('artifacts') if isinstance(record.get('artifacts'), dict) else {}
            paper = dict(by_id.get(paper_id) or {})
            paper.setdefault('paper_id', paper_id)
            hit = project_fulltext_excerpt(
                paper, text=text, paper_id=paper_id,
                text_sha256=artifacts.get('text_sha256') or record.get('text_sha256'),
                source_url=artifacts.get('source_url'),
                closest_passage=record.get('closest_passage'),
            )
            if hit is None or hit['id'] in seen:
                continue
            seen.add(hit['id'])
            hits.append(hit)
            if len(hits) >= limit:
                return hits
    return hits


def _one_id(token, *, field='action.fulltext_ids'):
    if (not isinstance(token, str) or not token.strip() or any(ch.isspace() for ch in token)
            or len(token) > 80):
        raise fulltext_error(
            '全文字段必须是已下载全文摘录的 id', field, 'fulltext_id_required')
    return token.strip()


def _ids_from(value, *, field='action.fulltext_ids'):
    if isinstance(value, str):
        return [_one_id(value, field=field)]
    if isinstance(value, dict):
        token = value.get('id') if isinstance(value.get('id'), str) else value.get('fulltext_id')
        return [_one_id(token, field=field)]
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_ids_from(item, field=field))
        return found
    raise fulltext_error(
        '全文字段必须是已下载全文摘录的 id', field, 'fulltext_id_required')


def claimed_fulltext_ids(raw):
    """None when the action does not claim fulltext. A list when it does."""
    if not isinstance(raw, dict):
        return None
    present = [key for key in CLAIM_FIELDS if key in raw]
    kind = raw.get('kind')
    if kind not in FULLTEXT_KINDS and not present:
        return None
    found = []
    for key in present:
        found.extend(_ids_from(raw.get(key), field=f'action.{key}'))
    return found


def fulltext_claim_from(raw, fulltext_ids):
    kind = raw.get('kind') if isinstance(raw, dict) else None
    rationale = raw.get('rationale') if isinstance(raw, dict) else None
    if kind not in FULLTEXT_KINDS or not isinstance(rationale, str) or not rationale.strip():
        raise fulltext_error('全文动作缺少合法说明', 'action.rationale', 'required')
    refs = raw.get('evidence_refs', [])
    if refs is None:
        refs = []
    if not isinstance(refs, list) or not all(isinstance(item, str) and item.strip() for item in refs):
        raise fulltext_error('全文动作的证据引用不合法', 'action.evidence_refs', 'list')
    literature_ids = []
    for key in ('literature_ids', 'literature', 'scoop_ids', 'scoop'):
        if key in raw and isinstance(raw, dict):
            value = raw.get(key)
            if isinstance(value, str):
                literature_ids.append(value.strip())
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str) and item.strip():
                        literature_ids.append(item.strip())
                    elif isinstance(item, dict):
                        token = item.get('id')
                        if isinstance(token, str) and token.strip():
                            literature_ids.append(token.strip())
    return FulltextClaim(kind, rationale.strip(), fulltext_ids, literature_ids, refs)


def citable_fulltext_ids(context):
    """Ids allowed for fulltext / page claims."""
    known = set()
    for item in context.get('fulltext') or []:
        if not isinstance(item, dict):
            continue
        token = item.get('id')
        if isinstance(token, str) and token.strip():
            known.add(token)
    return known

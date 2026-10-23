"""Bounded fulltext excerpts and citation checks. No network access."""
import json

import pytest

from popper.chat import FULLTEXT_CLAIM_REFUSAL, gate_numeric_claims
from popper.core import ProtocolError
from popper.research.fulltext_context import (
    load_fulltext_excerpts, project_fulltext_excerpt, prompt_requires_fulltext,
    fulltext_request_blocked,
)
from popper.research.runtime import ResearchRuntime


def _paper(**extra):
    base = {
        'paper_id': 'P-fixture01',
        'source': 'arxiv', 'arxiv_id': '2401.00042',
        'title': 'Fixture fulltext paper',
        'doi': '10.1000/fixture.fulltext',
        'abstract': 'Abstract only should not become the excerpt.',
    }
    base.update(extra)
    return base


def _body(pages=1):
    chunk = (
        'This is the actual methodological passage used for bounded fulltext '
        'projection tests. It must be long enough to pass the excerpt floor. '
    )
    parts = [chunk * 3 for _ in range(pages)]
    return '\f'.join(parts)


def _write_fulltext(root, *, access='fulltext', text=None, paper=None,
                    closest_passage=None, revision=None):
    scoop = root / 'scoop'
    papers_dir = scoop / 'papers'
    if revision:
        papers_dir = papers_dir / revision
    papers_dir.mkdir(parents=True)
    paper = paper or _paper()
    pid = paper['paper_id']
    body = text if text is not None else _body()
    text_path = papers_dir / f'{pid}.txt'
    text_path.write_text(body, encoding='utf-8')
    (scoop / 'step2.json').write_text(json.dumps({'papers': [paper]}), encoding='utf-8')
    artifacts = {
        'text': str(text_path),
        'text_sha256': 'a' * 64,
        'source_url': 'https://arxiv.org/pdf/2401.00042',
    }
    record = {'paper_id': pid, 'access': access, 'artifacts': artifacts}
    if closest_passage is not None:
        record['closest_passage'] = closest_passage
    (scoop / 'step5.json').write_text(json.dumps({'papers': [record]}), encoding='utf-8')
    return scoop, paper, body


def _host():
    class _Host:
        pass
    host = _Host()
    host._reject = ResearchRuntime._reject.__get__(host)
    host._validate_refs = ResearchRuntime._validate_refs.__get__(host)
    host._validate_action = ResearchRuntime._validate_action.__get__(host)
    host._parse_proposal_action = ResearchRuntime._parse_proposal_action.__get__(host)
    return host


def _facts(*, fulltext=None, literature=None, scoop=None):
    return {
        'candidates': [{'hypothesis_id': 'H-known', 'executed': False}],
        'raw_evidence': [{'evidence_refs': ['OBS-known']}],
        'remaining_experiments': 1,
        'fulltext': fulltext or [],
        'literature': literature or [],
        'scoop': scoop or [],
    }


def test_projected_excerpt_keeps_id_links_identifier_and_truncation():
    paper = _paper()
    text = _body(pages=2)
    hit = project_fulltext_excerpt(
        paper, text=text, paper_id=paper['paper_id'],
        text_sha256='b' * 64, source_url='https://arxiv.org/pdf/2401.00042',
    )
    assert hit['id'].startswith('FT-')
    assert hit['literature_id'].startswith('LIT-')
    assert hit['scoop_id'].startswith('SCOOP-')
    assert hit['identifier'] == {'doi': '10.1000/fixture.fulltext', 'arxiv': '2401.00042'}
    assert hit['title'] == 'Fixture fulltext paper'
    assert len(hit['excerpt']) <= 480
    assert 'methodological passage' in hit['excerpt']
    assert hit['page'] == 1
    assert hit['char_offset'] == 0
    assert project_fulltext_excerpt(
        paper, text=text, paper_id=paper['paper_id'], text_sha256='b' * 64,
    )['id'] == hit['id']


def test_closest_passage_preferred_when_present_in_text():
    paper = _paper()
    text = _body()
    passage = 'This is the actual methodological passage used for bounded'
    hit = project_fulltext_excerpt(
        paper, text=text, paper_id=paper['paper_id'], closest_passage=passage,
    )
    assert passage[:40] in hit['excerpt']


def test_stored_fulltext_artifacts_are_loaded_abstract_only_skipped(tmp_path):
    study = tmp_path / 'study'
    _write_fulltext(study)
    hits = load_fulltext_excerpts(study)
    assert len(hits) == 1
    assert hits[0]['id'].startswith('FT-')
    assert hits[0]['literature_id'].startswith('LIT-')
    assert 'https://arxiv.org' in hits[0]['source_url']

    only_abstract = tmp_path / 'abstract'
    _write_fulltext(only_abstract, access='abstract_only')
    assert load_fulltext_excerpts(only_abstract) == []


def test_missing_txt_on_disk_is_not_projected(tmp_path):
    study = tmp_path / 'study'
    scoop, paper, _ = _write_fulltext(study)
    (scoop / 'papers' / f"{paper['paper_id']}.txt").unlink()
    assert load_fulltext_excerpts(study) == []


def test_campaign_fulltext_is_bounded(tmp_path):
    project = tmp_path / 'project'
    scoop = project / 'integrations' / 'runs' / 'demo' / 'scoop'
    papers_dir = scoop / 'papers'
    papers_dir.mkdir(parents=True)
    records = []
    papers = []
    for index in range(10):
        paper = _paper(
            paper_id=f'P-{index:04d}', title=f'Paper {index}',
            arxiv_id=f'2403.{index:05d}', doi=f'10.1000/ft.{index}',
        )
        text_path = papers_dir / f"{paper['paper_id']}.txt"
        text_path.write_text(_body(), encoding='utf-8')
        papers.append(paper)
        records.append({
            'paper_id': paper['paper_id'], 'access': 'fulltext',
            'artifacts': {'text': str(text_path), 'text_sha256': f'{index:064d}'},
        })
    (scoop / 'step2.json').write_text(json.dumps({'papers': papers}), encoding='utf-8')
    (scoop / 'step5.json').write_text(json.dumps({'papers': records}), encoding='utf-8')
    hits = load_fulltext_excerpts(project)
    assert len(hits) == 8
    assert all(hit['id'].startswith('FT-') for hit in hits)


def test_fulltext_claim_requires_ids_and_rejects_when_empty():
    host = _host()
    paper = _paper()
    known = project_fulltext_excerpt(
        paper, text=_body(), paper_id=paper['paper_id'], text_sha256='c' * 64,
    )
    context = _facts(fulltext=[known])
    bare = {'kind': 'passage_claim', 'rationale': '论文正文第 2 页说了机制。'}
    with pytest.raises(ProtocolError) as missing:
        host._parse_proposal_action(bare, context)
    assert missing.value.details == [{'path': 'action.fulltext_ids', 'rule': 'fulltext_id_required'}]

    with pytest.raises(ProtocolError) as empty:
        host._parse_proposal_action(bare, _facts())
    assert empty.value.details == [{'path': 'fulltext', 'rule': 'no_fulltext_hits'}]
    assert '全文' in str(empty.value) or '页码' in str(empty.value)

    with pytest.raises(ProtocolError) as unknown:
        host._parse_proposal_action({
            'kind': 'passage_claim', 'rationale': '依据全文',
            'fulltext_ids': ['FT-missing'],
        }, context)
    assert unknown.value.details == [{'path': 'action.fulltext_ids', 'rule': 'unknown_fulltext'}]

    accepted = host._parse_proposal_action({
        'kind': 'passage_claim', 'rationale': '依据已下载摘录',
        'fulltext_ids': [known['id']],
        'literature_ids': [known['literature_id']],
    }, context)
    host._validate_action(accepted, context)
    assert accepted.model_dump()['fulltext_ids'] == [known['id']]
    assert known['literature_id'] in accepted.literature_ids


def test_compute_action_may_attach_fulltext_ids_without_becoming_passage_claim():
    host = _host()
    paper = _paper()
    known = project_fulltext_excerpt(
        paper, text=_body(), paper_id=paper['paper_id'], text_sha256='d' * 64,
    )
    action = host._parse_proposal_action({
        'kind': 'execute_candidate', 'candidate_id': 'H-known',
        'rationale': 'Registered candidate', 'evidence_refs': [],
        'fulltext_ids': [known['id']],
    }, _facts(fulltext=[known]))
    host._validate_action(action, _facts(fulltext=[known]))
    assert action.kind == 'execute_candidate'


def test_prompt_requires_fulltext_and_blocks_without_hits():
    assert prompt_requires_fulltext('请根据论文正文回答')
    assert prompt_requires_fulltext('第 3 页说了什么')
    assert prompt_requires_fulltext('What does the full text say on page 2')
    assert not prompt_requires_fulltext('执行已登记候选')
    assert not prompt_requires_fulltext('请给出查新结论')
    assert fulltext_request_blocked([], require_fulltext=True)
    assert fulltext_request_blocked([], planning_prompt='论文正文怎么写的')
    assert not fulltext_request_blocked([{'id': 'FT-1'}], require_fulltext=True)


def test_chat_fulltext_page_claim_requires_ft_id():
    text = '论文正文第 2 页说准确率很高。'
    refused = gate_numeric_claims(text, observation_ids=(), literature_ids=())
    assert FULLTEXT_CLAIM_REFUSAL in refused
    assert '第 2 页' not in refused

    cited = '论文正文说了方法边界（FT-real）。'
    assert gate_numeric_claims(
        cited, observation_ids=(), literature_ids=['FT-real']) == cited
    assert gate_numeric_claims(
        cited, observation_ids=(), literature_ids=(), fulltext_ids=['FT-real']) == cited

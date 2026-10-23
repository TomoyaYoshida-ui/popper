"""Literature context and citation checks. No literature network access."""
import json

import pytest

from popper.core import ProtocolError
from popper.research.literature_context import (
    load_literature_hits, project_paper, prompt_requires_literature,
)
from popper.research.runtime import ResearchRuntime
from popper.research.runtime_protocol import decision_instructions


def _paper(**extra):
    base = {
        'source': 'arxiv', 'arxiv_id': '2401.00001', 'title': 'Fixture neighbor',
        'abstract': 'A short excerpt about the registered method.',
        'content': 'FULLTEXT_MARKER ' * 40,
    }
    base.update(extra)
    return base


def _host():
    class _Host:
        pass
    host = _Host()
    host._reject = ResearchRuntime._reject.__get__(host)
    host._validate_refs = ResearchRuntime._validate_refs.__get__(host)
    host._validate_action = ResearchRuntime._validate_action.__get__(host)
    host._parse_proposal_action = ResearchRuntime._parse_proposal_action.__get__(host)
    return host


def _facts(literature):
    return {
        'candidates': [{'hypothesis_id': 'H-known', 'executed': False}],
        'raw_evidence': [{'evidence_refs': ['OBS-known']}],
        'remaining_experiments': 1,
        'literature': literature,
    }


def test_projected_hit_keeps_id_source_identifier_and_excerpt_only():
    hit = project_paper(_paper())
    assert hit['id'].startswith('LIT-')
    assert hit['source'] == 'arxiv'
    assert hit['identifier'] == {'arxiv': '2401.00001'}
    assert hit['title'] == 'Fixture neighbor'
    assert 'short excerpt' in hit['excerpt']
    assert 'FULLTEXT_MARKER' not in json.dumps(hit)
    assert project_paper(_paper())['id'] == hit['id']
    assert len(project_paper(_paper(abstract='A' * 1000))['excerpt']) == 480


def test_missing_doi_and_arxiv_are_not_invented():
    hit = project_paper({
        'source': 'openalex', 'title': 'Title only', 'url': 'https://example.test/paper',
        'abstract': 'Excerpt without an identifier.',
    })
    assert hit['source'] == 'openalex'
    assert 'identifier' not in hit
    assert hit['excerpt'] == 'Excerpt without an identifier.'


def test_doi_and_arxiv_are_both_kept_when_both_were_stored():
    hit = project_paper(_paper(doi='https://doi.org/10.1000/fixture.1', source='crossref'))
    assert hit['identifier']['doi'] == '10.1000/fixture.1'
    assert hit['identifier']['arxiv'] == '2401.00001'
    assert hit['source'] == 'crossref'


def test_stored_search_files_are_bounded_and_include_campaign_hits(tmp_path):
    study = tmp_path / 'study'
    campaign = tmp_path / 'project' / 'integrations' / 'runs' / 'demo' / 'literature'
    study.joinpath('literature').mkdir(parents=True)
    campaign.mkdir(parents=True)
    papers = [_paper(title=f'Paper {index}', arxiv_id=f'2401.{index:05d}') for index in range(13)]
    study.joinpath('literature', 'search-study.json').write_text(
        json.dumps({'papers': papers[:2]}), encoding='utf-8')
    campaign.joinpath('search-campaign.json').write_text(
        json.dumps({'papers': papers[2:]}), encoding='utf-8')
    hits = load_literature_hits(study, tmp_path / 'project')
    assert len(hits) == 12
    assert hits[0]['title'] == 'Paper 0'
    assert all({'id', 'source', 'title', 'excerpt'} <= set(hit) for hit in hits)
    assert any(hit['identifier']['arxiv'] == '2401.00002' for hit in hits)


def test_unknown_literature_id_is_rejected_and_compute_actions_are_not():
    host = _host()
    known = project_paper(_paper())
    context = _facts([known])
    review = {
        'kind': 'literature_review', 'rationale': 'Neighbors overlap the mechanism.',
        'literature_ids': ['LIT-not-in-context'],
    }
    with pytest.raises(ProtocolError) as missing:
        host._parse_proposal_action(review, context)
    assert missing.value.details == [{'path': 'action.literature_ids', 'rule': 'unknown_literature'}]
    assert 'LIT-not-in-context' not in str(missing.value)

    cited = {**review, 'literature_ids': [known['id']]}
    accepted = host._parse_proposal_action(cited, context)
    host._validate_action(accepted, context)
    assert accepted.model_dump()['literature_ids'] == [known['id']]

    with pytest.raises(ProtocolError) as neighbor:
        host._parse_proposal_action({
            'kind': 'execute_candidate', 'candidate_id': 'H-known',
            'rationale': 'Registered candidate', 'evidence_refs': [],
            'neighbor_work': ['LIT-not-in-context'],
        }, context)
    assert neighbor.value.details == [{'path': 'action.literature_ids', 'rule': 'unknown_literature'}]

    execute = {
        'kind': 'execute_candidate', 'candidate_id': 'H-known',
        'rationale': 'Registered candidate', 'evidence_refs': [],
    }
    action = host._parse_proposal_action(execute, _facts([]))
    host._validate_action(action, _facts([]))
    assert action.kind == 'execute_candidate'

    with pytest.raises(ProtocolError) as empty:
        host._parse_proposal_action(review, _facts([]))
    assert empty.value.details == [{
        'path': 'literature', 'rule': 'no_literature_hits',
    }]
    assert '没有检索命中' in str(empty.value)


def test_neighbor_prompt_requires_hits_and_default_planning_prompt_does_not():
    assert prompt_requires_literature('请根据近邻工作回答')
    assert prompt_requires_literature('Write a literature review of the neighbors')
    assert not prompt_requires_literature('执行已登记候选')
    assert not prompt_requires_literature('请给出查新结论')
    assert not prompt_requires_literature(decision_instructions())
    from popper.research.literature_context import literature_request_blocked
    assert literature_request_blocked([], require_literature=True)
    assert literature_request_blocked([], planning_prompt='根据近邻工作回答')
    assert not literature_request_blocked([], require_literature=False, planning_prompt=None)
    assert not literature_request_blocked([{'id': 'LIT-1'}], require_literature=True)

import pytest

from evaluation.execute_authored_decision import extract_authored_source


SOURCE = "def solve(rhs, y0, times, budget, config):\n    return [list(y0)]"


def test_exact_code_and_markdown_identity():
    text = "**Next candidate_index**: 3\n```python\n" + SOURCE + "\n```"
    assert extract_authored_source(text, 3) == (SOURCE + "\n", True)


@pytest.mark.parametrize("heading", ["## 决策：停止（不修复）", "## Decision: Stop"])
def test_stop_with_source_display_is_not_a_proposal(heading):
    text = heading + "\ncandidate_index=3\n```python\n" + SOURCE + "\n```"
    with pytest.raises(RuntimeError, match="chose stop"):
        extract_authored_source(text, 3)


def test_repair_requires_explicit_request_scope():
    text = "Decision: Repair\n```python\n" + SOURCE + "\n```"
    with pytest.raises(RuntimeError, match="unambiguous"):
        extract_authored_source(text, 3)
    assert extract_authored_source(text, 3, explicit_repair_scope=True) == (SOURCE + "\n", False)


def test_cannot_infer_index_or_merge_sources():
    text = "candidate_index=30\n```python\n" + SOURCE + "\n```"
    with pytest.raises(RuntimeError, match="unambiguous"):
        extract_authored_source(text, 3)
    text = "candidate_index=3\n```python\n" + SOURCE + "\n```\n```python\n" + SOURCE + "\n```"
    with pytest.raises(RuntimeError, match="merging"):
        extract_authored_source(text, 3)


@pytest.mark.parametrize("index", [True, -1, 6])
def test_index_is_exact_integer(index):
    with pytest.raises(ValueError):
        extract_authored_source("candidate_index=3", index)

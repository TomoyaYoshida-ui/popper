"""Manuscript writing gate: numeric/scientific claims need claim/ev/ref markers.

This is not a paper generator. It only refuses to treat unmarked or
unverifiable numeric conclusions as a completed manuscript when materializing.
Cited observation / LIT- / SCOOP- / FT- ids must already exist in the study
context passed by the caller.
"""
from __future__ import annotations

import re
from pathlib import Path

from ..core import ProtocolError
from ..evidence import (CLAIM_PATTERN, EV_PATTERN, REF_PATTERN,
                        parse_manuscript, _claim_regions, _inside_any_channel)

GATE_FAILED = "gate_failed"
PASSED = "passed"

# Context ids that must resolve against the current study projection.
_CONTEXT_PREFIXES = ("LIT-", "SCOOP-", "FT-", "OBS-")
_CONTEXT_ID = re.compile(r"\b(?:LIT|SCOOP|FT|OBS)-[A-Za-z0-9_-]{1,80}\b")

# Scientific/numeric conclusions (aligned with chat gate intent; not every year).
_NUMERIC_CLAIM = re.compile(
    r"\d+(?:\.\d+)?\s*%"
    r"|百分之\s*\d"
    r"|(?<![A-Za-z0-9])p(?:\s*|-)?(?:值|value)\s*[=:：<>≤≥]?\s*\d"
    r"|(?<![A-Za-z0-9])p\s*[=<>≤≥]\s*\d"
    r"|置信区间|confidence interval"
    r"|\bCI\d*\b[^0-9\n]{0,6}\d"
    r"|\[[\s]*-?\d+(?:\.\d+)?\s*[,，]\s*-?\d+(?:\.\d+)?\s*\]"
    r"|(?:改善|提升|降低|下降|增幅|降幅|improved|improvement|reduced|reduction|delta|Δ)"
    r"[^0-9\n]{0,12}-?\d"
    r"|\b(?:MAE|MSE|RMSE|AUC|F1|accuracy|precision|recall)\b[^0-9\n]{0,8}-?\d"
    r"|(?:误差|均值|准确率|得分)\s*(?:为|是|=|:|：)\s*-?\d"
    r"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\s*(?:ms|μs|ns|kg|mg|mm|cm|°C|℃|mol|mA|kPa|MPa)\b",
    re.IGNORECASE,
)


def is_context_id(token):
    return isinstance(token, str) and token.startswith(_CONTEXT_PREFIXES)


def manuscript_body_text(manuscript_json):
    """Flatten user-authored manuscript fields for scanning (not system disclosure)."""
    if not isinstance(manuscript_json, dict):
        raise ProtocolError("manuscript 必须是 JSON 对象")
    parts = []
    for key in ("title", "abstract"):
        value = manuscript_json.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    sections = manuscript_json.get("sections", [])
    if sections is None:
        sections = []
    if not isinstance(sections, list):
        raise ProtocolError("manuscript sections 必须是列表")
    for item in sections:
        if isinstance(item, dict):
            heading = item.get("heading")
            body = item.get("body")
            if isinstance(heading, str) and heading.strip():
                parts.append(heading)
            if isinstance(body, str) and body.strip():
                parts.append(body)
        elif item is not None:
            parts.append(str(item))
    return "\n".join(parts)


def _has_markers(parsed):
    return bool(parsed["claims"] or parsed["evs"] or parsed["refs"])


def _has_scientific_numeric_claim(text):
    if not isinstance(text, str) or not text.strip():
        return False
    if _NUMERIC_CLAIM.search(text):
        return True
    parsed = parse_manuscript(text)
    regions = _claim_regions(text)
    for bare in parsed["bare_numbers"]:
        if _inside_any_channel(text, bare["pos"], regions):
            continue
        # Ignore lone 4-digit years outside claim channels.
        token = bare["text"]
        if re.fullmatch(r"19\d{2}|20\d{2}", token):
            continue
        return True
    return False


def cited_context_ids(text):
    """Observation / literature / scoop / fulltext ids referenced in the manuscript."""
    found = []
    seen = set()

    def _add(token):
        if not is_context_id(token) or token in seen:
            return
        seen.add(token)
        found.append(token)

    for pattern in (EV_PATTERN, REF_PATTERN, CLAIM_PATTERN):
        for match in pattern.finditer(text or ""):
            _add(match.group(1))
    for match in _CONTEXT_ID.finditer(text or ""):
        _add(match.group(0))
    return found


def validate_manuscript_writing(text, *, known_ids=()):
    """Return {passed, status, problems}. Does not write files."""
    known = {token for token in known_ids
             if isinstance(token, str) and token.strip()}
    text = text if isinstance(text, str) else ""
    problems = []
    parsed = parse_manuscript(text) if text else {
        "claims": [], "evs": [], "refs": [], "bare_numbers": [],
    }
    scientific = _has_scientific_numeric_claim(text)
    markers = _has_markers(parsed)
    cited = cited_context_ids(text)

    if scientific and not markers:
        problems.append({
            "type": "缺标记",
            "text": "数字结论或科学主张必须带 [[claim:]] / [[ev:]] / [[ref:]] 标记",
        })

    if scientific and markers and not cited:
        problems.append({
            "type": "缺上下文引用",
            "text": "数字结论须引用当前 study 中的 observation_id 或 LIT-/SCOOP-/FT-*",
        })

    for token in cited:
        if token not in known:
            problems.append({"type": "无效 id", "text": token})

    passed = not problems
    return {
        "passed": passed,
        "status": PASSED if passed else GATE_FAILED,
        "problems": problems,
        "cited_context_ids": cited,
        "scientific_claim": scientific,
        "has_markers": markers,
    }


def enforce_manuscript_writing(manuscript_json, *, known_ids=()):
    """Raise ProtocolError when the writing gate fails. Returns the gate result."""
    text = manuscript_body_text(manuscript_json)
    result = validate_manuscript_writing(text, known_ids=known_ids)
    if result["passed"]:
        return result
    detail = "；".join(
        f"{item['type']}:{item['text']}" for item in result["problems"]
    ) or "未通过"
    error = ProtocolError(f"稿件写作门禁未通过（{GATE_FAILED}）：{detail}")
    error.details = result["problems"]
    error.gate_status = GATE_FAILED
    raise error


def load_known_manuscript_ids(*roots, run_dirs=()):
    """Collect citable ids from study / campaign roots for the writing gate."""
    from .literature_context import workspace_evidence_ids

    directories = [Path(item) for item in run_dirs if item is not None]
    search = [Path(item) for item in roots if item is not None]
    if not directories and not search:
        return ()
    observations, citable = workspace_evidence_ids(directories or search, search or directories)
    return tuple(dict.fromkeys([*observations, *citable]))

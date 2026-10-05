"""Workspace-owned, idempotent materialization of a checked bound draft."""
from __future__ import annotations

import tempfile
import re
from pathlib import Path

from ..core import ProtocolError, digest, read_json, write_json
from .bound_manuscript import (_validate_input, materialize_bound_draft,
                               verify_bound_draft)
from .evidence_binding import (export_research_evidence,
                               render_research_evidence,
                               verify_research_evidence)


def _draft_root(project_dir):
    project = Path(project_dir).resolve()
    parent = project / ".popper" / "research-bound-drafts"
    if not parent.resolve().is_relative_to(project) or parent.is_symlink():
        raise ProtocolError("证据绑定草稿目录越出项目范围")
    return parent


def bound_draft_paths(project_dir, key):
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ProtocolError("证据绑定草稿标识不合法")
    target = _draft_root(project_dir) / key
    if target.is_symlink() or not target.is_dir():
        raise FileNotFoundError("证据绑定草稿不存在")
    return target, target / "evidence", target / "draft", target / "draft-spec.json"


def list_workbench_bound_drafts(project_dir, run_dir, study_id):
    parent = _draft_root(project_dir)
    if not parent.is_dir():
        return []
    rows = []
    for candidate in sorted(parent.iterdir(), key=lambda item: item.name):
        if not re.fullmatch(r"[0-9a-f]{64}", candidate.name):
            continue
        try:
            _, evidence, draft, spec_path = bound_draft_paths(project_dir, candidate.name)
            recorded = read_json(evidence / "research-evidence.json")
            if recorded.get("study_id") != study_id:
                continue
            checked = verify_bound_draft(project_dir, run_dir, evidence,
                                         spec_path, draft)
            rows.append({"draft_id": candidate.name, "ok": True,
                         "claim_count": checked["claim_count"],
                         "manuscript_path": str(draft / "manuscript.md")})
        except (OSError, ValueError, ProtocolError, KeyError) as error:
            rows.append({"draft_id": candidate.name, "ok": False,
                         "reason": str(error)})
    return rows


def read_workbench_bound_draft(project_dir, run_dir, key):
    target, evidence, draft, spec_path = bound_draft_paths(project_dir, key)
    checked = verify_bound_draft(project_dir, run_dir, evidence, spec_path, draft)
    manuscript_path = draft / "manuscript.md"
    return {"draft_id": key, "status": "draft_needs_review",
            "claim_count": checked["claim_count"],
            "draft_dir": str(draft), "evidence_dir": str(evidence),
            "draft_spec_path": str(spec_path),
            "manuscript_path": str(manuscript_path),
            "manuscript": manuscript_path.read_text(encoding="utf-8"),
            "publication_attested": False}


def preview_agent_bound_draft(project_dir, run_dir, title, author_note,
                              expected_state_version):
    """Bind an Agent's prose proposal to the current complete Claim set."""
    if type(expected_state_version) is not int or expected_state_version < 1:
        raise ProtocolError("草稿需绑定有效 Study 状态版本")
    if not isinstance(author_note, str) or len(author_note) > 4000:
        raise ProtocolError("作者说明须为不超过 4000 字的文本")
    evidence = render_research_evidence(project_dir, run_dir)["bundle"]
    if evidence["study_state_version"] != expected_state_version:
        raise ProtocolError("Study 状态已变化，请重新读取证据后提议草稿")
    claim_ids = sorted(row["claim_id"] for row in evidence["bindings"])
    if not claim_ids:
        raise ProtocolError("Study 尚无可绑定 Claim，不能生成结果草稿")
    blocks = [{"kind": "claim", "claim_id": claim_id} for claim_id in claim_ids]
    if author_note.strip():
        blocks.append({"kind": "author_note", "text": author_note.strip()})
    spec = {"schema_version": "1.0", "title": title,
            "sections": [{"heading": "研究结果", "blocks": blocks}]}
    _validate_input(spec, {row["claim_id"]: row for row in evidence["bindings"]})
    request = {"study_id": evidence["study_id"],
               "state_version": expected_state_version,
               "research_manifest_sha256": evidence["research_manifest_sha256"],
               "claim_ids": claim_ids, "spec": spec}
    return {**request, "approval_sha256": digest(request),
            "author_note_verified": False,
            "warning": "实验结果只由真实 Claim 生成；作者说明未验证，草稿仍需人工审校"}


def create_workbench_bound_draft(project_dir, run_dir, spec):
    project, run = Path(project_dir).resolve(), Path(run_dir).resolve()
    checked = render_research_evidence(project, run)
    bundle = checked["bundle"]
    _validate_input(spec, {row["claim_id"]: row for row in bundle["bindings"]})
    key = digest({"study_id": bundle["study_id"],
                  "state_version": bundle["study_state_version"],
                  "research_manifest_sha256": bundle["research_manifest_sha256"],
                  "spec": spec})
    parent = _draft_root(project)
    target = parent / key
    evidence = target / "evidence"
    draft = target / "draft"
    spec_path = target / "draft-spec.json"
    if target.exists():
        if target.is_symlink() or read_json(spec_path) != spec:
            raise ProtocolError("既有证据绑定草稿目录与请求不一致")
        verify_research_evidence(project, run, evidence)
        verify_bound_draft(project, run, evidence, spec_path, draft)
        replayed = True
    else:
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="popper-bound-workbench-", dir=parent) as stage:
            staged = Path(stage)
            write_json(staged / "draft-spec.json", spec)
            export_research_evidence(project, run, staged / "evidence")
            materialize_bound_draft(project, run, staged / "evidence",
                                    staged / "draft-spec.json", staged / "draft")
            if target.exists():
                raise ProtocolError("证据绑定草稿目录在生成期间被创建")
            staged.rename(target)
        verify_research_evidence(project, run, evidence)
        verify_bound_draft(project, run, evidence, spec_path, draft)
        replayed = False
    return {"status": "draft_needs_review", "replayed": replayed,
            "draft_id": key,
            "study_id": bundle["study_id"], "claim_count": len(bundle["bindings"]),
            "draft_dir": str(draft), "evidence_dir": str(evidence),
            "draft_spec_path": str(spec_path),
            "manuscript_path": str(draft / "manuscript.md"),
            "manuscript": (draft / "manuscript.md").read_text(encoding="utf-8"),
            "publication_attested": False}

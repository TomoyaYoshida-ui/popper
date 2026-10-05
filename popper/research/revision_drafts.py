"""Explicit, reviewed creation of an unexecuted initial CodeRevision.

This does not spend budget, run code, or create an Observation. The caller must
separately authorize execution through the Research Controller.
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from ..core import Experiment, ProtocolError, digest, file_hash, portable_path, read_json
from ..workspace_files import ProjectFiles
from .. import revision_tools
from .readout import controller_routing_readiness, read_research_state
from .revisions import CodeEdit, RevisionStore, source_control_provenance


def failed_repair_parent(run_dir, state, parent_revision_id, hypothesis_id, design_id):
    parent = RevisionStore(Path(run_dir) / "revisions").verify(parent_revision_id)
    if (parent["identity"]["hypothesis_id"] != hypothesis_id
            or parent["identity"]["design_id"] != design_id):
        raise ProtocolError("修复父版本必须属于同一 hypothesis/design")
    runs = [row for row in state.get("runs", []) if row.get("revision_id") == parent_revision_id]
    if (not runs or any(row["status"] not in {"implementation_failed", "infra_failed", "cancelled"}
                        for row in runs)):
        raise ProtocolError("只有已终态失败的 Revision 可以提出修复；不能重跑成功或运行中版本")
    return parent


def apply_exact_edits(source, edits):
    """Apply a whole deterministic edit transaction in memory, never to a file."""
    revision_tools.validate_edit({"edits": edits})
    result = source
    for index, edit in enumerate(edits):
        matches = result.count(edit["search"])
        if matches != 1:
            raise ProtocolError(f"SEARCH/REPLACE edit {index + 1} requires exactly one match; found {matches}")
        result = result.replace(edit["search"], edit["replace"], 1)
    if len(result.encode("utf-8")) > 65536:
        raise ProtocolError("Revision replacement exceeds 64 KiB")
    return result


def resolve_initial_revision(run_dir, workspace_root, spec):
    """Return the approval projection and exact resolved source without mutation."""
    revision_tools.validate("research.revision_preview", spec)
    required = revision_tools.SPEC_COMMON_FIELDS
    if (type(spec["expected_state_version"]) is not int
            or spec["expected_state_version"] < 1
            or not isinstance(spec["expected_sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", spec["expected_sha256"]) is None):
        raise ProtocolError("CodeRevision 版本或源码摘要不合法")
    run_dir = Path(run_dir).resolve()
    workspace_root = Path(workspace_root).resolve()
    manifest = read_json(run_dir / "research.json")
    project = Path(manifest["project"]).resolve()
    if project != workspace_root or manifest.get("study_id") != spec["study_id"]:
        raise ProtocolError("研究项目不属于当前登记工作区")
    state = read_research_state(run_dir)
    if (not state.get("ok") or not state.get("experiment_graph", {}).get(
            "integrity", {}).get("ok")):
        raise ProtocolError("研究状态或来源图未通过完整性检查")
    if state.get("state_version") != spec["expected_state_version"]:
        raise ProtocolError("Study 状态版本已变化，请重新预览")
    if state["study"].get("status") != "active":
        raise ProtocolError("只有 active Study 能创建初始 CodeRevision")
    hypothesis = next((row for row in state.get("hypotheses", [])
                       if row.get("hypothesis_id") == spec["hypothesis_id"]
                       and row.get("study_id") == spec["study_id"]), None)
    design = next((row for row in state.get("designs", [])
                   if row.get("design_id") == spec["design_id"]
                   and row.get("study_id") == spec["study_id"]), None)
    if (hypothesis is None or hypothesis.get("status") != "untested"
            or spec["hypothesis_id"] == manifest.get("control_hypothesis_id")
            or design is None or design.get("hypothesis_id") != spec["hypothesis_id"]
            or design.get("status") != "frozen"):
        raise ProtocolError("修订必须绑定当前 Study 的未测试候选和已冻结设计")
    readiness = controller_routing_readiness(run_dir, state, design)
    if not readiness["controller_routing_ready"]:
        raise ProtocolError("设计不满足当前 Controller 路由: "
                            + ", ".join(readiness["reasons"]))
    exp = Experiment(project, readonly=True)
    try:
        core = exp.verify_inputs()
    finally:
        exp.close()
    if core["phase"] != "searching":
        raise ProtocolError("实验已离开 searching 阶段")
    path = spec["path"]
    if (not isinstance(path, str) or portable_path(path) != path
            or path not in {portable_path(name) for name in core["spec"]["code_files"]}):
        raise ProtocolError("修订路径不是预注册源码文件")
    parent_id = spec.get("parent_revision_id")
    if parent_id:
        parent = failed_repair_parent(run_dir, state, parent_id, spec["hypothesis_id"], spec["design_id"])
        parent_path = run_dir / "revisions" / parent_id / "code" / path
        source = {"sha256": parent["files"][path], "content": parent_path.read_text(encoding="utf-8")}
    else:
        source = ProjectFiles(project).read(path)
    if (source["sha256"] != spec["expected_sha256"]
            or (not parent_id and core["input_hashes"].get(path) != spec["expected_sha256"])):
        raise ProtocolError("冻结源码摘要已变化，请重新读取")
    replacement = spec["replacement"] if "replacement" in spec else apply_exact_edits(source["content"], spec["edits"])
    edit = CodeEdit(path, replacement, spec["expected_sha256"])
    if edit.replacement == source["content"]:
        raise ProtocolError("修订提案没有实际源码修改")
    source_control = source_control_provenance(project, core["spec"]["code_files"])
    proposal = {**{key: spec[key] for key in required if key != "replacement"},
                "replacement_sha256": digest(edit.replacement),
                "source_control": source_control}
    if "edits" in spec:
        # The approval binds both the model's original edits and their exact
        # deterministic conversion, not just a similar resulting source.
        proposal["edit_input_sha256"] = digest(spec["edits"])
    if parent_id:
        proposal["parent_revision_id"] = parent_id
    diff = "".join(difflib.unified_diff(
        source["content"].splitlines(keepends=True),
        edit.replacement.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))
    if len(diff.encode("utf-8")) > 200_000:
        raise ProtocolError("修订差异超过 200 KiB")
    preview = {"study_id": spec["study_id"], "hypothesis_id": spec["hypothesis_id"],
            "design_id": spec["design_id"], "path": path,
            "expected_state_version": spec["expected_state_version"],
            "proposal_sha256": digest(proposal), "source_control": source_control,
            "replacement_sha256": digest(edit.replacement),
            "edit_mode": "search_replace" if "edits" in spec else "replacement",
            "parent_revision_id": parent_id,
            "diff": diff, "effect": "创建不可变 Revision；不执行、不计预算、不产生 Observation"}
    if "edits" in spec:
        preview["edit_input_sha256"] = digest(spec["edits"])
    resolved = {key: value for key, value in spec.items() if key != "edits"}
    resolved["replacement"] = edit.replacement
    return preview, resolved


def preview_initial_revision(run_dir, workspace_root, spec):
    return resolve_initial_revision(run_dir, workspace_root, spec)[0]


def submit_initial_revision(run_dir, workspace_root, spec, proposal_sha256, *, actor="local_user"):
    if (not isinstance(proposal_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", proposal_sha256) is None):
        raise ProtocolError("CodeRevision 审批摘要不合法")
    preview, resolved = resolve_initial_revision(run_dir, workspace_root, spec)
    if preview["proposal_sha256"] != proposal_sha256:
        raise ProtocolError("修订提案已变化，请重新审查差异")
    project = Path(workspace_root).resolve()
    store = RevisionStore(Path(run_dir) / "revisions")
    revision = store.create(
        project, spec["hypothesis_id"], spec["design_id"],
        [CodeEdit(spec["path"], resolved["replacement"], spec["expected_sha256"])],
        actor=actor, expected_source_control=preview["source_control"],
        parent_revision_id=spec.get("parent_revision_id"))
    verified = store.verify(revision["revision_id"])
    graph = read_research_state(run_dir).get("experiment_graph", {})
    if not graph.get("integrity", {}).get("ok"):
        raise ProtocolError("修订已保存但研究来源图未通过完整性检查")
    return {"revision_id": verified["revision_id"],
            "manifest_sha256": file_hash(store.path(verified["revision_id"]) /
                                         "revision.json"),
            "study_id": spec["study_id"], "design_id": spec["design_id"],
            "status": "immutable_unexecuted", "proposal_sha256": proposal_sha256}

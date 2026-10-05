"""Prepare a new, explicitly preregistered follow-up experiment.

An adaptive candidate never mutates the parent Study's frozen manifest or core
candidate set. A new independent confirmation dataset is mandatory.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from ..core import (Experiment, ProtocolError, dataset, digest, evaluator_pack, file_hash,
                    initialize, inside, read_json, validate_spec, write_json)
from ..identity import EntityKind, parse_id
from .store import ResearchStore


LINEAGE_FILE = "followup-lineage.json"
CLAIM_SCOPE = "new_study_only; parent evidence is exploratory context"


def validate_followup_lineage(lineage, spec, test_sha256):
    required = {"schema_version", "kind", "parent_study_id",
                "parent_manifest_sha256", "parent_input_identity_sha256",
                "new_confirmation_sha256", "new_candidate_sha256", "claim_scope"}
    if not isinstance(lineage, dict) or set(lineage) != required:
        raise ProtocolError("后继研究血缘字段不完整或包含额外字段")
    parse_id(lineage["parent_study_id"], EntityKind.STUDY)
    if (lineage["schema_version"] != "1.0"
            or lineage["kind"] != "adaptive_followup"
            or lineage["claim_scope"] != CLAIM_SCOPE
            or any(not isinstance(lineage[key], str)
                   or not re.fullmatch(r"[0-9a-f]{64}", lineage[key])
                   for key in ("parent_manifest_sha256",
                               "parent_input_identity_sha256",
                               "new_confirmation_sha256"))
            or lineage["new_confirmation_sha256"] != test_sha256
            or lineage["new_candidate_sha256"] !=
                [digest(item) for item in spec["candidates"]]):
        raise ProtocolError("后继研究血缘与已注册输入/候选不一致")
    return lineage


def prepare_followup_project(parent_run_dir, new_project, candidates, new_test,
                             *, budget, objective=None):
    parent_run_dir = Path(parent_run_dir).resolve()
    new_project = Path(new_project).resolve()
    new_test = Path(new_test).resolve()
    if new_project.exists():
        raise ProtocolError("新实验项目目录必须尚不存在")
    if not new_project.parent.is_dir():
        raise ProtocolError("新实验项目的父目录不存在")
    if not new_test.is_file():
        raise ProtocolError("必须提供已有的独立新确认集文件")
    new_test_sha = file_hash(new_test)
    manifest_path = parent_run_dir / "research.json"
    if not manifest_path.is_file() or not (parent_run_dir / "research.sqlite").is_file():
        raise ProtocolError("父研究未初始化")
    manifest = read_json(manifest_path)
    parent_project = Path(manifest["project"]).resolve()
    store = ResearchStore(parent_run_dir / "research.sqlite")
    try:
        integrity = store.verify()
        if not integrity["ok"]:
            raise ProtocolError(f"父研究事件链损坏: {integrity['reason']}")
        study = store.get("study", manifest["study_id"])
        if study is None or json.loads(study["scope"])["manifest_sha256"] != file_hash(
                manifest_path):
            raise ProtocolError("父研究清单与登记身份不一致")
    finally:
        store.close()
    experiment = Experiment(parent_project)
    try:
        state = experiment.verify_inputs()
    finally:
        experiment.close()
    if ({"input_hashes": state["input_hashes"],
         "evaluator_hash": state["evaluator_hash"]} != manifest["input_identity"]):
        raise ProtocolError("父实验输入身份与研究清单不一致")
    spec = json.loads(json.dumps(state["spec"]))
    if not isinstance(candidates, list) or not candidates or any(
            not isinstance(item, dict) for item in candidates):
        raise ProtocolError("新候选必须是非空配置对象列表")
    prior = {digest(item) for item in [spec["baseline"], *spec["candidates"]]}
    if not any(digest(item) not in prior for item in candidates):
        raise ProtocolError("后继实验至少需要一个父研究未预注册的新候选")
    if (not isinstance(objective, str) and objective is not None) or (
            isinstance(objective, str) and not objective.strip()):
        raise ProtocolError("新研究问题必须是非空文本")
    spec["candidates"] = candidates
    spec["budget"] = budget
    if objective is not None:
        spec["objective"] = objective.strip()
    provenance = list(spec.get("provenance_files", []))
    if LINEAGE_FILE in [*provenance, *spec["code_files"],
                        spec["train"], spec["dev"], spec["test"]]:
        raise ProtocolError("后继血缘文件名与父实验文件冲突")
    spec["provenance_files"] = [*provenance, LINEAGE_FILE]
    validate_spec(spec)
    old_hashes = {state["input_hashes"][spec[split]] for split in ("train", "dev", "test")}
    if new_test_sha in old_hashes:
        raise ProtocolError("新确认集不能复用父实验任一数据划分")
    pack = evaluator_pack(state["evaluator_id"])
    prior_rows = [dataset(inside(parent_project, spec[split]), state["evaluator_id"])
                  for split in ("train", "dev", "test")]
    new_rows = dataset(new_test, state["evaluator_id"])
    prior_ids = {row["id"] for rows in prior_rows for row in rows}
    prior_identity = {pack.row_identity(row) for rows in prior_rows for row in rows}
    if (prior_ids & {row["id"] for row in new_rows}
            or prior_identity & {pack.row_identity(row) for row in new_rows}):
        raise ProtocolError("新确认集与父实验数据存在样本重叠")
    pack.validate_splits([prior_rows[0], prior_rows[1], new_rows])
    staging = Path(tempfile.mkdtemp(prefix=".popper-followup-", dir=new_project.parent))
    try:
        for name in [*spec["code_files"], *provenance, spec["train"], spec["dev"]]:
            source = inside(parent_project, name)
            if not source.is_file() or file_hash(source) != state["input_hashes"][name]:
                raise ProtocolError(f"父实验文件身份已变化: {name}")
            destination = staging / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        test_destination = staging / spec["test"]
        test_destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(new_test, test_destination)
        if file_hash(test_destination) != new_test_sha:
            raise ProtocolError("新确认集复制后摘要不一致")
        lineage = {
            "schema_version": "1.0", "kind": "adaptive_followup",
            "parent_study_id": manifest["study_id"],
            "parent_manifest_sha256": file_hash(manifest_path),
            "parent_input_identity_sha256": digest(manifest["input_identity"]),
            "new_confirmation_sha256": new_test_sha,
            "new_candidate_sha256": [digest(item) for item in candidates],
            "claim_scope": CLAIM_SCOPE,
        }
        write_json(staging / LINEAGE_FILE, lineage)
        write_json(staging / "experiment.json", spec)
        initialized = initialize(staging)
        staging.rename(new_project)
        return {"project": str(new_project), "lineage": lineage,
                "experiment_phase": initialized["phase"],
                "candidate_count": len(candidates), "budget": budget,
                "research_initialized": False}
    except Exception:
        if staging.is_dir() and staging.parent == new_project.parent:
            shutil.rmtree(staging)
        raise


def create_followup_study(parent_run_dir, new_project, new_run_dir, candidates,
                          new_test, *, budget, objective=None):
    """Create a follow-up Project and Study without masking partial failures.

    Filesystem project creation and SQLite Study initialization cannot share a
    transaction. If the second phase fails, preserve the new project for
    inspection/recovery rather than deleting preregistered inputs behind the
    user's back. Both destination paths must be new and independent.
    """
    from .controller import ResearchController

    new_project = Path(new_project).resolve()
    new_run_dir = Path(new_run_dir).resolve()
    if new_run_dir.exists():
        raise ProtocolError("新研究运行目录必须尚不存在")
    if (new_run_dir == new_project or new_run_dir.is_relative_to(new_project)
            or new_project.is_relative_to(new_run_dir)):
        raise ProtocolError("新项目与新研究运行目录不能重叠")
    if not new_run_dir.parent.is_dir():
        raise ProtocolError("新研究运行目录的父目录不存在")
    prepared = prepare_followup_project(
        parent_run_dir, new_project, candidates, new_test,
        budget=budget, objective=objective)
    try:
        research = ResearchController.initialize(new_project, new_run_dir)
    except Exception as error:
        raise ProtocolError(
            f"后继项目已建立于 {new_project}，但 Study 初始化失败；"
            f"请检查 {new_run_dir} 并修复后运行 research init，"
            f"原始错误: {type(error).__name__}: {error}") from error
    return {**prepared, "research_initialized": True,
            "research_run": str(new_run_dir),
            "study_id": read_json(new_run_dir / "research.json")["study_id"],
            "phase": research["phase"]}


def resume_followup_study(parent_run_dir, new_project, new_run_dir, candidates,
                          new_test, *, budget, objective=None):
    """Accept an exact replay, or resume a verified project before Study init.

    An existing directory is never adopted solely because its name matches.
    Parent identity, candidate/config protocol, copied confirmation data and
    the Research Store must all agree with the original request.
    """
    from .controller import ResearchController
    from .readout import read_research_state

    parent_run_dir = Path(parent_run_dir).resolve()
    new_project = Path(new_project).resolve()
    new_run_dir = Path(new_run_dir).resolve()
    new_test = Path(new_test).resolve()
    if not (parent_run_dir / "research.sqlite").is_file():
        raise ProtocolError("父研究库缺失，不能重放后继创建")
    manifest = read_json(parent_run_dir / "research.json")
    parent_store = ResearchStore(parent_run_dir / "research.sqlite")
    try:
        if not parent_store.verify()["ok"]:
            raise ProtocolError("父研究事件链不完整，不能重放后继创建")
    finally:
        parent_store.close()
    if not new_project.is_dir():
        raise ProtocolError("后继项目目录不存在，不能恢复")
    if not (new_project / "experiment.json").is_file() or not (
            new_project / LINEAGE_FILE).is_file() or not (
            new_project / ".popper" / "state.db").is_file():
        raise ProtocolError("已有目录不是可验证的后继实验项目")
    try:
        experiment = Experiment(new_project)
        try:
            state = experiment.verify_inputs()
        finally:
            experiment.close()
        spec = state["spec"]
        lineage = validate_followup_lineage(
            read_json(new_project / LINEAGE_FILE), spec,
            state["input_hashes"][spec["test"]])
        if (spec["candidates"] != candidates or spec["budget"] != budget
                or (objective is not None and spec["objective"] != objective.strip())
                or lineage["parent_study_id"] != manifest["study_id"]
                or lineage["parent_manifest_sha256"] != file_hash(
                    parent_run_dir / "research.json")
                or lineage["parent_input_identity_sha256"] != digest(
                    manifest["input_identity"])
                or lineage["new_confirmation_sha256"] != file_hash(new_test)):
            raise ProtocolError("已有后继项目与本次请求内容不一致")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProtocolError(f"已有后继项目无法验证: {error}") from error
    if not new_run_dir.exists():
        try:
            research = ResearchController.initialize(new_project, new_run_dir)
        except Exception as error:
            raise ProtocolError(
                f"后继项目已验证，但 Study 初始化仍失败；请检查 {new_run_dir} "
                f"并修复后运行 research init，原始错误: "
                f"{type(error).__name__}: {error}") from error
        return {"project": str(new_project), "lineage": lineage,
                "experiment_phase": state["phase"],
                "candidate_count": len(candidates), "budget": budget,
                "research_initialized": True, "research_run": str(new_run_dir),
                "study_id": read_json(new_run_dir / "research.json")["study_id"],
                "phase": research["phase"], "replayed": True}
    if not (new_run_dir / "research.json").is_file():
        raise ProtocolError("已有研究运行目录缺少清单，请人工检查后恢复")
    research_manifest = read_json(new_run_dir / "research.json")
    if (Path(research_manifest.get("project", "")).resolve() != new_project
            or research_manifest.get("followup_lineage") != lineage):
        raise ProtocolError("已有 Study 与后继项目血缘不一致")
    projection = read_research_state(new_run_dir)
    if not projection.get("ok") or not projection.get(
            "experiment_graph", {}).get("integrity", {}).get("ok"):
        raise ProtocolError("已有后继 Study 不可验证，请人工检查")
    if projection.get("study", {}).get("entity_id") != research_manifest["study_id"]:
        raise ProtocolError("已有后继 Study 身份不一致")
    store = ResearchStore(new_run_dir / "research.sqlite")
    try:
        if not store.verify()["ok"]:
            raise ProtocolError("已有后继 Study 事件链不完整")
    finally:
        store.close()
    return {"project": str(new_project), "lineage": lineage,
            "experiment_phase": state["phase"], "candidate_count": len(candidates),
            "budget": budget, "research_initialized": True,
            "research_run": str(new_run_dir),
            "study_id": research_manifest["study_id"],
            "phase": "researching", "replayed": True}

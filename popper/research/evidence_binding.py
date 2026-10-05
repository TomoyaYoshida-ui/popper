"""Deterministic, locally verifiable bindings from Research Claims to prose.

This is an evidence appendix, not a scientific manuscript or portable replay.
The original ResearchStore and source artifacts remain the authority.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from ..core import (EVALUATORS, ProtocolError, digest, file_hash, inside,
                    canonical, read_json, write_json)
from .evaluation_service import scoring_code_hash
from .readout import read_research_state
from .store import ResearchStore


_VERDICTS = {
    "supported_in_scope": "在预注册确认范围内达到最小有意义效应",
    "not_supported_in_scope": "在预注册确认范围内未达到最小有意义效应",
    "boundary_supported_in_scope": "整体效应未过阈值，但预注册切片支持存在边界",
    "boundary_not_supported_in_scope": "未确认预注册切片边界",
}


def _derive(project_dir, run_dir):
    project, run = Path(project_dir).resolve(), Path(run_dir).resolve()
    manifest_path = run / "research.json"
    if not manifest_path.is_file() or not (run / "research.sqlite").is_file():
        raise ProtocolError("研究证据导出需要已登记的 Research Run")
    manifest = read_json(manifest_path)
    if Path(manifest.get("project", "")).resolve() != project:
        raise ProtocolError("Research Run 未绑定当前项目")
    store = ResearchStore(run / "research.sqlite", readonly=True)
    try:
        audit = store.verify()
        if not audit["ok"]:
            raise ProtocolError(f"研究证据完整性校验失败: {audit['reason']}")
        cursor = store.event_cursor()
        study_id = manifest["study_id"]
        study = store.get("study", study_id)
        if study is None:
            raise ProtocolError("研究证据缺少 Study")
        if study["status"] not in {"concluded", "budget_exhausted"}:
            raise ProtocolError("Study 尚未终结，不能导出最终证据附录")
        try:
            scope = json.loads(study["scope"])
        except (KeyError, TypeError, ValueError):
            raise ProtocolError("Study 缺少可核验的研究登记范围") from None
        if (not isinstance(scope, dict) or scope.get("project") != str(project)
                or scope.get("manifest_sha256") != file_hash(manifest_path)):
            raise ProtocolError("Research manifest 与 Study 冻结登记不一致")
        if manifest.get("scoring_code_sha256") != scoring_code_hash():
            raise ProtocolError("独立计分代码与冻结研究版本不一致")
        evaluator_id = manifest.get("scorer_id")
        if (evaluator_id not in EVALUATORS
                or manifest.get("scorer_hash") != digest(EVALUATORS[evaluator_id])):
            raise ProtocolError("领域评估器与冻结研究版本不一致")
        identity = manifest.get("input_identity", {})
        hashes = identity.get("input_hashes") if isinstance(identity, dict) else None
        if not isinstance(hashes, dict) or not hashes:
            raise ProtocolError("研究输入缺少冻结摘要")
        for name, expected in hashes.items():
            try:
                current_hash = file_hash(inside(project, name))
            except (OSError, TypeError, ValueError):
                raise ProtocolError(f"已登记研究输入不可读: {name}") from None
            if current_hash != expected:
                raise ProtocolError(f"已登记研究输入发生变化: {name}")
        claims = [row for row in store.list_with_ids("claim")
                  if row["study_id"] == study_id]
        if not claims:
            raise ProtocolError("研究尚无独立确认 Claim，不能生成证据附录")
        bindings = []
        for claim in sorted(claims, key=lambda row: row["entity_id"]):
            if claim["status"] not in _VERDICTS:
                raise ProtocolError("研究 Claim 裁定状态不受支持")
            sources = []
            for observation_id in claim["observation_refs"]:
                observation = store.get("observation", observation_id)
                if observation is None or observation["study_id"] != study_id:
                    raise ProtocolError("Claim 引用的 Observation 缺失或跨 Study")
                sources.append({
                    "observation_id": observation_id,
                    "run_id": observation["run_id"],
                    "scope": observation["scope"],
                    "value": observation["value"],
                    "unit": observation["unit"],
                    "uncertainty": observation["uncertainty"],
                    "artifact_sha256": observation["artifact_sha256"],
                    "selector": observation["selector"],
                })
            core = {
                "claim_id": claim["entity_id"], "status": claim["status"],
                "verdict": _VERDICTS[claim["status"]],
                "scope": claim["scope"],
                "metric": claim["metric"],
                "metric_direction": claim["metric_direction"],
                "oriented_delta": claim["oriented_delta"],
                "min_meaningful_effect": claim["min_meaningful_effect"],
                "slice_effects": claim.get("slice_effects", []),
                "design_id": claim["design_id"],
                "decision_id": claim["decision_id"],
                "observation_refs": claim["observation_refs"],
                "sources": sources,
                "receipt_sha256": claim.get("receipt_sha256"),
                "trust": claim.get("trust", "separate_process_same_account"),
                "limitations": claim["limitations"],
            }
            bindings.append({"binding_id": "EB-" + digest(core)[:24], **core})
        projection = read_research_state(run)
        if (not projection.get("ok")
                or not projection["experiment_graph"]["integrity"]["ok"]):
            raise ProtocolError("研究来源图不完整，不能生成证据附录")
        if store.event_cursor() != cursor:
            raise ProtocolError("研究在证据导出期间发生变化，请重试")
        return {
            "schema_version": "1.0", "kind": "research_evidence_bindings",
            "verification_level": "local_research_store_and_source_artifacts",
            "study_id": study_id, "study_state_version": study["state_version"],
            "research_manifest_sha256": file_hash(manifest_path),
            "bindings": bindings,
            "limitations": ["not_a_portable_reproduction_package",
                            "not_an_institutionally_independent_review",
                            "not_a_reviewed_manuscript"],
        }
    finally:
        store.close()


def _markdown(bundle):
    lines = ["# 研究证据附录", "",
             "> 从已验证的 Research Claim 与 Observation 确定性生成；不是人工审校稿件、"
             "机构独立复核或便携复现实验。", "",
             f"Study：`{bundle['study_id']}`；状态版本：{bundle['study_state_version']}", ""]
    for item in bundle["bindings"]:
        lines.extend([
            f"## Claim `{item['claim_id']}`", "",
            f"{item['verdict']}。指标 `{item['metric']}` 的方向统一效应为 "
            f"`{item['oriented_delta']!r}`，预注册阈值为 "
            f"`{item['min_meaningful_effect']!r}`。", "",
            f"适用范围：`{item['scope']}`；设计：`{item['design_id']}`；"
            f"决策：`{item['decision_id']}`。", "",
            "证据绑定：", "",
        ])
        for source in item["sources"]:
            lines.append(f"- `{source['observation_id']}` / `{source['run_id']}` "
                         f"/ `{source['scope']}`：`{source['value']!r}` "
                         f"（制品 SHA-256 `{source['artifact_sha256']}`，"
                         f"selector `{source['selector']}`）")
        if item["slice_effects"]:
            lines.extend(["", "预注册切片方向统一效应：", ""])
            for row in item["slice_effects"]:
                lines.append(f"- `{row['slice_id']}`：`{row['oriented_delta']!r}`")
        if item["receipt_sha256"]:
            lines.extend(["", f"签名回执 SHA-256：`{item['receipt_sha256']}`；"
                          f"信任级别：`{item['trust']}`。"])
        lines.extend(["", "限制：不证明机制，不自动推广到预注册范围之外。", "",
                      f"绑定 ID：`{item['binding_id']}`。", ""])
    return "\n".join(lines)


def render_research_evidence(project_dir, run_dir):
    """Return a checked, read-only appendix for an explicit workbench download."""
    bundle = _derive(project_dir, run_dir)
    return {"bundle": bundle, "json_text": canonical(bundle) + "\n",
            "markdown": _markdown(bundle)}


def export_research_evidence(project_dir, run_dir, out_dir):
    """Write an immutable-by-policy evidence appendix into a new directory."""
    bundle = _derive(project_dir, run_dir)
    target = Path(out_dir).absolute()
    if target.exists():
        raise ProtocolError("证据附录输出目录已存在，不能覆盖")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="popper-evidence-", dir=target.parent) as staged:
        staging = Path(staged)
        write_json(staging / "research-evidence.json", bundle)
        (staging / "research-evidence.md").write_text(
            _markdown(bundle), encoding="utf-8")
        if target.exists():
            raise ProtocolError("证据附录输出目录在生成期间被创建")
        staging.rename(target)
    return {"status": "exported", "output_dir": str(target),
            "claim_count": len(bundle["bindings"]),
            "verification_level": bundle["verification_level"]}


def verify_research_evidence(project_dir, run_dir, out_dir):
    """Fail closed if the appendix or its original research evidence changed."""
    target = Path(out_dir)
    path, markdown = target / "research-evidence.json", target / "research-evidence.md"
    if (not target.is_dir() or path.is_symlink() or markdown.is_symlink()
            or not path.is_file() or not markdown.is_file()
            or {item.name for item in target.iterdir()} !=
               {"research-evidence.json", "research-evidence.md"}):
        raise ProtocolError("证据附录缺少 JSON 或 Markdown 文件")
    expected = _derive(project_dir, run_dir)
    try:
        recorded = read_json(path)
        rendered = markdown.read_text(encoding="utf-8")
    except (OSError, ValueError, UnicodeError):
        raise ProtocolError("证据附录不可读或格式损坏") from None
    if recorded != expected or rendered != _markdown(expected):
        raise ProtocolError("证据附录与当前权威研究状态不一致")
    return {"ok": True, "claim_count": len(expected["bindings"]),
            "verification_level": expected["verification_level"]}

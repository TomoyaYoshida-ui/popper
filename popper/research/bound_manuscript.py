"""A structured draft whose result paragraphs are generated from Claims.

Author notes remain visibly unverified. This does not prove their assertions or
replace editorial/scientific review; it prevents the renderer from inventing
numeric result statements or selectively omitting recorded Claims.
"""
from __future__ import annotations

import hashlib
import tempfile
from html import escape
from pathlib import Path

from ..core import ProtocolError, file_hash, read_json, write_json
from ..evidence import BARE_NUMBER_PATTERN
from .evidence_binding import verify_research_evidence


def _validate_input(spec, bindings):
    if not isinstance(spec, dict) or set(spec) != {"schema_version", "title", "sections"}:
        raise ProtocolError("证据绑定草稿必须包含 schema_version/title/sections")
    if spec["schema_version"] != "1.0":
        raise ProtocolError("证据绑定草稿版本不受支持")
    title, sections = spec["title"], spec["sections"]
    if (not isinstance(title, str) or not title.strip() or len(title) > 200
            or any(char in title for char in "\r\n#<>")
            or BARE_NUMBER_PATTERN.search(title)):
        raise ProtocolError("证据绑定草稿标题必须是无数字的单行纯文本")
    if not isinstance(sections, list) or not sections or len(sections) > 30:
        raise ProtocolError("证据绑定草稿 sections 必须是非空有限列表")
    seen = []
    for section in sections:
        if (not isinstance(section, dict) or set(section) != {"heading", "blocks"}
                or not isinstance(section["heading"], str)
                or not section["heading"].strip()
                or len(section["heading"]) > 120
                or any(char in section["heading"] for char in "\r\n#<>")
                or BARE_NUMBER_PATTERN.search(section["heading"])
                or not isinstance(section["blocks"], list)
                or not section["blocks"]):
            raise ProtocolError("证据绑定草稿 section 格式不合法")
        for block in section["blocks"]:
            if not isinstance(block, dict) or block.get("kind") not in {"claim", "author_note"}:
                raise ProtocolError("证据绑定草稿仅支持 claim/author_note 块")
            if block["kind"] == "claim":
                if (set(block) != {"kind", "claim_id"}
                        or not isinstance(block["claim_id"], str)
                        or block["claim_id"] not in bindings):
                    raise ProtocolError("草稿引用了未验证的 Claim")
                seen.append(block["claim_id"])
            else:
                text = block.get("text")
                if (set(block) != {"kind", "text"} or not isinstance(text, str)
                        or not text.strip() or len(text) > 4000
                        or BARE_NUMBER_PATTERN.search(text)
                        or "[[" in text or "{{" in text):
                    raise ProtocolError("作者说明含裸数字、伪 Claim 或非法文本")
    if len(seen) != len(set(seen)) or set(seen) != set(bindings):
        raise ProtocolError("草稿必须恰好一次纳入本 Study 的全部 Claim")


def _render(spec, bindings):
    lines = [f"# {spec['title'].strip()}", "",
             "> 未经人工审校的研究草稿。仅标为「系统证据绑定结果」的段落由当前"
             " Research Claim 派生；作者说明不在系统验证范围内。", "",
             "## 摘要", "",
             f"本研究登记了 {len(bindings)} 项范围限定结论。具体裁定、效应、阈值与"
             "来源见下方系统证据绑定结果；不据此声称机理已证实或范围外泛化。", ""]
    for section in spec["sections"]:
        lines.extend([f"## {section['heading'].strip()}", ""])
        for block in section["blocks"]:
            if block["kind"] == "author_note":
                lines.extend(["> 作者说明（未验证）：", ">",
                              *["> " + escape(row) for row in
                                block["text"].strip().splitlines()], ""])
                continue
            item = bindings[block["claim_id"]]
            lines.extend([
                "**系统证据绑定结果。** "
                f"{item['verdict']}。效应 `{item['effect_type']}` 为 "
                f"`{item['effect_value']!r}` `{item['effect_unit']}`，登记阈值 "
                f"`{item['threshold']!r}`，95% 区间 `{item['ci95']!r}`；范围 `{item['scope']}`。"
                f"[Claim `{item['claim_id']}`；绑定 `{item['binding_id']}`；"
                f"Observation：{', '.join(item['observation_refs'])}；"
                f"Decision `{item['decision_id']}`]", "",
            ])
            if item["slice_effects"]:
                lines.append("预注册切片方向统一效应：" + "；".join(
                    f"`{row['slice_id']}` = `{row['oriented_delta']!r}`"
                    for row in item["slice_effects"]) + "。")
                lines.append("")
            if item["receipt_sha256"]:
                lines.extend([f"签名回执 `{item['receipt_sha256']}`；"
                              f"信任级别 `{item['trust']}`。", ""])
            lines.extend(["该结论不证明机制，也不自动推广到预注册范围之外。", ""])
    lines.extend(["## 审校状态", "",
                  "本文件是证据绑定草稿，不是投稿或发表证明。作者说明、方法学解释与"
                  "外部文献仍需人工核验；应与研究证据附录及原始 Research Run 一起审阅。", ""])
    return "\n".join(lines)


def _expected(project_dir, run_dir, evidence_dir, manuscript_json):
    verify_research_evidence(project_dir, run_dir, evidence_dir)
    evidence_path = Path(evidence_dir) / "research-evidence.json"
    evidence = read_json(evidence_path)
    bindings = {item["claim_id"]: item for item in evidence["bindings"]}
    spec = read_json(manuscript_json)
    _validate_input(spec, bindings)
    rendered = _render(spec, bindings)
    manifest = {
        "schema_version": "1.0", "kind": "research_bound_draft",
        "verification_level": "generated_claim_passages_only",
        "study_id": evidence["study_id"],
        "research_evidence_sha256": file_hash(evidence_path),
        "draft_spec_sha256": file_hash(manuscript_json),
        "manuscript_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
        "claim_ids": sorted(bindings),
        "author_notes_verified": False,
    }
    return rendered, manifest


def materialize_bound_draft(project_dir, run_dir, evidence_dir,
                            manuscript_json, out_dir):
    rendered, manifest = _expected(project_dir, run_dir, evidence_dir,
                                   manuscript_json)
    target = Path(out_dir).absolute()
    if target.exists():
        raise ProtocolError("证据绑定草稿输出目录已存在，不能覆盖")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="popper-bound-draft-", dir=target.parent) as staged:
        staging = Path(staged)
        (staging / "manuscript.md").write_text(rendered, encoding="utf-8")
        write_json(staging / "binding-manifest.json", manifest)
        if target.exists():
            raise ProtocolError("证据绑定草稿输出目录在生成期间被创建")
        staging.rename(target)
    return {"status": "draft_needs_review", "output_dir": str(target),
            "claim_count": len(manifest["claim_ids"]),
            "verification_level": manifest["verification_level"]}


def verify_bound_draft(project_dir, run_dir, evidence_dir,
                       manuscript_json, out_dir):
    target = Path(out_dir)
    manuscript = target / "manuscript.md"
    manifest_path = target / "binding-manifest.json"
    if (not target.is_dir() or manuscript.is_symlink() or manifest_path.is_symlink()
            or not manuscript.is_file() or not manifest_path.is_file()
            or {item.name for item in target.iterdir()} !=
               {"manuscript.md", "binding-manifest.json"}):
        raise ProtocolError("证据绑定草稿缺少稿件或绑定清单")
    rendered, expected = _expected(project_dir, run_dir, evidence_dir,
                                   manuscript_json)
    try:
        actual_text = manuscript.read_text(encoding="utf-8")
        actual_manifest = read_json(manifest_path)
    except (OSError, ValueError, UnicodeError):
        raise ProtocolError("证据绑定草稿不可读或格式损坏") from None
    if actual_text != rendered or actual_manifest != expected:
        raise ProtocolError("证据绑定草稿与权威 Claim 或草稿结构不一致")
    return {"ok": True, "claim_count": len(expected["claim_ids"]),
            "verification_level": expected["verification_level"]}

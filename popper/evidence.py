"""证据系统 · 指标注册表 + claim registry（CS 语义版）。

- metrics.json 版本化注册表：results.json 必须用规范名，未知 = L0 失败。
- claim registry：稿件每个数字/事实声明绑定 claim ID → 证据 ID。
- ``[[ev:规范名]]`` / ``[[ref:xxx]]`` 写作通道；裸数字被门禁拒绝。
- 校验脚本（audit_claims/check_consistency/check_references/lint_manuscript）离线确定性。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .core import ProtocolError, canonical, file_hash, read_json, write_json
from .domains import registered_metrics

# 证据绑定状态
BOUND, UNBOUND, DISPUTED = "bound", "unbound", "disputed"

# 引用正文支持等级
SUPPORT_LEVELS = ("full", "partial", "none", "unavailable")


def _default_metrics():
    """缺省指标注册表：直接从领域包导出，不再维护第三套命名体系。"""
    return {"schema_version": "1.0",
            "metrics": {name: {"direction": spec.direction, "description": spec.description}
                        for name, spec in registered_metrics().items()}}


DEFAULT_METRICS = _default_metrics()

CLAIM_PATTERN = re.compile(r"\[\[claim:([A-Za-z0-9_\-]+)\]\]")
EV_PATTERN = re.compile(r"\[\[ev:([A-Za-z0-9_\-]+)\]\]")
REF_PATTERN = re.compile(r"\[\[ref:([A-Za-z0-9_\-]+)\]\]")
# 裸数字：未被 claim 通道包裹的数字串（含 %、小数点、千分位）。排除年份/版本等由调用方判定。
BARE_NUMBER_PATTERN = re.compile(r"(?<!\[\[)(?<!\w)(\d{1,3}(?:,\d{3})*\.?\d*%?)(?!\]\])(?!\w)")


def _select_json(path, selector):
    selected = read_json(path)
    for part in selector.split("."):
        selected = selected[int(part)] if isinstance(selected, list) else selected[part]
    return selected


class MetricsRegistry:
    """版本化指标注册表。"""

    def __init__(self, path=None):
        self.path = Path(path) if path else None

    def load(self):
        if self.path and self.path.is_file():
            data = read_json(self.path)
        else:
            data = DEFAULT_METRICS
        if not isinstance(data.get("metrics"), dict):
            raise ProtocolError("metrics.json 格式非法")
        return data

    def names(self):
        return set(self.load()["metrics"].keys())

    def require_registered(self, metric_name):
        if metric_name not in self.names():
            raise ProtocolError(f"未登记指标名: {metric_name}（未知指标 = L0 门禁失败）")
        return metric_name


class EvidenceStore:
    """claim registry（CS 语义版）。

    每件证据 Evidence(claim_id, value, source_ref, artifact_id, category, sha256)。
    claim 通过 source_ref 绑定到 run/tool_call/行号与 artifact。
    """

    CATEGORIES = ("claim", "note", "method", "observation", "reference")
    FIELD_GUARD = {"claim": "论文数字/事实声明", "note": "方法/代码描述",
                   "method": "实验方法/模型架构", "observation": "实验结果/指标观测",
                   "reference": "文献引用/外部事实"}

    def __init__(self, store_dir):
        self.store_dir = Path(store_dir)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.store_dir / "claims.csv.json"
        self.registry = self._load()

    def _load(self):
        if not self.manifest_path.is_file():
            return {"schema_version": "1.0", "claims": {}}
        return read_json(self.manifest_path)

    def _save(self):
        write_json(self.manifest_path, self.registry)

    def _claim(self, claim_id):
        claim = self.registry["claims"].get(claim_id)
        if claim is None:
            raise ProtocolError(f"claim 不存在: {claim_id}")
        return claim

    def register_claim(self, claim_id, text, category="claim"):
        if category not in self.CATEGORIES:
            raise ProtocolError(f"category 必须是 {self.CATEGORIES}")
        claims = self.registry["claims"]
        if claim_id in claims:
            current = claims[claim_id]
            if current.get("text") == text and current.get("category") == category:
                return {"status": "exists", "claim_id": claim_id}
            raise ProtocolError("相同 claim_id 对应不同声明")
        claims[claim_id] = {
            "claim_id": claim_id, "text": text, "category": category,
            "field": self.FIELD_GUARD[category], "evidence_ids": [],
            "value": None, "status": UNBOUND, "created": _now(),
        }
        self._save()
        return {"status": "created", "claim_id": claim_id}

    def bind(self, claim_id, evidence_id, value, source_ref, artifact_id, sha256=None,
             selector=None):
        """绑定 evidence 到 claim；必要时先登记 claim。

        artifact_id 必须指向真实文件；sha256 如提供，必须与文件摘要一致。
        所有声明都必须用 selector 从 JSON 或文本证据中重新取得同一值。
        """
        claim = self._claim(claim_id)
        artifact_path = Path(artifact_id)
        if not artifact_path.is_file():
            raise ProtocolError("artifact_id 必须指向可核验的真实文件")
        actual_sha256 = file_hash(artifact_path)
        if sha256 is not None and sha256 != actual_sha256:
            raise ProtocolError("提供的 sha256 与真实证据文件不一致")
        sha256 = actual_sha256
        if not source_ref:
            raise ProtocolError("source_ref 不能为空")
        if not selector:
            raise ProtocolError("证据绑定必须提供 selector 以从制品重算声明值")
        if selector:
            try:
                if selector.startswith("text:"):
                    selected = selector.removeprefix("text:")
                    content = artifact_path.read_text(encoding="utf-8", errors="replace")
                    if selected not in content:
                        raise KeyError(selected)
                else:
                    selected = _select_json(artifact_path, selector)
            except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
                raise ProtocolError(f"证据 selector 无法解析: {selector}") from None
            if str(selected) != str(value):
                raise ProtocolError(
                    f"claim value 与证据 selector 结果不一致: {value!r} != {selected!r}")
        existing = self.registry.setdefault("evidence", {}).get(evidence_id)
        proposed = {"evidence_id": evidence_id, "claim_id": claim_id, "value": value,
                    "source_ref": source_ref, "artifact_id": str(artifact_path),
                    "sha256": sha256, "selector": selector}
        if existing:
            comparable = {k: existing.get(k) for k in proposed}
            if comparable == proposed:
                return {"status": "duplicate", "claim_id": claim_id,
                        "evidence_id": evidence_id}
            raise ProtocolError("相同 evidence_id 对应不同证据")
        prior_values = [self.registry.get("evidence", {}).get(ev, {}).get("value")
                        for ev in claim["evidence_ids"]]
        if evidence_id not in claim["evidence_ids"]:
            claim["evidence_ids"].append(evidence_id)
        conflict = any(previous is not None and str(previous) != str(value)
                       for previous in prior_values)
        claim["value"] = None if conflict else value
        claim["status"] = DISPUTED if conflict else BOUND
        records = self.registry.setdefault("evidence", {})
        records[evidence_id] = {
            "evidence_id": evidence_id, "claim_id": claim_id, "value": value,
            "source_ref": source_ref, "artifact_id": str(artifact_path),
            "sha256": sha256, "selector": selector,
            "timestamp": _now(),
        }
        self._save()
        return {"status": "bound", "claim_id": claim_id, "evidence_id": evidence_id}

    def unbound_claims(self):
        return [c for c in self.registry["claims"].values() if c["status"] != BOUND]

    def fix_claim(self, claim_id, new_evidence_id):
        """局部修复单个 claim 的证据绑定，不触发全量重算。"""
        claim = self._claim(claim_id)
        chosen = self.registry.get("evidence", {}).get(new_evidence_id)
        if chosen is None:
            raise ProtocolError(f"evidence 不存在: {new_evidence_id}")
        if chosen.get("claim_id") != claim_id:
            raise ProtocolError("evidence 不属于目标 claim")
        claim["evidence_ids"] = [new_evidence_id]
        claim["value"] = chosen.get("value")
        claim["status"] = BOUND
        self._save()
        return {"status": "fixed", "claim_id": claim_id}

    def verify_claim_binding(self, claim_id):
        """重新读取当前绑定制品，验证摘要与 selector/value。"""
        claim = self._claim(claim_id)
        issues = []
        if claim.get("status") != BOUND or not claim.get("evidence_ids"):
            return {"ok": False, "issues": ["claim 未绑定证据"]}
        for evidence_id in claim["evidence_ids"]:
            evidence = self.registry.get("evidence", {}).get(evidence_id)
            if evidence is None or evidence.get("claim_id") != claim_id:
                issues.append(f"evidence 不存在或归属错误: {evidence_id}")
                continue
            path = Path(evidence.get("artifact_id") or "")
            if not path.is_file():
                issues.append(f"证据文件不存在: {evidence_id}")
                continue
            if file_hash(path) != evidence.get("sha256"):
                issues.append(f"证据文件摘要变化: {evidence_id}")
                continue
            selector = evidence.get("selector")
            if selector:
                try:
                    if selector.startswith("text:"):
                        selected = selector.removeprefix("text:")
                        if selected not in path.read_text(encoding="utf-8", errors="replace"):
                            raise KeyError(selected)
                    else:
                        selected = _select_json(path, selector)
                except (KeyError, IndexError, TypeError, ValueError, UnicodeError):
                    issues.append(f"证据 selector 无法解析: {evidence_id}")
                    continue
                if str(selected) != str(evidence.get("value")):
                    issues.append(f"证据值发生变化: {evidence_id}")
        return {"ok": not issues, "issues": issues}

    def register_reference(self, reference_id, status="real", metadata_artifact=None,
                           sha256=None):
        """登记并核验引用状态（供 check_references 判定 Real）。"""
        if status not in ("real", "potential", "hallucinated"):
            raise ProtocolError("引用状态必须为 real/potential/hallucinated")
        metadata_path = None
        metadata_hash = None
        if status == "real":
            metadata_path = Path(metadata_artifact or "")
            if not metadata_path.is_file():
                raise ProtocolError("real 引用必须绑定解析器保存的元数据文件")
            metadata_hash = file_hash(metadata_path)
            if sha256 is not None and sha256 != metadata_hash:
                raise ProtocolError("引用元数据 sha256 与真实文件不一致")
            try:
                metadata = read_json(metadata_path)
            except (OSError, ValueError):
                raise ProtocolError("引用元数据文件必须是有效 JSON") from None
            identities = {str(metadata.get(k)) for k in ("reference_id", "doi", "url", "id")
                          if metadata.get(k)}
            if reference_id not in identities:
                raise ProtocolError("引用元数据没有绑定 reference_id")
        refs = self.registry.setdefault("references", {})
        if reference_id in refs:
            current = refs[reference_id]
            if (current.get("status") == status
                    and current.get("metadata_artifact") == (str(metadata_path) if metadata_path else None)
                    and current.get("metadata_sha256") == metadata_hash):
                return {"status": "exists", "reference_id": reference_id,
                        "state": status}
            raise ProtocolError("相同 reference_id 对应不同核验状态")
        refs[reference_id] = {"reference_id": reference_id, "status": status,
                              "support_level": "unavailable",
                              "metadata_artifact": str(metadata_path) if metadata_path else None,
                              "metadata_sha256": metadata_hash}
        self._save()
        return {"status": "registered", "reference_id": reference_id, "state": status}

    def set_reference_support(self, reference_id, support_text, level=None,
                              artifact_id=None, sha256=None):
        """登记引用正文片段与显式支持等级 full/partial/none/unavailable。

        必须显式提供 level；full/partial 还必须绑定真实全文文件，且片段能在
        文件中逐字定位。删除按片段长度自动判定的通过路径。
        """
        if level is None:
            raise ProtocolError("必须显式提供 support_level，不允许按片段长度自动判定")
        if level not in SUPPORT_LEVELS:
            raise ProtocolError("support_level 必须为 full/partial/none/unavailable")
        refs = self.registry.setdefault("references", {})
        entry = refs.get(reference_id)
        if entry is None:
            raise ProtocolError("引用必须先登记，不能由支持声明自动创建为 real")
        if level in {"full", "partial"} and entry.get("status") != "real":
            raise ProtocolError("只有已核验为 real 的引用可以声明正文支持")
        text = str(support_text or "")
        if level in {"full", "partial"} and not text.strip():
            raise ProtocolError("full/partial 支持必须提供正文片段")
        artifact_hash = None
        if level in {"full", "partial"}:
            artifact_path = Path(artifact_id or "")
            if not artifact_path.is_file():
                raise ProtocolError("full/partial 支持必须绑定真实全文文件")
            artifact_hash = file_hash(artifact_path)
            if sha256 is not None and sha256 != artifact_hash:
                raise ProtocolError("引用全文 sha256 与真实文件不一致")
            content = artifact_path.read_text(encoding="utf-8", errors="replace")
            if " ".join(text.split()) not in " ".join(content.split()):
                raise ProtocolError("支持片段不是全文中的连续原文")
            artifact_id = str(artifact_path)
        entry.update({"support_text": text[:4000], "support_level": level,
                      "artifact_id": artifact_id, "sha256": artifact_hash})
        self._save()
        return {"status": "set", "reference_id": reference_id, "support_level": level}


def run_checks(manuscript_path, store):
    """运行一组离线确定性校验，汇总为门禁可判定的结果。"""
    return {
        "audit": audit_claims(manuscript_path, store),
        "consistency": check_consistency(manuscript_path, store),
        "references": check_references(manuscript_path, store),
        "lint": lint_manuscript(manuscript_path, store),
    }


# ---- 稿件通道解析（裸数字拦截 + claim/ev/ref 通道） ----

def parse_manuscript(text):
    """解析稿件文本：提取 claim/ev/ref 引用，标注裸数字。"""
    claims = [{"claim_id": m} for m in CLAIM_PATTERN.findall(text)]
    evs = [{"evidence_id": m} for m in EV_PATTERN.findall(text)]
    refs = [{"reference_id": m} for m in REF_PATTERN.findall(text)]
    bare = [{"text": m.group(0), "pos": m.span()} for m in BARE_NUMBER_PATTERN.finditer(text)]
    return {"claims": claims, "evs": evs, "refs": refs, "bare_numbers": bare}


def _claim_regions(text):
    """返回被 [[claim:ID]] ... [[/claim]] 包裹的值区间 [(start, end), ...]。"""
    regions = []
    for m in CLAIM_PATTERN.finditer(text):
        close = text.find("[[/claim]]", m.end())
        if close != -1:
            regions.append((m.end(), close))
    return regions


def _inside_any_channel(text, span, regions):
    """判断数字 span 是否落在某个 claim 值区间内（豁免裸数字报告）。"""
    start, end = span
    return any(rs <= start and end <= re for rs, re in regions)


# ---- 四个离线确定性校验脚本（供 CLI/门禁挂载点调用） ----

def _load_text(path):
    return Path(path).read_text(encoding="utf-8")


def audit_claims(manuscript_path, store):
    """扫描稿件中未绑定的 claim（含未注册 + 已注册但未绑定证据）。"""
    parsed = parse_manuscript(_load_text(manuscript_path))
    claims = store.registry["claims"]
    unbound = []
    for c in parsed["claims"]:
        claim = claims.get(c["claim_id"])
        if claim is None:
            unbound.append({"claim_id": c["claim_id"], "issue": "claim 未注册"})
        elif claim["status"] != BOUND:
            unbound.append({"claim_id": c["claim_id"], "issue": "claim 未绑定证据"})
    return {
        "referenced_claims": sorted({c["claim_id"] for c in parsed["claims"]}),
        "unbound_claims": unbound,
        "bare_numbers_count": len(parsed["bare_numbers"]),
        "count": len(unbound),
    }


def check_consistency(manuscript_path, store):
    """检查稿件引用到的 claim 是否已绑定证据（一致）。"""
    parsed = parse_manuscript(_load_text(manuscript_path))
    issues = []
    for c in parsed["claims"]:
        claim = store.registry["claims"].get(c["claim_id"])
        if claim is None:
            issues.append({"claim_id": c["claim_id"], "issue": "claim 未注册"})
        elif claim["status"] != BOUND:
            issues.append({"claim_id": c["claim_id"], "issue": "claim 未绑定证据"})
        else:
            verified = store.verify_claim_binding(c["claim_id"])
            issues += [{"claim_id": c["claim_id"], "issue": issue}
                       for issue in verified["issues"]]
    return {"issues": issues, "count": len(issues)}


def check_references(manuscript_path, store):
    """检查 [[ref:]] 引用是否全部登记为已核验（Real），并给出正文支持等级。"""

    def _real(ref_id):
        catalog = store.registry.get("references", {})
        entry = catalog.get(ref_id)
        if entry is None or entry.get("status") != "real":
            return False
        metadata_path = Path(entry.get("metadata_artifact") or "")
        if (not metadata_path.is_file()
                or file_hash(metadata_path) != entry.get("metadata_sha256")):
            return False
        if entry.get("support_level") in {"full", "partial"}:
            path = Path(entry.get("artifact_id") or "")
            return path.is_file() and file_hash(path) == entry.get("sha256")
        return True

    def _support(ref_id):
        entry = (store.registry.get("references", {}) or {}).get(ref_id) or {}
        return entry.get("support_level", "unavailable")

    parsed = parse_manuscript(_load_text(manuscript_path))
    unresolved = [r["reference_id"] for r in parsed["refs"] if not _real(r["reference_id"])]
    supports = [{"reference_id": r["reference_id"], "support_level": _support(r["reference_id"])}
                for r in parsed["refs"]]
    return {"total": len(parsed["refs"]), "unresolved": unresolved, "count": len(unresolved),
            "supports": supports}


def lint_manuscript(manuscript_path, store):
    """综合文档 lint：无裸数字、claim 全绑定、引用可解析。"""
    text = _load_text(manuscript_path)
    parsed = parse_manuscript(text)
    regions = _claim_regions(text)
    problems = []
    for bare in parsed["bare_numbers"]:
        # 已存在于 [[claim:...]][[值]][[/claim]] 值区间内的裸数字免检，避免误报。
        if _inside_any_channel(text, bare["pos"], regions):
            continue
        problems.append({"type": "裸数字", "text": bare["text"]})
    problems += [{"type": "未绑定 claim", "text": c["claim_id"]}
                 for c in audit_claims(manuscript_path, store)["unbound_claims"]]
    problems += [{"type": "引用不可解析", "text": r}
                 for r in check_references(manuscript_path, store)["unresolved"]]
    return {"passed": len(problems) == 0, "problems": problems, "count": len(problems)}


def _now():
    return datetime.now(timezone.utc).isoformat()

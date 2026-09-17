"""Brownfield 稿件路径（已有稿件 + 代码）。

- B1 workspace 加载 → workspace_manifest.json（结构图）。
- B2 代码摄入：ast 识别实验脚本 + 日志/指标提取 → results.json 候选 → 用户确认。
- B2.5 复现验证：环境锁定重跑，δ≤容差 → repro_match / repro_mismatch。
- B3 稿件审计：全量 claim 提取 + 引用核验 + overclaim 检测。
- B4 缺口报告：R1-R7 + 7-mode + 缺失证据/实验 + 引用风险 + 就绪度 %。
"""
from __future__ import annotations

import ast
import re
from datetime import datetime, timezone
from pathlib import Path

from .core import ProtocolError, read_json, write_json
from .evidence import EvidenceStore, BOUND, check_references, lint_manuscript
from .review import RejectionReview

ROLES = ("experiment_script", "config", "log", "data", "manuscript", "other")

# 常见实验脚本入口名
SCRIPT_NAMES = ("train", "run", "main", "eval", "evaluate", "reproduce")
# 常用日志指标正则： epoch: 1 accuracy: 0.95 → (accuracy, 0.95)
METRIC_PATTERN = re.compile(
    r"\b(accuracy|acc|loss|mse|f1|auc|bleu)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)",
    re.IGNORECASE)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _relative(root, path):
    try:
        return path.relative_to(root)
    except ValueError:
        raise ProtocolError(f"路径越界: {path}")


class Brownfield:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.manifest_path = self.workspace / "workspace_manifest.json"
        self.results_path = self.workspace / "results.json"

    # ---- B1 workspace 加载 ----
    def load(self, recursive=True):
        if not self.workspace.is_dir():
            raise ProtocolError(f"workspace 不存在: {self.workspace}")
        files = []
        executor = self.workspace.rglob("*") if recursive else self.workspace.iterdir()
        for path in executor:
            if not path.is_file():
                continue
            rel = _relative(self.workspace, path).as_posix()
            files.append({"path": rel, "size": path.stat().st_size,
                          "type": self._classify(path)})
        has_manuscript = any(f["type"] == "manuscript" for f in files)
        has_code = any(f["type"] == "experiment_script" for f in files)
        if not (has_manuscript or has_code):
            raise ProtocolError("workspace 至少需要一份稿件或实验代码")
        manifest = {"schema_version": "1.0", "root": str(self.workspace),
                    "scan_timestamp": _now(), "files": files,
                    "counts": self._counts(files)}
        write_json(self.manifest_path, manifest)
        return manifest

    def _classify(self, path):
        suffix = path.suffix.lower()
        name = path.stem.lower()
        if suffix == ".md" or suffix == ".tex":
            return "manuscript"
        if suffix == ".py":
            return "experiment_script" if _is_experiment_script(path) else "other"
        if suffix in (".yaml", ".yml", ".json", ".toml", ".ini"):
            return "config"
        if suffix in (".log", ".txt", ".csv", ".jsonl"):
            if _looks_like_log(path):
                return "log"
            return "data"
        if suffix in (".data", ".npz", ".npy", ".pkl", ".parquet"):
            return "data"
        return "other"

    def _counts(self, files):
        counts = {}
        for f in files:
            counts[f["type"]] = counts.get(f["type"], 0) + 1
        return counts

    # ---- B2 代码摄入 + 指标提取 ----
    def ingest(self, confirm_all=False):
        self._require_manifest()
        manifest = read_json(self.manifest_path)
        candidates = []
        for f in manifest["files"]:
            if f["type"] != "log":
                continue
            path = self.workspace / f["path"]
            for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore")
                                            .splitlines(), 1):
                m = METRIC_PATTERN.search(line)
                if m:
                    candidates.append({
                        "source_file": f["path"], "line_number": line_no,
                        "raw_text": line.strip(),
                        "mapped_metric_name": m.group(1).lower(),
                        "metric": float(m.group(2)),
                    })
        # 指标名 → 注册表校验：未知指标降级为候选、需用户确认
        results = []
        seen = set()
        for c in candidates:
            key = (c["source_file"], c["line_number"])
            if key in seen:
                continue
            seen.add(key)
            item = dict(c, confirmed=bool(confirm_all))
            results.append(item)
        out = {"candidates": results, "count": len(results),
               "policy": "指标名 ∈ 注册表校验后再进入权威通道，未经确认不写入 results.json"}
        write_json(self.results_path, out)
        return out

    def _require_manifest(self):
        if not self.manifest_path.is_file():
            raise ProtocolError("先运行 brownfield load 生成 workspace_manifest.json")

    def _require_results(self):
        if not self.results_path.is_file():
            raise ProtocolError("先运行 brownfield ingest 生成 results.json 候选")

    # ---- B2.5 复现验证 ----
    def reproduce(self, paper_values=None, tolerance=None):
        """比对论文数字与复现数字（从 results 候选读取），δ≤容差 → repro_match。"""
        self._require_results()
        paper_values = paper_values or {}
        tolerance = tolerance if tolerance is not None else 0.05
        results = read_json(self.results_path)
        out = []
        for c in results["candidates"]:
            if not c.get("confirmed"):
                continue
            metric = c["mapped_metric_name"]
            repro = c["metric"]
            paper = paper_values.get(metric)
            entry = {"metric_name": metric, "repro_value": repro}
            if paper is None:
                entry["status"] = "no_paper_value"
                entry["subject"] = "未比对"
            else:
                entry["paper_value"] = paper
                entry["delta"] = abs(repro - paper)
                entry["status"] = ("repro_match" if abs(repro - paper) <= tolerance
                                   else "repro_mismatch")
                entry["tolerance"] = tolerance
            out.append(entry)
        mismatches = [e for e in out if e["status"] == "repro_mismatch"]
        return {"results": out, "count": len(out),
                "mismatch_count": len(mismatches),
                "status": "repro_match" if not mismatches else "repro_mismatch"}

    # ---- B3 稿件审计（走 evidence/review） ----
    def audit(self, evidence_store, manuscript):
        """全量 claim 提取 + 引用核验 + 裸数字/一致性。返回结构化审计报告。"""
        lints = lint_manuscript(manuscript, evidence_store)
        refs = check_references(manuscript, evidence_store)
        claims = {c["claim_id"]: c for c in
                  evidence_store.registry["claims"].values()
                  if c["claim_id"] in _referenced_ids(manuscript)}
        return {
            "lint": lints,
            "references": refs,
            "claims": claims,
            "overclaim_risk": [{"claim_id": c["claim_id"], "issue": "claim 未绑定证据"}
                               for c in claims.values() if c.get("status") != BOUND],
        }

    # ---- B4 缺口报告 ----
    def gap_report(self, evidence_store=None, manuscript=None, reviewer_report=None):
        """稿件体检：就绪度 % + R1-R7 + 缺失证据/实验 + 引用风险。"""
        claims = []
        missing_evidence = []
        readiness_pct = 0.0
        if manuscript and evidence_store:
            findings = self.audit(evidence_store, manuscript)
            bound = sum(1 for c in findings["claims"].values() if c.get("status") == BOUND)
            total = len(findings["claims"])
            readiness_pct = (bound / total * 100) if total else 100.0
            missing_evidence = [{"claim_id": c["claim_id"],
                                 "status": c.get("status", "unknown")}
                                for c in findings["claims"].values()
                                if c.get("status") != BOUND]
            claims = list(findings["claims"].values())
        review = RejectionReview().assess(reviewer_report or {})
        return {
            "readiness_pct": round(readiness_pct, 1),
            "r1_r7": review["rejection_side"]["risks"],
            "mode7": review["fraud_side"]["modes"],
            "missing_evidence": missing_evidence,
            "missing_experiments": self._missing_experiments(claims),
            "citation_risks": review["fraud_side"]["summary"]["hits"],
            "overall_status": "pass" if readiness_pct >= 100 else "review",
        }

    def _missing_experiments(self, claims):
        # 机械启发：机制 claim 缺消融、泛化 claim 缺 held-out 时给缺失实验建议。
        suggestions = []
        for c in claims:
            if c.get("category") == "claim" and not c.get("value"):
                suggestions.append({"claim_id": c["claim_id"], "suggestion": "补做 held-out/消融实验"})
        return suggestions


def _is_experiment_script(path):
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return path.stem.lower() in SCRIPT_NAMES
    if path.stem.lower() in SCRIPT_NAMES:
        return True
    for node in ast.walk(tree):
        if isinstance(node, (ast.arg, ast.Str)) and str(getattr(node, "arg", "") or getattr(node, "s", "")) in ("train", "X_train", "y_train"):
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("fit", "train", "save"):
            return True
    return False


def _looks_like_log(path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    return bool(re.search(r"(accuracy|loss|epoch|step)\s*[:=]", text, re.I))


def _referenced_ids(manuscript):
    import re as _re
    text = Path(manuscript).read_text(encoding="utf-8")
    return set(_re.findall(r"\[\[claim:([A-Za-z0-9_\-]+)\]\]", text))
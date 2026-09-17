"""拒稿风险预检：R1–R7（拒稿侧）+ ARS 7-mode（造假侧）。

- R1 是 provisional 而非二值；R6/R7 机器产证据、用户裁决。
- 机械可消解 R1–R5；R6/R7 输出需用户裁决标记。
- 输出两栏报告（拒稿侧 + 造假侧），逐条 pass/warn/fail + 具体原因。
"""
from __future__ import annotations

from pathlib import Path

from .core import ProtocolError, read_json
from .evidence import EvidenceStore, check_references, lint_manuscript

# 拒稿侧（R1-R7）
RISK_IDS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")

# R1-R7 中文标签：后端单一定义，前端不再硬编码或回退。
RISK_NAMES = {
    "R1": "无 novelty（反证未覆盖）",
    "R2": "incremental / 增量过小",
    "R3": "无消融 / 机制不可证",
    "R4": "overclaim / 过度声明",
    "R5": "metric gaming / 指标博弈",
    "R6": "泛化性弱",
    "R7": "问题不重要 / novelty 撞车",
}

# 评审门禁状态→中文标签：后端单一下发，前端不再硬编码或回退。
GATE_STATUS_LABELS = {
    "pass": "PASS",
    "warn": "WARN",
    "fail": "FAIL",
}

# 造假侧 7-mode（源自 Lu et al., Nature 651:914-919）
MODE7 = (
    "implementation_bug",        # bug
    "hallucinated_citation",      # 幻觉引用
    "hallucinated_result",        # 幻觉结果
    "shortcut_dependence",        # 捷径依赖
    "bug_as_insight",             # bug-as-insight
    "methods_fabrication",        # Methods 造假
    "frame_lock",                 # frame-lock（只挑有利解释）
)

# 造假侧 7-mode 中文标签：后端单一定义，前端不再硬编码或回退。
MODE7_NAMES = {
    "implementation_bug": "实现 bug",
    "hallucinated_citation": "幻觉引用",
    "hallucinated_result": "幻觉结果",
    "shortcut_dependence": "捷径依赖",
    "bug_as_insight": "bug-as-insight",
    "methods_fabrication": "Methods 造假",
    "frame_lock": "frame-lock",
}


class RejectionReview:
    """对一篇稿件 / 一个 claim 运行 R1-R7 + 7-mode 预检。

    evidence: EvidenceStore（提供 claim 绑定 / 引用 Real / lint）
    mechanisms: {机制名: 绑定消融/反事实证据 id} 用于 R3。
    reversal: 是否有反向文献覆盖声明用于 R1。
    """

    def __init__(self, evidence=None, manuscript_path=None):
        self.evidence = evidence
        self.manuscript_path = Path(manuscript_path) if manuscript_path else None

    # ---------- 拒稿侧 R1-R7 ----------
    def assess(self, report=None):
        report = report or {}
        results = {
            "R1": self._r1(report), "R2": self._r2(report),
            "R3": self._r3(report), "R4": self._r4(report),
            "R5": self._r5(report), "R6": self._r6(report),
            "R7": self._r7(report),
        }
        return {
            "rejection_side": {"risks": results, "summary": self._summary(results)},
            "fraud_side": {"modes": self._mode7(report), "summary": self._mode7_summary(report)},
        }

    def _r1(self, report):
        # R1 无 novelty：R1 是 provisional 而非二值
        covered = bool(report.get("prior_covered"))
        coverage_statement = report.get("coverage_statement")
        if not coverage_statement:
            return self._entry("R1", "warn", "缺少穷尽式覆盖声明，novelty 判 provisional",
                               provisional=True)
        if covered:
            return self._entry("R1", "fail", "已存在覆盖 → provisional 高", provisional=True)
        return self._entry("R1", "pass", "未发现覆盖；novelty 仍为 provisional", provisional=True)

    def _r2(self, report):
        # R2 incremental：非增量 delta 或 delta 越阈值且机制可证
        delta = report.get("delta")
        threshold = report.get("min_improvement")
        if delta is None or threshold is None:
            return self._entry("R2", "warn", "缺少 delta/阈值")
        if delta > 0 and delta >= threshold:
            return self._entry("R2", "pass", f"delta {delta} ≥ 阈值 {threshold}")
        return self._entry("R2", "fail", "增量过小，未越阈值 → R2 风险")

    def _r3(self, report):
        # R3 无消融/机制不可证：每个机制 claim 绑定消融/反事实
        mechanism_claims = report.get("mechanisms", [])
        if not mechanism_claims:
            return self._entry("R3", "warn", "无机制声明（降级现象描述）")
        missing = [m for m in mechanism_claims if not m.get("ablation")]
        if missing:
            return self._entry("R3", "fail", f"机制未绑定消融: {[m['name'] for m in missing]}")
        return self._entry("R3", "pass", "机制均绑定消融/反事实")

    def _r4(self, report):
        # R4 overclaim：L1 contradiction + 证据方向一致
        contradiction = report.get("contradiction")
        evidence_consistent = report.get("evidence_consistent", True)
        if contradiction:
            return self._entry("R4", "fail", "注入矛盾：证据方向不一致 → overclaim")
        if not evidence_consistent:
            return self._entry("R4", "fail", "结论方向与证据方向不一致")
        return self._entry("R4", "pass", "无矛盾，结论与证据方向一致")

    def _r5(self, report):
        # R5 metric gaming：预注册 + 重采样 + 密封测试集
        preregistered = bool(report.get("preregistered"))
        resampled = bool(report.get("resampled"))
        sealed_test = bool(report.get("sealed_test"))
        if not (preregistered and resampled and sealed_test):
            return self._entry("R5", "warn", "需要 预注册+重采样+密封测试集 三要素")
        return self._entry("R5", "pass", "预注册+重采样+密封测试集齐备")

    def _r6(self, report):
        # R6 泛化弱：held-out 可证伪预测，机器产证据、用户裁决
        falsifiable = bool(report.get("heldout_falsifiable"))
        verified = bool(report.get("heldout_verified"))
        if not falsifiable:
            return self._entry("R6", "warn", "缺 held-out 可证伪预测", user_adjudication=True)
        if verified:
            return self._entry("R6", "pass", "held-out 验证通过, 用户裁决", user_adjudication=True)
        return self._entry("R6", "fail", "held-out 未通过, 用户裁决", user_adjudication=True)

    def _r7(self, report):
        # R7 问题不重要：importance 前瞻信号 + 反共识信号；机器产证据用户裁决
        importance = report.get("importance")
        if importance is None:
            return self._entry("R7", "warn", "缺 importance 前瞻信号, 用户裁决", user_adjudication=True)
        if importance.get("foresight") and importance.get("counter_consensus"):
            return self._entry("R7", "pass", "有前瞻+反共识信号, 用户裁决", user_adjudication=True)
        if importance.get("foresight"):
            return self._entry("R7", "warn", "仅前瞻信号, 无反共识, 用户裁决", user_adjudication=True)
        return self._entry("R7", "fail", "缺前瞻信号不得高分, 用户裁决", user_adjudication=True)

    def _entry(self, rid, status, reason, provisional=False, user_adjudication=False):
        entry = {"risk_id": rid, "status": status, "reason": reason}
        if provisional:
            entry["provisional"] = True  # R1 永不二值
        if user_adjudication:
            entry["user_adjudication"] = True  # R6/R7 用户裁决
        return entry

    def _summary(self, results):
        counts = {"pass": 0, "warn": 0, "fail": 0}
        for r in results.values():
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        has_fatal = any(r["status"] == "fail" and not r.get("user_adjudication")
                        for r in results.values())
        return {"counts": counts, "status": "fail" if has_fatal else "review"}

    # ---------- 造假侧 7-mode ----------
    def _mode7(self, report):
        evidence_flags = report.get("mode7") or {}
        return [
            {"mode": mode, "status": "hit" if evidence_flags.get(mode) else "clean",
             "evidence": evidence_flags.get(mode) or None}
            for mode in MODE7
        ]

    def _mode7_summary(self, report):
        flags = report.get("mode7") or {}
        hits = [m for m in MODE7 if flags.get(m)]
        return {"count": len(hits), "hits": hits,
                "status": "fail" if hits else "pass"}

    # ---------- 稿件级集成（读 evidence/稿件文件） ----------
    def from_manuscript(self, manuscript_path=None, report=None):
        if self.manuscript_path is None and manuscript_path is None:
            raise ProtocolError("缺少稿件路径")
        path = Path(manuscript_path or self.manuscript_path)
        if self.evidence:
            lint = lint_manuscript(path, self.evidence)
            refs = check_references(path, self.evidence)
            report = report or {}
            report.setdefault("evidence_consistent", True)
            if not lint["passed"]:
                report["mode7"] = dict(report.get("mode7") or {})
                # 未绑定的裸数字 / 未绑定 claim 归入造假侧风险（证据链断裂）
                for p in lint["problems"]:
                    report["mode7"]["hallucinated_result"] = report["mode7"].get(
                        "hallucinated_result") or f"稿件存在 {p['type']}: {p['text']}"
            if refs["unresolved"]:
                report["mode7"] = dict(report.get("mode7") or {})
                report["mode7"]["hallucinated_citation"] = \
                    f"未解析引用: {refs['unresolved']}"
        return self.assess(report)
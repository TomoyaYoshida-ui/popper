"""评测验收层 · Task 12（M4 验收阶段）。

确定性验收期物：机械复现（A4a）、消融回归（记录型，不重跑模型）、
综合验收报告（A4a / A4b / A2 / 可复现包 / 语料·门禁就绪度）。

- 全部基于已落库的真实状态 / 已保存预测重算；不调用外部 LLM，不伪造结果。
- 项目未完成 / 字段缺失时明确标 skipped / not_available，不抛错。
- LLM 相关步骤未执行（需 key）。无第三方依赖，仅标准库。
"""
from __future__ import annotations

from pathlib import Path

from .core import Experiment, ProtocolError

ABLATION_FEATURES = ("search", "gate", "replay")

# 语料 / 门禁就绪度检查的项目内成分（存在即就绪）。
READINESS_INGREDIENTS = (
    "spec",              # experiment.json
    "state_machine",     # .popper/state.db
    "metrics_registry",  # metrics.json
    "evidence_store",    # claims.csv.json
    "corpus_records",    # records.json
    "gate_context",      # gate_context.json
)


class Evaluator:
    """确定性验收评估器。构造可传 project_dir，也可在方法级显式传入。"""

    def __init__(self, project_dir=None):
        self.project_dir = Path(project_dir).resolve() if project_dir else None

    # -- 参数解析（兼容 CLI 显式传参 与 测试期物构造传参） -------------
    def _parse_project(self, project_dir):
        if project_dir is None:
            if self.project_dir is None:
                raise ProtocolError("缺少项目目录")
            return self.project_dir
        return Path(project_dir).resolve()

    # -- A4a 机械复现 ----------------------------------------------------
    def replay_check(self, project_dir=None):
        """离线 replay 重算（复用 Experiment().replay()），不重跑代码 / LLM。

        已完成项目返回 delta_consistent；未完成 / 未初始化返回 skipped 不抛错。
        """
        root = self._parse_project(project_dir)
        if not (root / ".popper" / "state.db").is_file():
            return {"status": "skipped", "reason": "项目未初始化（无 .popper/state.db）"}
        exp = Experiment(root)
        try:
            state = exp.state()
            if state["phase"] != "completed":
                return {"status": "skipped",
                        "reason": f"项目未完成（阶段：{state['phase']}）；需 freeze+confirm 至 completed"}
            try:
                result = exp.replay()
            except ProtocolError as error:
                return {"status": "fail", "reason": str(error), "delta_consistent": False}
        finally:
            exp.close()
        return {"status": "pass", "runs_recomputed": result["runs_recomputed"],
                "delta_consistent": bool(result.get("claim_recomputed"))}

    # -- 消融回归（记录消融影响，不重跑模型） ----------------------------
    def ablation(self, project_dir=None, remove=None):
        """返回 {feature, claim_produced, affected} 结构化消融记录。

        - search：探索非基线候选的关键。移除后无非基线候选，无法 freeze 出改进声明。
        - gate：验收 / 质量门，不产出 claim；移除只影响验收完整性，不改变 claim 本体。
        - replay：离线证据核验，不产出 claim；移除影响 claim 的可复现核验。
        """
        if remove is None:
            remove = project_dir
            project_dir = None
        if remove not in ABLATION_FEATURES:
            raise ProtocolError(f"remove 必须是 {'/'.join(ABLATION_FEATURES)} 之一")
        root = self._parse_project(project_dir)
        if not (root / ".popper" / "state.db").is_file():
            return {"feature": remove, "claim_produced": False, "affected": False,
                    "reason": "项目未初始化"}
        exp = Experiment(root)
        try:
            state = exp.state()
        finally:
            exp.close()
        claim = state.get("claim")
        has_claim = bool(claim)
        spec = state["spec"]
        selected = state.get("selected")
        # 非基线候选只可能在 search 探索后出现，freeze 又必然依赖它。
        search_needed = selected is not None and selected != spec["baseline"]
        if remove == "search":
            return {"feature": "search", "claim_produced": False, "affected": bool(search_needed)}
        if remove == "replay":
            return {"feature": "replay", "claim_produced": has_claim, "affected": has_claim}
        return {"feature": "gate", "claim_produced": has_claim,
                "affected": has_claim and (root / "gate_context.json").is_file()}

    # -- 综合验收报告 ----------------------------------------------------
    def acceptance_report(self, project_dir=None):
        """汇总 A4a / A4b / A2 / 可复现包存在性 / 语料·门禁就绪度。"""
        root = self._parse_project(project_dir)
        return {
            "project": str(root),
            "a4a": self.replay_check(root),
            "a4b": self._a4b(root),
            "a2": self._a2(root),
            "reproducible_package": self._reproducible_package(root),
            "corpus_gate_readiness": self._readiness(root),
        }

    # -- A4b：多划分多种子方向一致性 ------------------------------------
    def _a4b(self, root):
        if not (root / ".popper" / "state.db").is_file():
            return "not_available"
        exp = Experiment(root)
        try:
            state = exp.state()
            results = exp.results()
        finally:
            exp.close()
        if state["phase"] not in {"frozen", "completed"} or not state.get("selected"):
            return "not_available"
        cons = {}
        for split in ("dev", "test"):
            rows = [r for r in results if r["split"] == split]
            cons[split] = self._split_direction(rows, state)
        have = {s for s, c in cons.items() if c}
        if not have:
            return "not_available"
        return {
            "n_splits_evaluated": len(have),
            "overall_consistent": all(cons[s]["all_consistent"] for s in have),
            "per_split": {s: cons[s] for s in cons if cons[s]},
        }

    @staticmethod
    def _split_direction(rows, state):
        """多种子下候选相对基线方向一致性（定向 delta>0 的种子占比）。"""
        if not rows:
            return None
        direction = state["spec"]["metric"]["direction"]
        baseline = state["spec"]["baseline"]
        selected = state.get("selected")
        base = next((r for r in rows if r["config"] == baseline), None)
        cand = next((r for r in rows if r["config"] == selected), None)
        if base is None or cand is None:
            return None
        bs = {p["seed"]: p["value"] for p in base["per_seed"]}
        cs = {p["seed"]: p["value"] for p in cand["per_seed"]}
        common = [s for s in cs if s in bs]
        if not common:
            return None
        sign = 1.0 if direction == "max" else -1.0
        oriented = [(cs[s] - bs[s]) * sign for s in common]
        consistent = sum(1 for value in oriented if value > 0)
        return {"n_seeds": len(common),
                "all_consistent": consistent == len(oriented),
                "proportion_consistent": consistent / len(common)}

    # -- A2：闭环有效性（AC-3，rubric） -----------------------------------
    # 维度="闭环闭包 vs 同预算单次的独立复现改进数"。
    # 真实 A2 需要 AutoEP headroom（闭环 vs 同预算单次）的独立复现数据；
    # 本环境无此算力数据 => 诚实标 not_available，不伪造计数。
    # 提供 closed_loop()：调用方可传入真实测得的"闭环改进数 / 单次改进数"，
    # 由本方法做确定性比较并给出 rubric 分数，缺失即 not_available。
    def closed_loop(self, closed_wins=None, single_wins=None):
        """A2 rubric 确定性评分器。缺数据时如实 not_available。"""
        if closed_wins is None or single_wins is None:
            return {"status": "not_available",
                    "reason": "缺少闭环 vs 单次的独立复现改进数（需 AutoEP headroom 数据），不伪造"}
        if closed_wins < 0 or single_wins < 0:
            return {"status": "not_available", "reason": "改进数为负非法"}
        # rubric: 1=闭环<单次 3=闭环≈单次 5=闭环>单次；阈值>=4（闭环>单次）
        if closed_wins > single_wins:
            score = 5
        elif closed_wins == single_wins:
            score = 3
        else:
            score = 1
        return {"status": "measured", "closed_wins": closed_wins,
                "single_wins": single_wins, "scope": "closed_loop > single",
                "rubric_score": score, "passed": score >= 4,
                "note": "评分基于传入的真实改进数，超出即视为未满足"}

    def _a2(self, root):
        # 保留状态标志视图，但明确标注 A2（闭环有效性）本身需 closed_loop()。
        if not (root / ".popper" / "state.db").is_file():
            return "not_available"
        exp = Experiment(root)
        try:
            state = exp.state()
        finally:
            exp.close()
        phase = state["phase"]
        return {"phase": phase,
                "preregistered": bool(state.get("selected")),
                "frozen": phase in {"frozen", "completed", "confirmation_failed", "confirming"},
                "closed_loop_advantage": "not_available_reason_autoep_data",
                "note": "该字段不度量 A2 维度；A2 闭环有效性需 closed_loop() 传入真实改进数"}

    # -- 可复现包存在性 --------------------------------------------------
    @staticmethod
    def _reproducible_package(root):
        matches = list(root.rglob("reproducibility.zip"))
        if matches:
            return {"present": True, "path": str(matches[0])}
        if (root / ".popper" / "state.db").is_file():
            return {"present": False, "note": "项目已初始化，但尚未物化复现包（materialize repro）"}
        return "not_available"

    # -- 语料 / 门禁就绪度占比 -------------------------------------------
    @classmethod
    def _readiness(cls, root):
        probes = {
            "spec": root / "experiment.json",
            "state_machine": root / ".popper" / "state.db",
            "metrics_registry": root / "metrics.json",
            "evidence_store": root / "claims.csv.json",
            "corpus_records": root / "records.json",
            "gate_context": root / "gate_context.json",
        }
        ready = sorted(name for name, path in probes.items() if path.is_file())
        return {"ratio": round(len(ready) / len(probes), 2), "ready": ready,
                "missing": sorted(set(probes) - set(ready)),
                "note": "语料目录为独立空间（corpus init），不驻留于实验项目；此处按项目内就绪成分计占比"}
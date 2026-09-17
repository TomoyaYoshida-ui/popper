"""门禁系统（L0 确定性 + L1 语义+统计）。

- 门禁挂载点 = claim/artifact 级检查点。
- L0：schema / claim 全命中 / 指标名∈注册表 / 引用全 Real / 无裸数字 / 进程环境 / 7-mode 确定性信号。
- L1：① 语义解析（仅此步 LLM）→ 三元组；② 方向+显著性（bootstrap/CV 主判据，多种子 sanity）；③ 预注册表（冻结后改动需审批）；④ 流转 passed/retry/human_review（retry≤2）。
- 7-mode 语义信号（surprisingly 无反向文献拦 / 消融对象≠机制拦 / in hindsight 提示回选题）。
"""
from __future__ import annotations

import math
import random
from pathlib import Path

from .core import ProtocolError, read_json
from .domains.protocol import (REPEAT_ANALYSIS_UNIT, REPEAT_TRAIN_SEED, REPEAT_UNITS)
from .evidence import (MetricsRegistry, run_checks,
                       check_references)

# L0/L1 流转状态
PASSED, RETRY, HUMAN_REVIEW = "passed", "retry", "human_review"
RETRY_LIMIT = 2

VALID_INPUTS = ("text", "embedded", "image")


class GateError(ProtocolError):
    pass


class _State:
    """门禁检查内部状态（retry 计数）。"""

    def __init__(self):
        self.retry_used = 0
        self.results = {}

    def can_retry(self):
        return self.retry_used < RETRY_LIMIT

    def grant_retry(self):
        self.retry_used += 1


class Gate:
    """挂载点门禁。按 /ARTIFACT:CHECKPOINT/ 命名。

    gate.path 如 "paper.json:L0" / "results.json:metric_registry"。
    """

    def __init__(self, name, mount="artifact"):
        self.name = name
        self.mount = mount  # claim | artifact

    def __call__(self, context, state=None):
        raise NotImplementedError


# ---- L0 确定性门禁（不依赖 LLM） ----

def l0_process_environment(context):
    """进程/环境：唯一 cell/session 签名，防二次重放。"""
    signature = context.get("environment_signature")
    expected = context.get("expected_process")
    if expected and signature and signature.get("pid") != expected:
        return {"passed": False, "reason": f"进程签名不匹配: {signature} vs {expected}"}
    return {"passed": True, "checks": "进程/环境一致"}


def l0_schema(context):
    """schema 校验（results.json / 稿件 JSON 结构）。"""
    data = context.get("payload")
    if not isinstance(data, dict):
        return {"passed": False, "reason": "payload 必须是对象"}
    required = context.get("required_fields", ())
    missing = [f for f in required if f not in data]
    if missing:
        return {"passed": False, "reason": f"缺少字段: {missing}"}
    return {"passed": True, "checks": f"{len(required)} 必需字段齐全"}


def l0_metric_registry(context):
    """指标名 ∈ metrics.json 注册表，否则 L0 失败。"""
    metric = context.get("metric_name")
    if metric is None:
        return {"passed": True, "reason": "无指标声明（跳过）"}
    registry = MetricsRegistry(context.get("registry_path"))
    try:
        registry.require_registered(metric)
    except ProtocolError as error:
        return {"passed": False, "reason": str(error)}
    return {"passed": True, "checks": f"指标已注册: {metric}"}


def l0_claim_and_bare_number(context):
    """claim 全命中 + 无裸数字（复用 evidence lint）。"""
    store = context.get("evidence_store")
    manuscript = context.get("manuscript_path")
    if not store or not manuscript:
        return {"passed": True, "reason": "无稿件输入（跳过）"}
    result = run_checks(manuscript, store)
    problems = result["lint"]["problems"]
    if problems:
        return {"passed": False, "problems": problems}
    return {"passed": True, "checks": "claim 全绑定、无裸数字"}


def l0_references_real(context):
    """[[ref:]] 全 Real。"""
    store = context.get("evidence_store")
    manuscript = context.get("manuscript_path")
    if not store or not manuscript:
        return {"passed": True, "reason": "无引用扫描（跳过）"}
    unresolved = check_references(manuscript, store)["unresolved"]
    if unresolved:
        return {"passed": False, "unresolved": unresolved}
    return {"passed": True, "checks": "引用全部 Real"}


_MODE7_DETERMINISTIC = ("bug", "hallucinated_result", "shortcut_dependence",
                        "bug_as_insight", "methods_fabrication", "frame_lock")


def l0_mode7_signals(context):
    """7-mode 确定性信号：根据注入的确定性标记检查。"""
    flags = context.get("mode7_flags") or {}
    hits = [k for k in _MODE7_DETERMINISTIC if flags.get(k)]
    if hits:
        return {"passed": False, "hits": hits, "reason": f"7-mode 确定性命中: {hits}"}
    return {"passed": True, "checks": "无 7-mode 确定性命中"}


# ---- L1 语义+统计门禁 ----

def _triplet(statement, semantic_llm=None, context=None):
    """语义解析（仅此步 LLM）→ (subject, relation, object)。无 LLM 时确定性退化。"""
    if semantic_llm:
        parsed = semantic_llm(statement, context or {})
        if not isinstance(parsed, dict) or not {"subject", "relation", "object"} <= parsed.keys():
            raise GateError("语义解析必须返回 subject/relation/object")
        return parsed
    return {"subject": "self", "relation": "claims", "object": statement}


def _bootstrap_direction(baseline, candidate, units, rng, n_iter=2000, direction="max"):
    """bootstrap 主判据：基于每重复单位的配对方向差值，返回方向一致性比例。

    ``units`` 是重复单位取值（训练种子 / 独立重复序号 / 分析单元 id）；差值在
    基线与候选共有的单位上配对。direction=min（误差等越小越好）时差值按
    候选−基线 取反后再判符号。
    """
    sign = 1.0 if direction == "max" else -1.0
    pairs = [(baseline.get(s), c) for s, c in
             ((s, candidate.get(s)) for s in units if s in candidate)
             if baseline.get(s) is not None]
    if not pairs:
        return None
    oriented = [(c - b) * sign for b, c in pairs]
    consistent = 0
    for _ in range(n_iter):
        sample = [rng.choice(oriented) for _ in range(len(oriented))]
        if sum(sample) > 0:
            consistent += 1
    return {"ratio": consistent / n_iter, "n_units": len(pairs)}


def claim_outcome(delta, min_improvement, stats, repeat_unit):
    """claim 结论：效应量过预注册阈值是必要条件。

    分析单元域包还要求 bootstrap 统计判据成立（``statistical_claim=True``）；
    训练种子/独立重复域包的统计永远只是描述性，结论只取决于效应量阈值。
    """
    meets_threshold = delta > 0 and delta >= min_improvement
    if repeat_unit == REPEAT_ANALYSIS_UNIT and not (stats or {}).get("statistical_claim"):
        meets_threshold = False
    return "supports_threshold" if meets_threshold else "insufficient_evidence"


def claim_stats(stats, repeat_unit):
    """从门禁统计结果投影出写入 claim 的 stats 块（键随重复单位种类变化）。"""
    if repeat_unit == REPEAT_ANALYSIS_UNIT:
        keys = ("p_direction_consistent", "bootstrap_n", "n_units", "descriptive")
    else:
        keys = ("p_direction_consistent", "bootstrap_n", "n_seeds", "descriptive")
    projected = {key: stats.get(key) for key in keys}
    projected["statistical_claim"] = bool(stats.get("statistical_claim"))
    projected["repeat_unit"] = repeat_unit
    return projected


def l1_significance(context, state, rng=None):
    """重复单位方向稳定性门禁；是否构成统计主张由 repeat_unit 决定。

    - direction 必须为 min/max；min 方向下候选更低才是改善。
    - ``train_seed`` / ``independent_run``：单元数少于 min_seeds_for_significance
      （默认 2）时只能描述性；即使方向全部一致，也只反映训练/重复随机性，
      ``statistical_claim`` 恒为 False。
    - ``analysis_unit``：对数据内独立分析单元（区块/群组/时间块）做配对 bootstrap，
      这是合法的单元层抽样不确定性程序。预注册的 significance_ratio（= 1−α 方向
      判据）与 min_units_for_significance 都满足时 statistical_claim=True，
      claim 结论才允许 supports_threshold。
    """
    baseline = context.get("baseline_per_seed") or {}
    candidate = context.get("candidate_per_seed") or {}
    units = context.get("seeds") or sorted(set(baseline) & set(candidate))
    rng = rng or random.Random(context.get("seed", 0))
    direction = context.get("direction", "max")
    if direction not in ("min", "max"):
        raise GateError(f"direction 必须为 min 或 max: {direction!r}")
    repeat_unit = context.get("repeat_unit", REPEAT_TRAIN_SEED)
    if repeat_unit not in REPEAT_UNITS:
        raise GateError(f"repeat_unit 必须属于 REPEAT_UNITS: {repeat_unit!r}")
    analysis = repeat_unit == REPEAT_ANALYSIS_UNIT
    measured = _bootstrap_direction(baseline, candidate, units, rng, direction=direction)
    threshold = context.get("significance_ratio", 0.5)
    min_units = int(context.get("min_units_for_significance",
                                context.get("min_seeds_for_significance", 2)))
    if measured is None:
        if analysis:
            reason = "缺少按分析单元的配对测量，无法计算单元层抽样不确定性（不能声称统计显著）"
        else:
            reason = "缺少多种子 per-seed 数据，无法检查训练稳定性（不能声称统计显著）"
        return {"passed": True, "descriptive": True, "statistical_claim": False,
                "sanity": reason}
    ratio, n_units = measured["ratio"], measured["n_units"]
    if n_units < min_units:
        if analysis:
            return {"passed": False, "descriptive": True, "statistical_claim": False,
                    "n_units": n_units,
                    "sanity": f"仅 {n_units} 个分析单元，不足以支撑单元层统计判断"
                              f"（需 ≥ {min_units}），结论降级为描述性"}
        return {"passed": True, "descriptive": True, "statistical_claim": False,
                "n_seeds": n_units,
                "sanity": f"仅 {n_units} 个种子，不足以支撑统计显著性判断（需 ≥ {min_units}），降级为描述性"}
    if analysis:
        statistical = ratio >= threshold
        result = {
            "passed": statistical,
            "p_direction_consistent": round(ratio, 4),
            "analysis_unit_direction_consistency": round(ratio, 4),
            "n_units": n_units,
            "bootstrap_n": 2000,
            "statistical_claim": statistical,
            "descriptive": not statistical,
        }
        if statistical:
            result["sanity"] = (f"{n_units} 个分析单元的自举方向一致性 {ratio:.3f} ≥ {threshold:g}，"
                                "满足预注册的单元层统计判据；不外推为机制或创新性证明")
        else:
            result["sanity"] = (f"{n_units} 个分析单元的自举方向一致性 {ratio:.3f} < {threshold:g}，"
                                "单元层证据不满足预注册判据，只能描述性报告")
        return result
    passed = ratio >= threshold
    result = {
        "passed": passed,
        "p_direction_consistent": round(ratio, 4),
        "training_seed_direction_consistency": round(ratio, 4),
        "n_seeds": n_units,
        "bootstrap_n": 2000,
        "statistical_claim": False,
        "descriptive": True,
    }
    if passed:
        result["sanity"] = (f"{n_units} 个训练种子的方向一致性 {ratio:.3f} ≥ {threshold}；"
                            "仅作为训练稳定性描述")
    else:
        result["sanity"] = f"方向一致性 {ratio:.3f} < {threshold}，建议描述性而非显著"
    return result


def l1_preregistered(context, state):
    """预注册表：冻结后改动需审批。"""
    claimed = context.get("claim_declaration")
    frozen = context.get("frozen_declaration")
    if frozen is not None and claimed is not None and claimed != frozen:
        return {"passed": False, "required_approval": True,
                "reason": "预注册表被改动，需用户审批", "changed": claimed, "frozen": frozen}
    return {"passed": True, "checks": "预注册一致"}


_MODE7_SEMANTIC_SIGNALS = ("no_reverse_literature", "ablation_mismatch", "in_hindsight")


def l1_mode7_semantic(context, state, semantic_llm=None):
    """7-mode 语义信号：无反向文献拦 / 消融对象≠机制拦 / in hindsight 提示回选题。"""
    statement = context.get("claim_statement")
    if not statement:
        return {"passed": True, "reason": "无声明（跳过语义 7-mode）"}
    signals = semantic_llm(statement, {"category": "mode7"}) if semantic_llm else {}
    hits = [s for s in _MODE7_SEMANTIC_SIGNALS if signals.get(s)]
    result = {"passed": len(hits) == 0, "signals": hits}
    if hits:
        result["reason"] = "、".join(hits) + " 命中"
    else:
        result["checks"] = "无 7-mode 语义命中"
    return result


# ---- 门禁流水线与流转状态机 ----

def l1_semantic(context, state, semantic_llm=None):
    """L1 语义解析→三元组；结构完整性即通过，矛盾由下游 significance 判定。"""
    reason = context.get("claim_statement") or context.get("claim_declaration")
    if not reason:
        return {"passed": True, "reason": "无声明（跳过语义解析）"}
    triplet = _triplet(reason, semantic_llm, context)
    return {"passed": True, "triplet": triplet, "checks": "语义三元组可解析"}


L0_CHECKS = {
    "schema": l0_schema,
    "metric_registry": l0_metric_registry,
    "claim_and_bare_number": l0_claim_and_bare_number,
    "references_real": l0_references_real,
    "process_environment": l0_process_environment,
    "mode7_signals": l0_mode7_signals,
}

L1_CHECKS = {
    "semantic_triplet": l1_semantic,
    "significance": l1_significance,
    "preregistered": l1_preregistered,
    "mode7_semantic": l1_mode7_semantic,
}


def run_l0(context):
    """运行全部 L0 检查。返回 {passed, results}。"""
    results = {}
    for name, check in L0_CHECKS.items():
        try:
            checks = context.get("enabled", None)
            if checks is not None and name not in checks:
                continue
            results[name] = check(context)
        except Exception as error:  # 确定性门禁异常即失败
            results[name] = {"passed": False, "reason": f"L0 执行异常: {error}"}
    all_pass = all(r["passed"] for r in results.values())
    return {"passed": all_pass, "results": results}


def run_l1(context, state=None, semantic_llm=None):
    """运行 L1 检查（语义+统计）。"""
    results = {}
    for name, check in L1_CHECKS.items():
        try:
            checks = context.get("enabled", None)
            if checks is not None and name not in checks:
                continue
            if name in ("semantic_triplet", "mode7_semantic"):
                results[name] = check(context, state, semantic_llm)
            else:
                results[name] = check(context, state)
        except Exception as error:
            results[name] = {"passed": False, "reason": f"L1 执行异常: {error}"}
    all_pass = all(r["passed"] for r in results.values())
    return {"passed": all_pass, "results": results}


def evaluate_gate(context, semantic_llm=None):
    """按挂载点评估门禁并驱动流转状态机 passed/retry/human_review。

    - L0 失败 => passed=False，直接 human_review（确定性且不可重试）。除非明确 allow_retry。
    - L0 通过、L1 失败 => 若 retry 预算未耗尽可 retry（state.retry_used），否则 human_review。
    """
    state = _State()
    l0 = run_l0(context)
    if not l0["passed"]:
        return {"status": HUMAN_REVIEW, "level": "L0", "detail": l0, "retry_used": 0}
    # L1 需 prereg + significance 关键检查才可 retry
    l1 = run_l1(context, state, semantic_llm)
    flow = dispatch(context, l1, state)
    decision = {"status": flow, "level": "L1", "detail": l1,
                "retry_used": state.retry_used, "retry_remaining": RETRY_LIMIT - state.retry_used}
    if flow == RETRY:
        decision["next_action"] = "修正声明或补多种子后重试"
    elif flow == HUMAN_REVIEW:
        decision["next_action"] = "人工审批（改动预注册或语义疑点）"
    return decision


def dispatch(context, l1_result, state):
    """L1 结果→流转：passed / retry / human_review（retry≤2）。"""
    if l1_result["passed"]:
        return PASSED
    retryable = (not l1_result.get("results", {}).get("preregistered", {}) or
                 l1_result.get("results", {}).get("preregistered", {}).get("passed", True))
    if retryable and state.can_retry():
        state.grant_retry()
        return RETRY
    return HUMAN_REVIEW


def read_gate_context(path):
    """从门禁 context JSON 文件读取。"""
    return read_json(Path(path))

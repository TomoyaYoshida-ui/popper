"""跨任务盲测三对照策略：FixedPlanPolicy、scripted_plan、模型调用限额。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from popper.core import ProtocolError
from popper.research.actions import RUN_EXPERIMENT, STOP, ActionProposal


COMPARATORS = ("fixed_registered_search", "same_model_same_tools_fixed_plan",
               "adaptive_research_controller")


class FixedPlanPolicy:
    """实验前冻结的有序研究计划；证据盲：choose/after_observation 均不看 delta。

    计划是候选 config 的有序列表。运行时按序选择下一个 untested 候选执行，
    计划耗尽即 STOP（永不对结果驱动地请求确认）。
    """

    name = "fixed_plan"
    plan_source = "scripted"

    def __init__(self, plan, *, plan_source="scripted"):
        self._plan = list(plan)
        self.plan_source = plan_source

    def _next_untested(self, context):
        for config in self._plan:
            for cand in context["candidates"]:
                if cand["config"] == config and cand["status"] == "untested":
                    return cand
        return None

    def choose(self, context):
        next_ = self._next_untested(context)
        if next_ is None:
            return ActionProposal(
                STOP, "固定计划已耗尽；按预注册顺序完成全部干预，不再追加。",
                alternatives=("扩大独立数据",), source=self.name)
        return ActionProposal(
            RUN_EXPERIMENT, f"固定计划按序执行下一个预注册候选 {next_['hypothesis_id']}。",
            next_["hypothesis_id"], alternatives=tuple(
                c["hypothesis_id"] for c in context["candidates"] if c["config"] != next_["config"]),
            source=self.name)

    def after_observation(self, hypothesis_id, delta, threshold, has_remaining):
        # 证据盲：忽略 delta/threshold。返回 RUN_EXPERIMENT 延续职责驱动的循环，
        # 让下一轮 choose() 按固定计划推进；计划耗尽时 choose() 才返回 STOP。
        return ActionProposal(
            RUN_EXPERIMENT, "固定计划不依据观察结果改变轨迹（证据盲对照）；继续按序推进。",
            hypothesis_id, alternatives=("结束固定计划",), source=self.name)


def scripted_plan(spec, candidates=None):
    """确定性脚本计划：按 experiment.json 注册顺序返回全部候选 config。"""
    return list(candidates or spec["candidates"])


def make_call_limiter(policy, *, logical_calls=4, http_attempts=8):
    """包装 policy._call，计数逻辑调用；物理尝试由 diagnostics 文件数估算。"""
    original_call = policy._call
    state = {"logical": 0}

    def bounded_call(system, payload):
        if state["logical"] >= logical_calls:
            raise ProtocolError(f"模型逻辑调用额度 {logical_calls} 已耗尽")
        state["logical"] += 1
        return original_call(system, payload)

    policy._call = bounded_call
    return state
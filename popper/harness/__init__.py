"""编码 Agent 接入注册表：Agent 提案，内核裁决。

新增一个 harness = 实现 ``base.Harness``（1 个文件）+ 在 ``_HARNESSES`` 加一项。
内核侧不需要任何改动：写入权、预算与覆盖门禁都在 ``ResearchController`` 与
``RevisionStore`` 手里，harness 只提供提案。
"""
from __future__ import annotations

from ..core import ProtocolError
from .base import Harness, PolicyHarness, parse_revision_proposal
from .json_agent import JSONAgentHarness

# 新增 harness = 实现 base.Harness + 在这里加一项（顺序即选择优先级）。
_HARNESSES = (JSONAgentHarness,)


def harnesses():
    """已注册的编码 Agent 接入实现。"""
    return _HARNESSES


def names():
    """可用的 harness 名称，供 CLI 选择与错误提示。"""
    return tuple(cls.name for cls in _HARNESSES)


def resolve(name):
    """按名称取 harness 类；需要端点配置的接入由调用方再实例化。"""
    for cls in _HARNESSES:
        if cls.name == name:
            return cls
    raise ProtocolError(f"未知的 harness: {name}（可用：{', '.join(names())}）")


def for_policy(policy):
    """把带 ``propose_revision`` 的策略适配成 harness；没有该能力时返回 None。"""
    if callable(getattr(policy, "propose_revision", None)):
        return PolicyHarness(policy)
    return None


__all__ = ["Harness", "JSONAgentHarness", "PolicyHarness", "for_policy", "harnesses",
           "names", "parse_revision_proposal", "resolve"]

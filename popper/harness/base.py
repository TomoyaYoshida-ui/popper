"""编码 Agent 的接入契约：Agent 提案，内核裁决。

- ``Harness`` 只承担**提案**职责：把 ``IMPLEMENT_REVISION`` / ``REPAIR_IMPLEMENTATION``
  要写入的编辑交回内核。落盘（``RevisionStore.create``）、预算预留与覆盖门禁仍由内核
  独占——harness 拿不到写入权，也不能自报结果，所以「换一个编码 Agent」不会扩大
  可信边界。
- 接入优先用官方 SDK / 结构化协议（JSON 契约），**不要**解析 Agent 的 stdout：
  正则抠取文本既是最脆弱的集成点，也让「提案」退化成一个不可校验的自由字符串。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..core import ProtocolError


def parse_revision_proposal(response):
    """把 Agent 的结构化响应校验成 ``{"edits": tuple[CodeEdit, ...], "rationale": str}``。

    这是提案进入内核的唯一形状：字段集合必须精确匹配；越界路径、空替换与语法错误由
    ``CodeEdit`` 拒绝。空 ``edits`` 是合法提案（表示已注册实现已满足冻结配置）。
    """
    # 延迟导入：harness 与 research 互为主要用户，模块级互引会成环。
    from ..research.revisions import CodeEdit

    if not isinstance(response, dict):
        raise ProtocolError("模型代码提案必须是 JSON 对象")
    rows = response.get("edits")
    rationale = response.get("rationale")
    if (not isinstance(rows, list) or len(rows) > 20
            or not isinstance(rationale, str) or not rationale.strip()):
        raise ProtocolError("模型代码提案缺少 edits 或 rationale")
    edits = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"path", "original_sha256", "replacement"}:
            raise ProtocolError("模型 CodeEdit 字段不正确")
        edits.append(CodeEdit(row["path"], row["replacement"], row["original_sha256"]))
    return {"edits": tuple(edits), "rationale": rationale.strip()}


@runtime_checkable
class Harness(Protocol):
    """编码 Agent 的接入契约。

    ``propose_revision`` 只返回**提案**：执行、计分与写入都不在契约内，仍由内核持有。
    """

    name: str

    def propose_revision(self, objective, hypothesis, config, code_files,
                         parent_revision=None, failure=None) -> dict:
        """针对冻结配置产出待写入的编辑与理由。"""


class PolicyHarness:
    """把「自带 ``propose_revision`` 的策略对象」适配成 Harness。

    提案职责一度直接挂在研究策略上；适配器让这类对象继续可用，只是把它们降级为
    harness 的一种——控制器不再需要知道提案者是谁。
    """

    def __init__(self, policy, name=None):
        self._policy = policy
        self.name = name or getattr(policy, "name", type(policy).__name__)

    @property
    def model(self):
        return getattr(self._policy, "model", None)

    def propose_revision(self, objective, hypothesis, config, code_files,
                         parent_revision=None, failure=None):
        # 每次调用都重新取属性：调用方在构造后替换 propose_revision 仍然生效。
        return self._policy.propose_revision(
            objective, hypothesis, config, code_files,
            parent_revision=parent_revision, failure=failure)

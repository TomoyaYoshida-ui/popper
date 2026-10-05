"""代码空间上的实验树搜索。

策略只决定下一个节点试什么：先起草若干初稿，有限次数修复失败叶子，
否则在当前独立评分最好的节点上改进。分数不由模型解析日志得出。
冒烟结果使用 dev:smoke，不能进入 Decision 或 Claim。
"""
from __future__ import annotations

import random

from ..core import ProtocolError, canonical, digest

CLAIM_SCOPES = frozenset({"dev", "confirmation"})
SMOKE_SCOPE = "dev:smoke"


def can_support_claim(scope):
    """低保真冒烟观察不能支撑结论。切片观察仍由原有 reflection 规则单独约束。"""
    return scope in CLAIM_SCOPES or (isinstance(scope, str) and scope.startswith("dev:slice:"))


def diff_fingerprint(edits):
    if not isinstance(edits, list) or not edits:
        raise ProtocolError("搜索节点必须给出代码修改")
    return digest({"edits": edits})


def choose_action(nodes, *, num_drafts=2, debug_prob=0.3, max_debug_depth=2, direction="min", rng=None):
    """返回下一个节点动作。nodes 只含已结束的 draft/debug/improve 记录。"""
    if type(num_drafts) is not int or num_drafts < 1:
        raise ProtocolError("初稿数量必须是正整数")
    if not 0 <= debug_prob <= 1:
        raise ProtocolError("修复概率必须在 0 到 1 之间")
    if type(max_debug_depth) is not int or max_debug_depth < 0:
        raise ProtocolError("修复深度必须是非负整数")
    rng = rng or random.Random(0)
    drafts = [node for node in nodes if node["kind"] == "draft"]
    if len(drafts) < num_drafts:
        return {"action": "draft", "parent_id": None, "kind": "draft"}
    buggy = [node for node in nodes if node["status"] == "buggy" and node["depth"] < max_debug_depth
             and not any(child["parent_id"] == node["node_id"] and child["kind"] == "debug"
                         for child in nodes)]
    if buggy and rng.random() < debug_prob:
        parent = buggy[-1]
        return {"action": "debug", "parent_id": parent["node_id"], "kind": "debug"}
    scored = [node for node in nodes if node["status"] == "scored" and node.get("value") is not None]
    if not scored:
        return {"action": "draft", "parent_id": None, "kind": "draft"}
    return {"action": "improve", "parent_id": _best(scored, direction)["node_id"], "kind": "improve"}


def _best(nodes, direction="min"):
    return min(nodes, key=lambda node: node["value"]) if direction == "min" else max(
        nodes, key=lambda node: node["value"])


def best_node(nodes, direction="min"):
    scored = [node for node in nodes if node["status"] == "scored" and node.get("scope") != SMOKE_SCOPE
              and node.get("value") is not None]
    return _best(scored, direction) if scored else None


def reject_duplicate(edits, seen):
    fingerprint = diff_fingerprint(edits)
    if fingerprint in seen:
        raise ProtocolError("重复节点，未执行")
    seen.add(fingerprint)
    return fingerprint


def node_record(node_id, kind, parent_id, depth, status, value=None, scope="dev", revision_id=None):
    if kind not in {"draft", "debug", "improve"}:
        raise ProtocolError("未知搜索节点类型")
    if status not in {"scored", "buggy", "duplicate"}:
        raise ProtocolError("未知搜索节点状态")
    return {"node_id": node_id, "kind": kind, "parent_id": parent_id, "depth": depth,
            "status": status, "value": value, "scope": scope, "revision_id": revision_id}


def canonical_nodes(nodes):
    """稳定投影，供测试和笔记使用。"""
    return canonical(nodes)

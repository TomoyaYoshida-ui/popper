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
    if isinstance(edits, (str, bytes)) or not edits:
        raise ProtocolError("搜索节点必须给出代码修改")
    rows = []
    for edit in edits:
        if isinstance(edit, dict):
            rows.append(edit)
        else:
            rows.append({"path": edit.path, "original_sha256": edit.original_sha256,
                         "replacement_sha256": digest(edit.replacement)})
    return digest({"edits": rows})


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


# 明显变好取较宽的一档：绝对 0.1，或相对 1%，二者里更大的那个。
CLEAR_ABSOLUTE_IMPROVEMENT = 0.1
CLEAR_RELATIVE_IMPROVEMENT = 0.01
FAMILY_SWITCH_INSTRUCTION = (
    "Model-family switch is required for this revision. The latest scored search-set "
    "result is not clearly better than the current best score. Clear improvement means "
    "beating that best by the wider of 0.1 absolute and 1% relative. "
    "Replace the estimator with a tree, a kernel method, or a clearly different feature "
    "transform. Do not submit another ridge regression, an L2-regularized linear model, "
    "or a change that only adjusts regularization strength. "
    "A contract-rejected node is a failure and is not the best score. "
    "下一稿必须换模型族：树、核方法，或明确不同的特征变换。"
    "禁止再交岭回归、L2 正则线性模型，或只改正则强度。"
)


def _scored_search_nodes(nodes):
    """合同拒绝、重复和冒烟都不是搜索集成绩。"""
    return [node for node in nodes if node.get("status") == "scored"
            and node.get("scope") == "dev" and node.get("value") is not None]


def improvement_margin(best_value):
    return max(CLEAR_ABSOLUTE_IMPROVEMENT, CLEAR_RELATIVE_IMPROVEMENT * abs(best_value))


def clearly_better(new_value, best_value, direction="min"):
    improvement = (best_value - new_value) if direction == "min" else (new_value - best_value)
    return improvement >= improvement_margin(best_value) - 1e-9


def family_switch_required(nodes, direction="min"):
    """最新已评分节点没有明显超过此前最好成绩时，下一稿必须换模型族。"""
    scored = _scored_search_nodes(nodes)
    if len(scored) < 2:
        return False
    incumbent = _best(scored[:-1], direction)["value"]
    return not clearly_better(scored[-1]["value"], incumbent, direction)


def best_node(nodes, direction="min"):
    scored = [node for node in nodes if node["status"] == "scored" and node.get("scope") != SMOKE_SCOPE
              and node.get("value") is not None]
    return _best(scored, direction) if scored else None


def representative_observation(rows, direction="min"):
    """同一设计的代表观察取完整开发集评分的最优值。冒烟观察不能代表设计。"""
    scored = [row for row in rows if row.get("scope") == "dev" and row.get("value") is not None]
    if not scored:
        return None
    return min(scored, key=lambda row: row["value"]) if direction == "min" else max(
        scored, key=lambda row: row["value"])


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

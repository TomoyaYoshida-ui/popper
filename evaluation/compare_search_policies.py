"""离线比较一次实现加一次修复，和开发集上的实验树搜索。

分数来自脚本给定的独立评分序列，不调用模型，也不写入 Observation 或 Claim。
"""
from __future__ import annotations

import json
from pathlib import Path

from popper.research.tree_search import best_node, choose_action, node_record


def one_shot(outcomes):
    """第一次成功即停止；只有第一次失败时才再看一次修复。"""
    tried = outcomes[:1]
    if tried and tried[0].get("value") is None:
        tried = outcomes[:2]
    scored = [item for item in tried if item.get("value") is not None]
    return {"policy": "one_shot_repair", "tried": len(tried),
            "value": None if not scored else scored[0]["value"]}


def tree_search(outcomes, *, direction="min", num_drafts=2):
    nodes = []
    cursor = 0
    while cursor < len(outcomes):
        decision = choose_action(nodes, num_drafts=num_drafts, debug_prob=1, direction=direction)
        item = outcomes[cursor]
        status = "buggy" if item.get("value") is None else "scored"
        parent = decision["parent_id"]
        depth = 0 if parent is None else next(
            node["depth"] + 1 for node in nodes if node["node_id"] == parent)
        nodes.append(node_record(
            f"node-{cursor}", decision["kind"], parent, depth, status, item.get("value")))
        cursor += 1
    winner = best_node(nodes, direction)
    return {"policy": "tree_search", "tried": len(nodes),
            "value": None if winner is None else winner["value"],
            "representative": None if winner is None else winner["node_id"]}


def compare(outcomes, *, baseline, direction="min", threshold=0.2):
    policies = [one_shot(outcomes), tree_search(outcomes, direction=direction)]
    for policy in policies:
        delta = None if policy["value"] is None else (
            baseline - policy["value"] if direction == "min" else policy["value"] - baseline)
        policy["delta"] = None if delta is None else round(delta, 6)
        policy["reaches_threshold"] = delta is not None and delta >= threshold
    return {"schema_version": "1.0", "baseline": baseline, "direction": direction,
            "threshold": threshold, "paid_model_calls": 0, "policies": policies}


def main():
    report = compare([
        {"value": 0.9}, {"value": None}, {"value": 0.4}, {"value": 0.2},
    ], baseline=1.0)
    destination = Path(__file__).resolve().parent / "runs" / "search-policy-offline" / "report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    main()

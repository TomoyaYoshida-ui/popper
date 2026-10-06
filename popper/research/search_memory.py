"""从 Research Kernel 投影研究笔记。笔记不是第二套事实，缺了可以重算。"""
from __future__ import annotations

from .tree_search import SMOKE_SCOPE, best_node


def progress_for(hypothesis_id, observations, *, baseline_value=None, direction="min"):
    """idea：尚无实现；implemented：有开发集观察；progress：有观察优于基线。"""
    own = [row for row in observations if row.get("hypothesis_id") == hypothesis_id
           and row.get("scope") == "dev" and row.get("value") is not None]
    if not own:
        return "idea"
    if baseline_value is None:
        return "implemented"
    better = (lambda value: value < baseline_value) if direction == "min" else (
        lambda value: value > baseline_value)
    if any(better(row["value"]) for row in own):
        return "progress"
    return "implemented"


def research_notes(store, manifest, question, *, limit=4000):
    """把已试节点压成一段带分数的笔记，附加到下一次代码提案的问题描述。"""
    observations = [row for row in store.list("observation")
                    if row.get("scope") in {"dev", SMOKE_SCOPE}]
    direction = manifest.get("metric", {}).get("direction", "min")
    control_id = manifest.get("control_hypothesis_id")
    baseline_rows = [row for row in observations if row.get("hypothesis_id") == control_id
                     and row.get("scope") == "dev" and row.get("value") is not None]
    baseline_value = None if not baseline_rows else baseline_rows[-1]["value"]
    lines = [
        question, "",
        "Experiment-tree search node: return a non-empty edits array. "
        "Empty edits are rejected and do not end the search. "
        "Change an executable statement that runs during prediction for every seed. "
        "Keep the frozen config and the CLI contract. "
        "Improve the development metric past the constant baseline and past any "
        "development score listed below.",
        "",
        "研究笔记（由研究记录投影，不是新的事实来源）：",
    ]
    for hypothesis_id in manifest["candidate_hypothesis_ids"]:
        own = [row for row in observations if row.get("hypothesis_id") == hypothesis_id]
        progress = progress_for(hypothesis_id, observations, baseline_value=baseline_value,
                                direction=direction)
        lines.append(f"- {hypothesis_id} 进度 {progress}，"
                     f"开发集观察 {sum(1 for row in own if row.get('scope') == 'dev')} 条，"
                     f"冒烟观察 {sum(1 for row in own if row.get('scope') == SMOKE_SCOPE)} 条")
        for row in own:
            if row.get("scope") == "dev":
                lines.append(f"  - {row['observation_id']} = {row['value']} ({row['scope']})")
    failed = [row for row in store.list("run") if str(row.get("status", "")).endswith("failed")]
    if failed:
        lines.append(f"- 失败运行 {len(failed)} 次，状态：" +
                     "、".join(sorted({row["status"] for row in failed})))
    text = "\n".join(lines)
    if len(text.encode("utf-8")) <= limit:
        return text
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def journal_summary(nodes, direction="min"):
    winner = best_node(nodes, direction)
    return {"tried": len(nodes), "buggy": sum(1 for node in nodes if node["status"] == "buggy"),
            "representative": None if winner is None else winner["node_id"]}

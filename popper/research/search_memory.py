"""从 Research Kernel 投影研究笔记。笔记不是第二套事实，缺了可以重算。"""
from __future__ import annotations

from .tree_search import SMOKE_SCOPE, best_node


def progress_for(hypothesis_id, observations):
    """idea：尚无实现；implemented：有开发集观察；progress：有观察即视为已执行过实现。"""
    own = [row for row in observations if row.get("hypothesis_id") == hypothesis_id
           and row.get("scope") == "dev"]
    if not own:
        return "idea"
    return "implemented"


def research_notes(store, manifest, question, *, limit=4000):
    """把已试节点压成一段带分数的笔记，附加到下一次代码提案的问题描述。"""
    observations = [row for row in store.list("observation")
                    if row.get("scope") in {"dev", SMOKE_SCOPE}]
    lines = [question, "", "研究笔记（由研究记录投影，不是新的事实来源）："]
    for hypothesis_id in manifest["candidate_hypothesis_ids"]:
        own = [row for row in observations if row.get("hypothesis_id") == hypothesis_id]
        lines.append(f"- {hypothesis_id} 进度 {progress_for(hypothesis_id, observations)}，"
                     f"开发集观察 {sum(1 for row in own if row.get('scope') == 'dev')} 条，"
                     f"冒烟观察 {sum(1 for row in own if row.get('scope') == SMOKE_SCOPE)} 条")
        for row in own:
            if row.get("scope") == "dev":
                lines.append(f"  - {row['observation_id']} = {row['value']} ({row['scope']})")
    text = "\n".join(lines)
    if len(text.encode("utf-8")) <= limit:
        return text
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def journal_summary(nodes, direction="min"):
    winner = best_node(nodes, direction)
    return {"tried": len(nodes), "buggy": sum(1 for node in nodes if node["status"] == "buggy"),
            "representative": None if winner is None else winner["node_id"]}

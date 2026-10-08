"""契约内的三类候选。

特征工程候选改配方，模型族候选只能选内核模板，消融候选只留下观察。
本模块不调用模型，也不写 Claim。预算记在研究族账本上，三类共用同一个上限。
"""
from __future__ import annotations

from ..core import ProtocolError, digest

ACTIONS = ("feature_engineering", "model_family", "ablation")
MODEL_TEMPLATES = {
    "linear": {"params": ("C",)},
    "tree": {"params": ("max_depth",)},
    "kernel": {"params": ("C", "kernel")},
    "mlp": {"params": ("hidden_units",)},
}


def candidate_identity(recipe, edits=None, template=None):
    """重复判定用配方哈希加 diff 指纹。模板写进 diff，避免两个空 diff 撞在一起。"""
    recipe_sha = digest(recipe) if recipe is not None else None
    diff = digest({"edits": list(edits or []), "template": template})
    return {
        "recipe_sha256": recipe_sha,
        "diff_fingerprint": diff,
        "fingerprint": digest({"recipe_sha256": recipe_sha, "diff_fingerprint": diff}),
    }


def register_candidate(ledger, family_id, seen, *, parent_id, action, reason,
                       recipe=None, template=None, edits=None):
    """登记一个候选并预留 1 个预算单位。重复候选不扣预算。"""
    if action not in ACTIONS:
        raise ProtocolError("未知候选动作")
    if parent_id is not None and not isinstance(parent_id, str):
        raise ProtocolError("候选必须给出父节点")
    if not isinstance(reason, str) or not reason.strip():
        raise ProtocolError("候选必须给出理由")
    if edits is not None and not isinstance(edits, list):
        raise ProtocolError("候选 diff 必须是列表")
    if action == "model_family":
        template = _template(template)
    elif template is not None:
        raise ProtocolError("只有模型族候选可以选择模板")
    if action == "feature_engineering" and not isinstance(recipe, dict):
        raise ProtocolError("特征工程候选必须给出配方")
    if action == "ablation" and not isinstance(recipe, dict):
        raise ProtocolError("消融候选必须给出配方")
    identity = candidate_identity(recipe, edits, template)
    if identity["fingerprint"] in seen:
        raise ProtocolError("重复节点，未执行")
    reservation = ledger.reserve(family_id, identity["fingerprint"], 1.0, kind="dev")
    seen.add(identity["fingerprint"])
    return {
        "candidate_id": identity["fingerprint"][:16],
        "parent_id": parent_id,
        "action": action,
        "reason": reason.strip(),
        "template": template,
        "reservation_id": reservation["reservation_id"],
        **identity,
    }


def record_observation(candidate, *, scope, value):
    """消融观察可以记录，但不能由此写成 Claim。"""
    if candidate.get("action") not in ACTIONS:
        raise ProtocolError("未知候选动作")
    if scope not in {"dev", "confirmation"}:
        raise ProtocolError("观察范围必须是开发集或确认集")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError("观察值必须是有限数")
    return {
        "candidate_id": candidate["candidate_id"],
        "action": candidate["action"],
        "scope": scope,
        "value": value,
        "claim": False,
    }


def claim_from_observation(observation):
    """消融观察停在这里。其他观察也不由本模块写 Claim。"""
    if not isinstance(observation, dict) or observation.get("claim") is not False:
        raise ProtocolError("观察还没有记录")
    if observation.get("action") == "ablation":
        raise ProtocolError("消融观察不能升级为 Claim")
    return {"written": False, "candidate_id": observation["candidate_id"]}


def choose_space_action(nodes, explanations=None):
    """先按固定顺序补齐三类候选，之后在非消融节点上改进。

    带两个竞争解释的新问题不走这条固定顺序。
    """
    if explanations:
        raise ProtocolError("新问题不按固定顺序搜索，下一步必须指向一个竞争解释")
    if not isinstance(nodes, list):
        raise ProtocolError("候选节点必须是列表")
    present = []
    for node in nodes:
        action = node.get("action")
        if action not in ACTIONS:
            raise ProtocolError("未知候选动作")
        present.append(action)
    for action in ACTIONS:
        if action not in present:
            parent = nodes[-1]["candidate_id"] if nodes else None
            return {"action": action, "parent_id": parent, "kind": "draft"}
    scored = [node for node in nodes if node["action"] != "ablation" and node.get("value") is not None]
    parent = (max(scored, key=lambda node: node["value"]) if scored else nodes[-1])["candidate_id"]
    return {"action": "feature_engineering", "parent_id": parent, "kind": "improve"}


def _template(template):
    if not isinstance(template, dict) or template.get("id") not in MODEL_TEMPLATES:
        raise ProtocolError("模型族候选不在模板集合内")
    allowed = set(MODEL_TEMPLATES[template["id"]]["params"]) | {"id"}
    if not set(template) <= allowed:
        raise ProtocolError("模型族候选不在模板集合内")
    return dict(template)

"""相反解释的内核判定。

模型可以提出两条解释，不能指定谁被杀掉。delta 与预注册阈值决定被杀掉的方向；
正预测失败后的后继机制必须改写，且不能带上父确认集哈希。
"""
from __future__ import annotations

from ..core import ProtocolError

ABOVE = "above_threshold"
NOT_ABOVE = "not_above_threshold"
DIRECTIONS = frozenset({ABOVE, NOT_ABOVE})
KILLED_FIELDS = ("killed_explanation_id", "killed_id")


def normalize_text(text):
    if not isinstance(text, str):
        raise ProtocolError("后继机制必须改写")
    return " ".join(text.split()).casefold()


def _direction(row):
    contrast = row.get("contrast")
    if (not isinstance(contrast, dict) or set(contrast) != {"direction"}
            or contrast["direction"] not in DIRECTIONS):
        raise ProtocolError("解释方向必须是 above_threshold 或 not_above_threshold")
    return contrast["direction"]


def has_contrasts(explanations):
    """两条都没有 contrast 时返回 False。缺一、重复或方向相同则拒绝。"""
    if not isinstance(explanations, list) or len(explanations) != 2:
        raise ProtocolError("问题书必须有两个预测不同的解释")
    if any(not isinstance(row, dict) for row in explanations):
        raise ProtocolError("问题书必须有两个预测不同的解释")
    present = ["contrast" in row for row in explanations]
    if not any(present):
        return False
    if not all(present):
        raise ProtocolError("两条解释的方向必须相反")
    directions = [_direction(row) for row in explanations]
    if directions[0] == directions[1]:
        raise ProtocolError("两条解释的方向必须相反")
    return True


def above_threshold_id(explanations):
    if not has_contrasts(explanations):
        raise ProtocolError("两条解释的方向必须相反")
    return next(row["id"] for row in explanations if _direction(row) == ABOVE)


def reject_model_verdict(response):
    """模型回复里的 killed id 不能进入判定。字段出现即拒绝，不采用其值。"""
    if isinstance(response, dict) and any(key in response for key in KILLED_FIELDS):
        raise ProtocolError("模型不能指定被杀掉的解释")


def verdict(explanations, delta, threshold):
    """内核判定。delta 达到阈值时杀掉「不会超过」的那条，否则杀掉正预测。"""
    if not has_contrasts(explanations):
        raise ProtocolError("两条解释的方向必须相反")
    if (isinstance(delta, bool) or not isinstance(delta, (int, float))
            or isinstance(threshold, bool) or not isinstance(threshold, (int, float))):
        raise ProtocolError("效应与阈值必须是有限数")
    killed_direction = NOT_ABOVE if delta >= threshold else ABOVE
    killed = next(row for row in explanations if _direction(row) == killed_direction)
    surviving = next(row for row in explanations if row["id"] != killed["id"])
    return {
        "killed_explanation_id": killed["id"],
        "surviving_explanation_id": surviving["id"],
        "positive_killed": killed_direction == ABOVE,
    }


def offline_revision(killed_id, surviving_id, delta, threshold):
    """用分数写后继机制。这句话不是父陈述的复述。"""
    return (
        f"解释 {killed_id} 的正预测未达到阈值 {threshold:.8g}（效应 {delta:.8g}），"
        f"后继只保留 {surviving_id} 并改用新的可区分机制"
    )


def accept_revised_mechanism(text, *, explanations, parent_question, parent_holdout_sha256):
    if not isinstance(text, str) or not text.strip():
        raise ProtocolError("后继机制必须改写")
    cleaned = " ".join(text.split())
    normalized = normalize_text(cleaned)
    banned = [parent_question, *(row["statement"] for row in explanations)]
    if any(normalize_text(item) == normalized for item in banned):
        raise ProtocolError("后继机制必须改写")
    if parent_holdout_sha256 and parent_holdout_sha256 in cleaned:
        raise ProtocolError("后继提案不能包含父确认集哈希")
    return cleaned


# 确认前写死。模型可以写机制，不能改这个数，也不能改成更容易的指标。
FROZEN_THRESHOLD = 0.05
RARE_POSITIVE_MAX_RATE = 0.20


def contains_killed_field(value):
    if isinstance(value, dict):
        if any(key in value for key in KILLED_FIELDS):
            return True
        return any(contains_killed_field(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_killed_field(item) for item in value)
    return False


def guard_card(card):
    """提案阶段只允许训练+开发的数据卡。确认集标签出现即拒绝。"""
    if not isinstance(card, dict):
        raise ProtocolError("提案只能使用训练和开发范围的数据卡")
    stats = card.get("label_stats")
    if not isinstance(stats, dict) or stats.get("scope") != "train_dev":
        raise ProtocolError("提案只能使用训练和开发范围的数据卡")
    holdout = card.get("holdout")
    if isinstance(holdout, dict) and ({"labels", "label", "scores", "predictions"} & set(holdout)):
        raise ProtocolError("提案不能读取确认集标签")
    return stats


def public_card(card):
    """给模型的卡片：列摘要和训练+开发上的关联。不含原始行、封存哈希或确认集标签。"""
    stats = guard_card(card)
    columns = card.get("columns")
    if not isinstance(columns, list):
        raise ProtocolError("数据卡没有列")
    public_columns = []
    for column in columns:
        if not isinstance(column, dict) or not isinstance(column.get("name"), str):
            raise ProtocolError("数据卡没有列")
        public_columns.append({
            "name": column["name"],
            "type": column.get("type"),
            "missing_rate": column.get("missing_rate"),
        })
    return {
        "target_column": card.get("target_column"),
        "n_train": card.get("n_train"),
        "n_dev": card.get("n_dev"),
        "n_holdout": card.get("n_holdout"),
        "columns": public_columns,
        "label_stats": {
            "scope": "train_dev",
            "distribution": stats.get("distribution"),
            "associations": stats.get("associations"),
        },
    }


def ranked_associations(card):
    """按绝对关联从高到低。没有数值的关联不参与排名。"""
    stats = guard_card(card)
    rows = stats.get("associations")
    if not isinstance(rows, list):
        raise ProtocolError("数据卡至少需要两个特征关联")
    usable = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("feature"), str) or not row["feature"]:
            continue
        value = row.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        usable.append(row)
    if len(usable) < 2:
        raise ProtocolError("数据卡至少需要两个特征关联")
    usable.sort(key=lambda row: (-abs(row["value"]), row["feature"]))
    return usable


def metric_for_card(card):
    """封存之后只看训练+开发的标签分布。二分类走 F1 或准确率，多分类走宏平均 F1。"""
    stats = guard_card(card)
    values = [item.get("value") if isinstance(item, dict) else None for item in stats.get("distribution") or []]
    if any(type(value) is not int or value < 0 for value in values) or len(values) < 2:
        raise ProtocolError("标签不是域包已经接受的整数")
    if set(values) <= {0, 1}:
        positive = next(item["rate"] for item in stats["distribution"] if item["value"] == 1)
        if positive < RARE_POSITIVE_MAX_RATE:
            return "binary-f1-v1", "f1"
        return "binary-accuracy-v1", "accuracy"
    return "multiclass-macro-f1-v1", "macro_f1"


def card_problem_reply(card):
    """不调模型。正预测放在第二条，这样绑定到 above_threshold 才有意义。"""
    target = card.get("target_column")
    if not isinstance(target, str) or not target:
        raise ProtocolError("目标列不在数据卡内")
    ranked = ranked_associations(card)
    top = ranked[0]["feature"]
    return {
        "question": (
            f"在预注册切分上，列 {top} 的线性组合相对常数基线预测 {target} 是否达到 "
            f"{FROZEN_THRESHOLD:.8g}"
        ),
        "explanations": [
            {
                "id": "class-prior",
                "statement": (
                    f"目标 {target} 的类别先验就足以解释开发集分数，列 {top} 并不提供可分离的信号"
                ),
                "prediction": f"线性候选的开发集改善不达到 {FROZEN_THRESHOLD:.8g}",
                "contrast": {"direction": NOT_ABOVE},
            },
            {
                "id": "measured-signal",
                "statement": (
                    f"列 {top} 与目标 {target} 的关联来自可测量信号，线性候选会超过常数基线"
                ),
                "prediction": f"线性候选的开发集改善达到 {FROZEN_THRESHOLD:.8g}",
                "contrast": {"direction": ABOVE},
            },
        ],
    }


def next_mechanism(second_feature, threshold, target):
    """后继只改机制：第二强的列单独进线性候选，阈值保持原值。"""
    if not isinstance(second_feature, str) or not second_feature:
        raise ProtocolError("后继机制必须改写")
    if not isinstance(target, str) or not target:
        raise ProtocolError("后继机制必须改写")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        raise ProtocolError("后继提案必须带上父阈值")
    return (
        f"后继改为只把列 {second_feature} 送进线性候选，"
        f"检验它单独相对常数基线预测 {target} 是否达到同一阈值 {threshold:.8g}"
    )

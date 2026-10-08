"""只根据数据卡和一句研究目的提案。不读取原始表、封存件或确认集标签。"""
from __future__ import annotations

from ..core import ProtocolError
from .actions import (ADD_CONTROL, REQUEST_CONFIRMATION, RUN_EXPERIMENT, ActionProposal)
from .agenda import (
    ABOVE, FROZEN_THRESHOLD, above_threshold_id, accept_revised_mechanism,
    card_problem_reply, contains_killed_field, guard_card, has_contrasts,
    metric_for_card, next_mechanism, public_card, ranked_associations,
    reject_model_verdict)
from .formulate import _proposal, normalize_prediction

_BOUND = {RUN_EXPERIMENT, ADD_CONTROL, REQUEST_CONFIRMATION}

FIXTURES = {
    "fixture-table": {
        "question": "年龄与收入能否预测标签",
        "target_column": "label",
        "task_shape": "aligned_prediction",
        "metric": "mse-v1",
        "min_improvement": {"value": 0.1, "unit": "squared_error", "reason": "夹具阈值"},
        "repeat_unit": "train_seed",
        "stop_rule": {"kind": "reach_baseline"},
        "explanations": (
            {"id": "linear-signal",
             "statement": "年龄和收入的数值组合预测标签",
             "prediction": "线性候选的开发集误差小于常数基线 0.1"},
            {"id": "missingness",
             "statement": "标签只由收入是否缺失决定",
             "prediction": "线性候选的开发集误差不小于常数基线"},
        ),
    },
    "heart-disease-cleveland": {
        "question": "在预注册切分上，线性候选相对常数基线的宏平均 F1 是否达到 0.05",
        "target_column": "num",
        "task_shape": "aligned_prediction",
        "metric": "multiclass-macro-f1-v1",
        "min_improvement": {
            "value": 0.05, "unit": "macro_f1",
            "reason": "确认前写死的描述性阈值，不根据确认集调整",
        },
        "repeat_unit": "train_seed",
        "stop_rule": {"kind": "budget", "max_candidates": 1},
        "explanations": (
            {"id": "clinical-linear",
             "statement": "临床指标的线性组合优于常数基线",
             "prediction": "线性候选的开发集宏平均 F1 高于常数基线 0.05"},
            {"id": "class-balance",
             "statement": "改善只来自类别不平衡，而不是临床指标的组合",
             "prediction": "线性候选的开发集宏平均 F1 不高于常数基线 0.05"},
        ),
    },
}


def _card_target(card, target_column):
    columns = card.get("columns") if isinstance(card, dict) else None
    if not isinstance(columns, list):
        raise ProtocolError("数据卡没有列")
    if not any(isinstance(column, dict) and column.get("name") == target_column
               for column in columns):
        raise ProtocolError("目标列不在数据卡内")


class FixtureProposer:
    """不调模型。问题句只放在本模块的夹具里。"""

    name = "fixture"

    def __init__(self, case):
        if case not in FIXTURES:
            raise ProtocolError("没有这一份问题夹具")
        self.case = case

    def propose(self, card, objective, split_declaration_sha256):
        if not isinstance(objective, str) or not objective.strip():
            raise ProtocolError("研究目的必须是一句非空文字")
        fixture = FIXTURES[self.case]
        _card_target(card, fixture["target_column"])
        proposal = {
            "question": fixture["question"],
            "hypothesis": {"explanations": [dict(item) for item in fixture["explanations"]]},
            "target_column": fixture["target_column"],
            "task_shape": fixture["task_shape"],
            "metric": fixture["metric"],
            "min_improvement": dict(fixture["min_improvement"]),
            "repeat_unit": fixture["repeat_unit"],
            "stop_rule": dict(fixture["stop_rule"]),
            "split_declaration_sha256": split_declaration_sha256,
        }
        return _proposal(proposal)


class CardProposer:
    """不调模型，也不读文件。两条解释和指标都从数据卡来，阈值是冻结的。"""

    name = "card"

    def propose(self, card, objective, split_declaration_sha256):
        if not isinstance(objective, str) or not objective.strip():
            raise ProtocolError("研究目的必须是一句非空文字")
        return proposal_from_card_reply(card, card_problem_reply(card), split_declaration_sha256, fixed_baseline=True)


def proposal_from_card_reply(card, response, split_declaration_sha256, *, fixed_baseline=False):
    """内核拥有指标、阈值和目标列。模型只提供问题和两条相反解释。"""
    if not isinstance(response, dict):
        raise ProtocolError("模型问题书必须是 JSON 对象")
    if contains_killed_field(response):
        raise ProtocolError("模型不能指定被杀掉的解释")
    guard_card(card)
    metric, unit = metric_for_card(card)
    top = ranked_associations(card)[0]["feature"]
    target = card.get("target_column")
    if not isinstance(target, str) or not target:
        raise ProtocolError("目标列不在数据卡内")
    explanations = response.get("explanations")
    if explanations is None and isinstance(response.get("hypothesis"), dict):
        explanations = response["hypothesis"].get("explanations")
    if not isinstance(explanations, list) or len(explanations) != 2:
        raise ProtocolError("问题书必须有两个预测不同的解释")
    cleaned = []
    for row in explanations:
        if (not isinstance(row, dict) or not {"id", "statement", "contrast"} <= set(row)
                or set(row) - {"id", "statement", "prediction", "contrast"}):
            raise ProtocolError("每条解释只能有 id、statement、prediction、contrast")
        cleaned.append({
            "id": row["id"],
            "statement": row["statement"],
            "prediction": row.get("prediction", ""),
            "contrast": row["contrast"],
        })
    question = response.get("question")
    if not isinstance(question, str) or not question.strip() or (fixed_baseline and (target not in question or top not in question)):
        raise ProtocolError("问题必须点名数据卡上的目标列和关联最强的特征")
    from .prediction import build_prediction, render_prediction, validate_prediction_text
    contract = build_prediction(metric, FROZEN_THRESHOLD, scope=split_declaration_sha256)
    for row in cleaned:
        if row["prediction"]:
            validate_prediction_text(row["prediction"], metric)
        row["prediction_contract"] = {**contract, "operator":
            "gte" if row["contrast"].get("direction") == ABOVE else "lt"}
        row["prediction"] = render_prediction(row["prediction_contract"])
    execution = response.get("execution")
    proposal = _proposal({
        **({"execution": execution} if execution is not None else {}),
        "schema_version": "2.0", "prediction_contract": contract,
        "question": question.strip(),
        "hypothesis": {"explanations": cleaned},
        "target_column": target,
        "task_shape": "aligned_prediction",
        "metric": metric,
        "min_improvement": {
            "value": FROZEN_THRESHOLD,
            "unit": contract["unit"],
            "reason": "确认前写死的描述性阈值，不根据确认集调整",
        },
        "repeat_unit": "train_seed",
        "stop_rule": {"kind": "budget", "max_candidates": 1},
        "split_declaration_sha256": split_declaration_sha256,
    })
    positive = next(row for row in proposal["hypothesis"]["explanations"]
                    if row["contrast"]["direction"] == ABOVE)
    if fixed_baseline and top not in positive["statement"]:
        raise ProtocolError("正预测必须点名数据卡上关联最强的特征")
    return proposal


def solicit_card_problem(call, card, objective, split_declaration_sha256):
    """模型只看公开数据卡。校验失败再给一次错误，不改用夹具。"""
    if not isinstance(objective, str) or not objective.strip():
        raise ProtocolError("研究目的必须是一句非空文字")
    ranked = ranked_associations(card)
    top = ranked[0]["feature"]
    target = card["target_column"]
    system = (
        "只根据训练和开发数据卡提出可检验研究问题。返回 JSON，键为 question、explanations 和 execution。"
        "execution 恰好包含 baseline 配置对象、candidates 配置对象数组、seeds 整数数组、budget 正整数。"
        "选择实际可执行的对照、候选和重复值，不要求常数对线性模型。"
        "explanations 恰好两条，每条仅有 id、statement、contrast。"
        "contrast 为 {\"direction\": \"above_threshold\"} 或 {\"direction\": \"not_above_threshold\"}，"
        "两条方向必须不同。statement 是待验证机制解释，不要宣称机制已成立。"
        "选择有科学依据的特征、交互或对照，不要求使用关联排名第一的特征。"
        "指标、阈值和预测文本由内核提供，不要输出预测分数、prediction 或 killed id。"
    )
    payload = {
        "objective": objective,
        "data_card": public_card(card),
        "target_column": target,
        "registered_metric": metric_for_card(card)[0],
        "frozen_min_improvement": FROZEN_THRESHOLD,
    }
    last = None
    for _attempt in range(2):
        body = payload if last is None else {**payload, "validation_error": str(last)}
        response = call(system, body)
        try:
            if not isinstance(response, dict) or "execution" not in response:
                raise ProtocolError("模型必须提出可执行的实验设计，不能只填写解释")
            return proposal_from_card_reply(card, response, split_declaration_sha256)
        except ProtocolError as error:
            last = error
    raise last


def mechanism_after_kill(call, *, second_feature, target, killed_id, surviving_id,
                         delta, threshold, explanations, parent_question,
                         parent_holdout_sha256):
    """模型可以改写后继句子。句子必须点名第二特征并带上原阈值，否则内核自己写。"""
    error = None
    text = None
    author = "kernel"
    if call is not None:
        system = (
            "内核已经杀掉一条解释。返回 JSON，键只有 revised_mechanism。"
            "用一句话提出新机制：只把指定的第二特征送进线性候选，"
            "对照常数基线，阈值与父研究相同。"
            "这句话必须原样包含第二特征名和阈值数字。"
            "不要复述父问题或任一父陈述，不要包含确认集哈希，"
            "不要输出 killed_explanation_id 或 killed_id。"
        )
        payload = {
            "second_feature": second_feature,
            "target_column": target,
            "threshold": threshold,
            "parent_question": parent_question,
            "statements": [row["statement"] for row in explanations],
            "killed_explanation_id": killed_id,
            "surviving_explanation_id": surviving_id,
            "delta": delta,
        }
        last = None
        for _attempt in range(2):
            body = payload if last is None else {**payload, "validation_error": str(last)}
            try:
                response = call(system, body)
                if contains_killed_field(response):
                    raise ProtocolError("模型不能指定被杀掉的解释")
                if not isinstance(response, dict) or not isinstance(response.get("revised_mechanism"), str):
                    raise ProtocolError("后继机制必须改写")
                cleaned = accept_revised_mechanism(
                    response["revised_mechanism"], explanations=explanations,
                    parent_question=parent_question,
                    parent_holdout_sha256=parent_holdout_sha256)
                if second_feature not in cleaned or f"{threshold:.8g}" not in cleaned:
                    raise ProtocolError("后继机制必须点名第二特征和同一阈值")
                text = cleaned
                author = "model"
                break
            except ProtocolError as caught:
                last = caught
                error = str(caught)
        if text is None:
            author = "kernel_after_model_rejection"
    if text is None:
        text = accept_revised_mechanism(
            next_mechanism(second_feature, threshold, target),
            explanations=explanations, parent_question=parent_question,
            parent_holdout_sha256=parent_holdout_sha256)
    return text, author, error


class ModelProposer:
    """可选。模型回复先过问题书校验，失败不落盘。"""

    name = "model"

    def __init__(self, call):
        self._call = call

    def propose(self, card, objective, split_declaration_sha256):
        if not isinstance(objective, str) or not objective.strip():
            raise ProtocolError("研究目的必须是一句非空文字")
        system = (
            "Propose one research problem from the data card only. Return JSON with "
            "question, hypothesis.explanations (exactly two items; each has id, statement, "
            "prediction, and contrast.direction; the two predictions must differ; "
            "the two directions must be above_threshold and not_above_threshold), "
            "target_column, task_shape, metric, min_improvement {value, unit, reason}, "
            "repeat_unit, stop_rule, split_declaration_sha256, "
            "execution {baseline: configuration object, candidates: configuration array, seeds: integer array, budget: positive integer}. "
            "Do not include killed_explanation_id. Do not invent holdout labels or scores.")
        response = self._call(system, {
            "objective": objective,
            "data_card": card,
            "split_declaration_sha256": split_declaration_sha256,
        })
        if not isinstance(response, dict):
            raise ProtocolError("模型问题书必须是 JSON 对象")
        reject_model_verdict(response)
        proposal = dict(response)
        proposal["split_declaration_sha256"] = split_declaration_sha256
        if "execution" not in proposal:
            raise ProtocolError("模型必须提出可执行的实验设计")
        from .prediction import build_prediction, render_prediction, validate_prediction_text
        contract = build_prediction(proposal["metric"], proposal["min_improvement"]["value"],
                                    scope=split_declaration_sha256)
        proposal.update(schema_version="2.0", prediction_contract=contract)
        proposal["min_improvement"]["unit"] = contract["unit"]
        for row in proposal["hypothesis"]["explanations"]:
            validate_prediction_text(row["prediction"], proposal["metric"])
            row["prediction_contract"] = {**contract, "operator": "gte" if row["contrast"]["direction"] == ABOVE else "lt"}
            row["prediction"] = render_prediction(row["prediction_contract"])
        return _proposal(proposal)


def require_explanation_action(proposal, explanations):
    """有竞争解释的 Study：实验动作必须指向其中一条，且两条预测不同。"""
    if not explanations:
        if proposal.explanation_id is not None:
            raise ProtocolError("没有竞争解释的研究不能指定 explanation_id")
        return proposal
    if proposal.kind not in _BOUND:
        return proposal
    if not isinstance(explanations, list) or len(explanations) != 2:
        raise ProtocolError("问题书必须有两个预测不同的解释")
    predictions = [normalize_prediction(item["prediction"]) for item in explanations]
    if predictions[0] == predictions[1]:
        raise ProtocolError("问题书必须有两个预测不同的解释")
    has_contrasts(explanations)
    ids = {item["id"] for item in explanations}
    if proposal.explanation_id not in ids:
        raise ProtocolError("下一步实验必须指向一个竞争解释")
    if (has_contrasts(explanations) and proposal.kind == RUN_EXPERIMENT
            and proposal.explanation_id != above_threshold_id(explanations)):
        raise ProtocolError("下一步实验必须指向可证伪的正预测")
    return proposal


def bind_primary_explanation(proposal, explanations):
    """漏填 id 时，有相反方向就绑正预测，否则绑第一条。模型已经填了的 id 不在这里改。"""
    if not explanations or proposal.kind not in _BOUND or proposal.explanation_id is not None:
        return proposal
    if has_contrasts(explanations) and proposal.kind == RUN_EXPERIMENT:
        explanation_id = above_threshold_id(explanations)
    else:
        explanation_id = explanations[0]["id"]
    return ActionProposal(
        proposal.kind, proposal.rationale, proposal.hypothesis_id,
        proposal.alternatives, source=proposal.source, model=proposal.model,
        explanation_id=explanation_id)

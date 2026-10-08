"""问题形成。

模型回复只作为一份固定提案进入本模块。批准前校验数据卡与指标登记表；
批准后写出 experiment.json。批准之后再改指标、阈值或确认集，只能新建 Study。
"""
from __future__ import annotations

from ..core import ProtocolError, digest, file_hash, validate_spec, write_json
from ..domains.protocol import IMPLEMENTED_SHAPES
from .intake import IntakeLog, _project_dir, load_intake
from .metrics import require_metric
from .prediction import validate_prediction, validate_prediction_text, render_prediction

PROPOSAL_KEYS = frozenset({
    "question", "hypothesis", "target_column", "task_shape", "metric",
    "min_improvement", "repeat_unit", "stop_rule", "split_declaration_sha256",
})
HYPOTHESIS_KEYS = frozenset({"explanations"})
EXPLANATION_KEYS = frozenset({"id", "statement", "prediction"})
IMPROVEMENT_KEYS = frozenset({"value", "unit", "reason"})
_FROZEN = "批准后不能改指标、阈值或确认集，请使用 research followup"


def _log(project):
    return IntakeLog(project / ".popper-intake" / "intake.sqlite")


def _stop_rule(rule):
    if not isinstance(rule, dict) or "kind" not in rule:
        raise ProtocolError("停止规则必须能机器判定")
    kind = rule["kind"]
    if (kind == "budget" and set(rule) == {"kind", "max_candidates"}
            and type(rule["max_candidates"]) is int and rule["max_candidates"] >= 1):
        return
    if (kind == "no_improvement_rounds" and set(rule) == {"kind", "rounds"}
            and type(rule["rounds"]) is int and rule["rounds"] >= 1):
        return
    if kind == "reach_baseline" and set(rule) == {"kind"}:
        return
    raise ProtocolError("停止规则必须能机器判定")


def normalize_prediction(text):
    if not isinstance(text, str):
        raise ProtocolError("问题书必须有两个预测不同的解释")
    return " ".join(text.split()).casefold()


def _explanations(hypothesis):
    if not isinstance(hypothesis, dict) or set(hypothesis) != HYPOTHESIS_KEYS:
        raise ProtocolError("问题书必须有两个预测不同的解释")
    rows = hypothesis["explanations"]
    if not isinstance(rows, list) or len(rows) != 2:
        raise ProtocolError("问题书必须有两个预测不同的解释")
    seen = set()
    predictions = []
    for row in rows:
        if not isinstance(row, dict):
            raise ProtocolError("问题书必须有两个预测不同的解释")
        keys = set(row)
        if "contrast" in keys:
            keys.remove("contrast")
        keys.discard("prediction_contract")
        if keys != EXPLANATION_KEYS:
            raise ProtocolError("问题书必须有两个预测不同的解释")
        if any(not isinstance(row[key], str) or not row[key].strip() for key in EXPLANATION_KEYS):
            raise ProtocolError("问题书必须有两个预测不同的解释")
        if row["id"] in seen:
            raise ProtocolError("问题书必须有两个预测不同的解释")
        seen.add(row["id"])
        predictions.append(normalize_prediction(row["prediction"]))
    if predictions[0] == predictions[1]:
        raise ProtocolError("问题书必须有两个预测不同的解释")
    from .agenda import has_contrasts
    has_contrasts(rows)
    return rows


def _proposal(raw):
    extra = {"schema_version", "prediction_contract", "execution"}
    if not isinstance(raw, dict) or not PROPOSAL_KEYS <= set(raw) or set(raw) - PROPOSAL_KEYS - extra:
        raise ProtocolError("问题书字段不完整")
    if not isinstance(raw["question"], str) or not raw["question"].strip():
        raise ProtocolError("研究问题必须是一句可检验的陈述")
    _explanations(raw["hypothesis"])
    improvement = raw["min_improvement"]
    if (not isinstance(improvement, dict) or set(improvement) != IMPROVEMENT_KEYS
            or not isinstance(improvement["value"], (int, float))
            or isinstance(improvement["value"], bool)
            or not isinstance(improvement["unit"], str) or not improvement["unit"].strip()
            or not isinstance(improvement["reason"], str) or not improvement["reason"].strip()):
        raise ProtocolError("最小有意义改善必须带单位和理由")
    if raw.get("schema_version") not in {None, "2.0"}:
        raise ProtocolError("问题书版本不受支持")
    if raw.get("schema_version") == "2.0":
        contract = validate_prediction(raw.get("prediction_contract"), evaluator_id=raw["metric"],
                            threshold=improvement["value"])
        if improvement["unit"] != contract["unit"]:
            raise ProtocolError("改善阈值单位与预测契约不一致")
        for row in raw["hypothesis"]["explanations"]:
            contract = validate_prediction(row.get("prediction_contract"),
                evaluator_id=raw["metric"], threshold=improvement["value"],
                scope=raw["prediction_contract"]["scope"])
            if row["prediction"] != render_prediction(contract):
                raise ProtocolError("预测文本必须由冻结契约生成")
    require_metric(raw["metric"])
    for row in raw["hypothesis"]["explanations"]:
        validate_prediction_text(row["prediction"], raw["metric"])
    _stop_rule(raw["stop_rule"])
    if not isinstance(raw["split_declaration_sha256"], str) or len(raw["split_declaration_sha256"]) != 64:
        raise ProtocolError("切分声明与封存件不一致")
    return raw


def _checked(project, proposal):
    proposal = _proposal(proposal)
    seal, card, _directory = load_intake(project)
    if proposal["split_declaration_sha256"] != seal["split_declaration_sha256"]:
        raise ProtocolError("切分声明与封存件不一致")
    columns = {entry["name"]: entry for entry in card["columns"]}
    column = columns.get(proposal["target_column"])
    if column is None:
        raise ProtocolError("目标列不在数据卡内")
    if proposal["task_shape"] not in IMPLEMENTED_SHAPES:
        raise ProtocolError("任务形状不在已实现集合内")
    if proposal["task_shape"] != "aligned_prediction":
        raise ProtocolError("任务形状与数据卡不兼容")
    entry = require_metric(proposal["metric"])
    if entry["task_shape"] != proposal["task_shape"]:
        raise ProtocolError("指标与任务形状不兼容")
    if column["type"] not in entry["label_types"]:
        raise ProtocolError("目标列类型与任务形状不兼容")
    if proposal["repeat_unit"] != "train_seed":
        raise ProtocolError("重复单位与任务形状不兼容")
    from ..domains.protocol import MetricSpec
    metric = MetricSpec(
        name=entry["metric"]["name"], direction=entry["metric"]["direction"],
        value_domain=_domain(proposal["metric"]))
    if not metric.accepts(proposal["min_improvement"]["value"]):
        raise ProtocolError("最小有意义改善不在指标值域内")
    return proposal, entry


def _domain(metric_id):
    from ..core import evaluator_pack
    spec = evaluator_pack(metric_id).primary_metric()
    return spec.value_domain


def _experiment(proposal, entry):
    execution = proposal.get("execution")
    if execution is not None:
        if (not isinstance(execution, dict) or set(execution) != {"baseline", "candidates", "seeds", "budget"}):
            raise ProtocolError("执行设计必须包含 baseline/candidates/seeds/budget")
    execution = execution or {"baseline": {"model": "constant"}, "candidates": [{"model": "linear"}], "seeds": [0], "budget": 1}
    return {
        "name": proposal["question"][:80],
        "objective": proposal["question"],
        "entrypoint": "model.py",
        "code_files": ["model.py"],
        "train": "train.json",
        "dev": "dev.json",
        "test": "test.json",
        "baseline": execution["baseline"],
        "candidates": execution["candidates"],
        "metric": {"name": entry["metric"]["name"], "direction": entry["metric"]["direction"]},
        "seeds": execution["seeds"],
        "budget": execution["budget"],
        "timeout_seconds": 30,
        "min_improvement": proposal["min_improvement"]["value"],
    }


def load_approved_explanations(project):
    """已批准问题书里的两个解释。旧问题书或尚未批准时返回 None。"""
    project = _project_dir(project)
    database = project / ".popper-intake" / "intake.sqlite"
    if not database.is_file():
        return None
    log = _log(project)
    try:
        approved = log.event("problem_approved")
        if approved is None:
            return None
        problem_sha = approved["payload"]["problem_sha256"]
    finally:
        log.close()
    path = project / ".popper-intake" / "problems" / f"{problem_sha}.json"
    if not path.is_file():
        return None
    from ..core import read_json
    hypothesis = read_json(path).get("hypothesis")
    if not isinstance(hypothesis, dict) or "explanations" not in hypothesis:
        return None
    return hypothesis["explanations"]


def propose_problem(project, proposal):
    project = _project_dir(project)
    log = _log(project)
    try:
        if log.event("holdout_sealed") is None:
            raise ProtocolError("确认集尚未封存")
        if log.event("problem_approved") is not None:
            raise ProtocolError(_FROZEN)
        proposal, _entry = _checked(project, proposal)
        problem_sha = digest(proposal)
        directory = project / ".popper-intake" / "problems"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{problem_sha}.json"
        write_json(path, proposal)
        payload = {
            "proposal_id": problem_sha[:16],
            "problem_sha256": problem_sha,
            "split_declaration_sha256": proposal["split_declaration_sha256"],
        }
        log.append(problem_sha, "problem_proposed", payload)
        log.commit()
        return payload
    finally:
        log.close()


def approve_problem(project, proposal_id):
    project = _project_dir(project)
    log = _log(project)
    try:
        if log.event("problem_approved") is not None:
            raise ProtocolError(_FROZEN)
        proposed = [item for item in log.events()
                    if item["event_type"] == "problem_proposed"
                    and item["payload"]["proposal_id"] == proposal_id]
        if not proposed:
            raise ProtocolError("问题书尚未提案")
        problem_sha = proposed[-1]["payload"]["problem_sha256"]
        path = project / ".popper-intake" / "problems" / f"{problem_sha}.json"
        from ..core import read_json
        proposal, entry = _checked(project, read_json(path))
        if digest(proposal) != problem_sha:
            raise ProtocolError("问题书哈希不一致")
        spec = _experiment(proposal, entry)
        validate_spec(spec)
        destination = project / "experiment.json"
        write_json(destination, spec)
        experiment_sha = file_hash(destination)
        payload = {
            "proposal_id": proposal_id,
            "problem_sha256": problem_sha,
            "experiment_sha256": experiment_sha,
        }
        approved = log.append(problem_sha, "problem_approved", payload)
        sealed = log.event("holdout_sealed")
        if sealed is None or approved <= sealed["seq"]:
            raise ProtocolError("问题书批准必须在确认集封存之后")
        log.commit()
        return payload
    finally:
        log.close()

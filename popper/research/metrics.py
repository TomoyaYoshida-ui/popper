"""指标登记表。

条目来自今天已经注册的评估器。模型不能自造分数。
新指标只记录人提交的实现与夹具哈希，不执行提交的代码，也不因此变成可计分指标。
"""
from __future__ import annotations

from ..core import ProtocolError, digest, file_hash, score
from ..domains.protocol import IMPLEMENTED_SHAPES

TABLE_SHAPES = {
    'vector-mae-v1': 'aligned_prediction',
    "mse-v1": "aligned_prediction",
    "mae-v1": "aligned_prediction",
    "binary-accuracy-v1": "aligned_prediction",
    "binary-f1-v1": "aligned_prediction",
    "multiclass-macro-f1-v1": "aligned_prediction",
    "mlebench-spooky-logloss-v1": "aligned_prediction",
    "mlebench-nomad-rmsle-v1": "aligned_prediction",
    "mlebench-pizza-auc-v1": "aligned_prediction",
    "algorithm-throughput-v1": "staged_artifacts",
    "convergence-order-v1": "staged_artifacts",
    "treatment-effect-v1": "staged_artifacts",
    "oscillator-trajectory-v1": "staged_artifacts",
}
LABEL_TYPES = {
    'vector-mae-v1': frozenset({'integer', 'number'}),
    "mse-v1": frozenset({"integer", "number"}),
    "mae-v1": frozenset({"integer", "number"}),
    "binary-accuracy-v1": frozenset({"integer", "categorical"}),
    "binary-f1-v1": frozenset({"integer", "categorical"}),
    "multiclass-macro-f1-v1": frozenset({"integer", "categorical"}),
}

_REGRESSION = [{"id": "a", "x": 1, "y": 3}, {"id": "b", "x": 2, "y": 5}]
_BINARY = [{"id": "a", "features": [0.0], "label": 1},
           {"id": "b", "features": [1.0], "label": 0}]
_MULTI = [{"id": "a", "features": [0.0], "label": 0},
          {"id": "b", "features": [1.0], "label": 1},
          {"id": "c", "features": [2.0], "label": 2}]
FIXTURES = {
    'vector-mae-v1': ([{'id': 'a', 'features': [1., 2.], 'y': 3.}],
                      [{'id': 'a', 'prediction': 3.}], 0),
    "mse-v1": (_REGRESSION, [{"id": "a", "prediction": 3}, {"id": "b", "prediction": 5}], 0),
    "mae-v1": (_REGRESSION, [{"id": "a", "prediction": 3}, {"id": "b", "prediction": 5}], 0),
    "binary-accuracy-v1": (_BINARY, [{"id": "a", "prediction": 1}, {"id": "b", "prediction": 0}], 1),
    "binary-f1-v1": (_BINARY, [{"id": "a", "prediction": 1}, {"id": "b", "prediction": 0}], 1),
    "multiclass-macro-f1-v1": (
        _MULTI,
        [{"id": "a", "prediction": 0}, {"id": "b", "prediction": 1}, {"id": "c", "prediction": 2}],
        1,
    ),
}


def _evaluators():
    from ..core import EVALUATORS
    return EVALUATORS


def registry():
    evaluators = _evaluators()
    if set(evaluators) != set(TABLE_SHAPES):
        raise ProtocolError("指标登记表与已注册评估器不一致")
    entries = {}
    for evaluator_id, contract in evaluators.items():
        shape = TABLE_SHAPES[evaluator_id]
        if shape not in IMPLEMENTED_SHAPES:
            raise ProtocolError("任务形状不在已实现集合内")
        entries[evaluator_id] = {
            "evaluator_id": evaluator_id,
            "metric": dict(contract["metric"]),
            "evaluator_hash": digest(contract),
            "task_shape": shape,
            "label_types": sorted(LABEL_TYPES.get(evaluator_id, ())),
            "fixture": evaluator_id in FIXTURES,
        }
    return entries


def require_metric(metric_id):
    entries = registry()
    if metric_id not in entries:
        raise ProtocolError("指标不在登记表内")
    return entries[metric_id]


def detect_unversioned_change(evaluator_id, recorded_hash):
    entry = require_metric(evaluator_id)
    if entry["evaluator_hash"] != recorded_hash:
        raise ProtocolError("指标实现已变但 id 未换")
    return entry["evaluator_hash"]


def fixtures_pass():
    for evaluator_id, (rows, predictions, expected) in FIXTURES.items():
        if score(rows, predictions, evaluator_id) != expected:
            return False
    return True


def register_metric(project, metric_id, implementation, fixture, approver):
    """记录人提交的新指标。不执行实现文件，也不把它加入可计分登记表。"""
    from .intake import IntakeLog, _inside, _project_dir
    if not isinstance(metric_id, str) or not metric_id:
        raise ProtocolError("指标不在登记表内")
    if not isinstance(approver, str) or not approver.strip():
        raise ProtocolError("指标登记必须有人审核")
    if metric_id in registry():
        raise ProtocolError("指标已在登记表内")
    project = _project_dir(project)
    implementation_path = _inside(project, implementation, missing_message="指标实现文件不存在")
    fixture_path = _inside(project, fixture, missing_message="指标夹具文件不存在")
    log = IntakeLog(project / ".popper-intake" / "intake.sqlite")
    try:
        if log.event("holdout_sealed") is None:
            raise ProtocolError("确认集尚未封存")
        payload = {
            "metric_id": metric_id,
            "implementation_sha256": file_hash(implementation_path),
            "fixture_sha256": file_hash(fixture_path),
            "approver": approver,
        }
        log.append(metric_id, "metric_registered", payload)
        log.commit()
        return payload
    finally:
        log.close()

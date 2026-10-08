"""M-C：五个表格域包的契约编译与手写计分对照。不调用模型，不读确认集。"""
from __future__ import annotations

from pathlib import Path

from popper.core import score, write_json
from popper.research.contract import compile_contract
from popper.research.metrics import FIXTURES

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "evaluation" / "runs" / "task-contract"
METRICS = (
    "mse-v1", "mae-v1", "binary-accuracy-v1", "binary-f1-v1", "multiclass-macro-f1-v1",
)


def _contract(metric_id):
    target = "y" if metric_id in {"mse-v1", "mae-v1"} else "label"
    return {
        "target_column": target,
        "task_shape": "aligned_prediction",
        "metric": metric_id,
        "repeat_unit": "train_seed",
        "split_constraints": {"group_by": "group", "no_overlap": True},
        "id_column": "id",
    }


def main():
    if set(FIXTURES) != set(METRICS):
        raise SystemExit("五个表格指标的夹具不齐全")
    for metric_id in METRICS:
        rows, predictions, _expected = FIXTURES[metric_id]
        compiled = compile_contract(_contract(metric_id))
        handwritten = score(rows, predictions, metric_id)
        contracted = compiled.score(rows, predictions)
        if handwritten != contracted or compiled.source_evaluator_id != metric_id:
            raise SystemExit(f"{metric_id} 的契约分数与手写域包不一致")
        report = {
            "schema": "popper.task-contract-report/v1",
            "case": metric_id,
            "milestone": "M-C",
            "status": "passed",
            "model_called": False,
            "evaluator_id": metric_id,
            "source_evaluator_id": compiled.source_evaluator_id,
            "handwritten_score": handwritten,
            "contract_score": contracted,
            "scores_equal": True,
        }
        write_json(RUNS / metric_id / "report.json", report)
        print(f"写入 {RUNS / metric_id / 'report.json'}")


if __name__ == "__main__":
    main()

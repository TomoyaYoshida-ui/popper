"""M-D：同一份公开表上的三类候选、一次确认，以及同预算的代码对照。

不调用模型。消融观察不写成 Claim。确认只发生一次。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

from popper.core import Experiment, initialize, write_json
from popper.research.budget import BudgetLedger
from popper.research.candidate_space import (
    claim_from_observation, record_observation, register_candidate,
)
from popper.research.contract import compile_contract
from popper.research.formulate import approve_problem, propose_problem
from popper.research.intake import intake
from popper.research.recipe import approve_recipe, compile_recipe, propose_recipe
from popper.research.schema import migrate_budget

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_data_intake as published  # noqa: E402
from run_problem_formation import MODEL  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CASE = "heart-disease-cleveland"
ROOT = REPO / "evaluation" / "data" / "contract-loop" / CASE
REPORT = REPO / "evaluation" / "runs" / "contract-loop" / CASE / "report.json"
MA_REPORT = REPO / "evaluation" / "runs" / "data-intake" / CASE / "report.json"
BUDGET = 3.0

TREE_MODEL = MODEL.replace(
    'elif config == {"model": "linear"}:',
    'elif config == {"model": "tree", "max_depth": 3}:\n'
    '        from sklearn.tree import DecisionTreeClassifier\n'
    '        model = DecisionTreeClassifier(max_depth=3, random_state=int(args.seed))\n'
    '        model.fit([row["features"] for row in train], [row["label"] for row in train])\n'
    '        predictions = [int(value) for value in model.predict([row["features"] for row in inputs])]\n'
    '    elif config == {"model": "linear"}:',
)


def _compile(project, recipe, evaluator):
    path = project / "recipe.json"
    write_json(path, recipe)
    proposed = propose_recipe(project, path)
    approve_recipe(project, proposed["proposal_id"])
    compiled = compile_recipe(project, proposed["proposal_id"], evaluator)
    compiled["recipe_sha256"] = proposed["recipe_sha256"]
    return compiled


def _place(project, compiled):
    prefix = project / ".popper-intake" / "compiled" / compiled["recipe_sha256"]
    shutil.copyfile(prefix / "train.json", project / "train.json")
    shutil.copyfile(prefix / "dev.json", project / "dev.json")
    shutil.copyfile(prefix / "holdout.json", project / "test.json")


def _search(project, model_source):
    (project / "model.py").write_text(model_source, encoding="utf-8")
    initialize(project)
    experiment = Experiment(project)
    try:
        return experiment.search(trusted_local=True), experiment
    except Exception:
        experiment.close()
        raise


def _spec(question, candidates):
    return {
        "name": question[:80],
        "objective": question,
        "entrypoint": "model.py",
        "code_files": ["model.py"],
        "train": "train.json",
        "dev": "dev.json",
        "test": "test.json",
        "baseline": {"model": "constant"},
        "candidates": candidates,
        "metric": {"name": "macro_f1", "direction": "max"},
        "seeds": [0],
        "budget": 1,
        "timeout_seconds": 120,
        "min_improvement": published.MIN_IMPROVEMENT,
    }


def main():
    source_report = json.loads(MA_REPORT.read_text(encoding="utf-8"))
    sources = published.DATA / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    csv_path = published.prepare_heart(sources)
    source = published.screen_table(csv_path, published.HEART, published.TARGET)
    if source["raw_sha256"] != source_report["source"]["raw_sha256"]:
        raise SystemExit("公开表哈希与 M-A 报告不一致")
    if ROOT.exists():
        shutil.rmtree(ROOT)
    ROOT.mkdir(parents=True)
    local_csv = ROOT / csv_path.name
    shutil.copyfile(csv_path, local_csv)
    intake(
        ROOT, raw=local_csv.name, split_strategy="stratified", seed=published.SPLIT_SEED,
        train_ratio=published.TRAIN_RATIO, dev_ratio=published.DEV_RATIO,
        holdout_ratio=published.HOLDOUT_RATIO, source_note=published.SOURCE_NOTE,
        column=published.TARGET, target_column=published.TARGET)
    card = json.loads((ROOT / ".popper-intake" / "data-card.json").read_text(encoding="utf-8"))
    evaluator, _metric, _candidate = published.choose_metric(card)
    base_recipe = published.recipe_from_card(card)
    numeric = [column["name"] for column in card["columns"]
               if column["name"] != published.TARGET and column["missing_rate"] == 0
               and column["type"] in {"integer", "number"}]
    if len(numeric) < 2:
        raise SystemExit("没有两列完整数值特征可以做受控乘积")
    engineered_recipe = {
        **base_recipe,
        "steps": base_recipe["steps"] + [{
            "op": "product", "left": numeric[0], "right": numeric[1],
            "output": "product_feature",
        }],
    }
    ablated_recipe = base_recipe
    base = _compile(ROOT, base_recipe, evaluator)
    engineered = _compile(ROOT, engineered_recipe, evaluator)
    if base["holdout_row_set_sha256"] != engineered["holdout_row_set_sha256"]:
        raise SystemExit("特征工程改变了确认集行集合")
    if base["input_identity_sha256"] == engineered["input_identity_sha256"]:
        raise SystemExit("特征工程没有产生新的输入哈希")
    contract = compile_contract({
        "target_column": published.TARGET,
        "task_shape": "aligned_prediction",
        "metric": evaluator,
        "repeat_unit": "train_seed",
        "split_constraints": {"group_by": "id", "no_overlap": True},
        "id_column": "id",
    })
    conn = sqlite3.connect(str(ROOT / "candidates.sqlite"))
    migrate_budget(conn)
    ledger = BudgetLedger(conn)
    ledger.open_family("contract-loop", BUDGET)
    seen = set()
    feature = register_candidate(
        ledger, "contract-loop", seen, parent_id=None, action="feature_engineering",
        reason="在已有配方上增加两列完整数值特征的乘积", recipe=engineered_recipe)
    family = register_candidate(
        ledger, "contract-loop", seen, parent_id=feature["candidate_id"], action="model_family",
        reason="从模板集合选择深度为 3 的树", recipe=engineered_recipe,
        template={"id": "tree", "max_depth": 3})
    ablation = register_candidate(
        ledger, "contract-loop", seen, parent_id=feature["candidate_id"], action="ablation",
        reason="去掉乘积派生列", recipe=ablated_recipe)
    try:
        register_candidate(
            ledger, "contract-loop", seen, parent_id=None, action="model_family",
            reason="预算用尽后的额外模板", template={"id": "linear", "C": 1.0})
    except Exception as exc:
        if "预算不足" not in str(exc):
            raise
    else:
        raise SystemExit("三类候选之后预算没有拒绝")
    seal = json.loads((ROOT / ".popper-intake" / "holdout-seal.json").read_text(encoding="utf-8"))
    question = "在预注册切分上，带乘积派生列的线性候选相对常数基线是否达到 0.05 的宏平均 F1"
    proposal = {
        "question": question,
        "hypothesis": {
            "explanations": [
                {"id": "derived-linear", "statement": "乘积派生列上的线性候选优于常数基线",
                 "prediction": "线性候选的开发集宏平均 F1 高于常数基线 0.05"},
                {"id": "class-frequency", "statement": "改善来自类别频率而不是派生列",
                 "prediction": "线性候选的开发集宏平均 F1 不高于常数基线 0.05"},
            ],
        },
        "target_column": published.TARGET,
        "task_shape": "aligned_prediction",
        "metric": evaluator,
        "min_improvement": {
            "value": published.MIN_IMPROVEMENT, "unit": "macro_f1",
            "reason": "确认前写死的描述性阈值",
        },
        "repeat_unit": "train_seed",
        "stop_rule": {"kind": "budget", "max_candidates": 1},
        "split_declaration_sha256": seal["split_declaration_sha256"],
    }
    _place(ROOT, engineered)
    proposed = propose_problem(ROOT, proposal)
    approved = approve_problem(ROOT, proposed["proposal_id"])
    results, experiment = _search(ROOT, MODEL)
    try:
        experiment.freeze()
        claim = experiment.confirm(trusted_local=True)
        print(f"确认 claim_status={claim['status']} delta={claim['delta']}")
        feature_dev = _mean(results, {"model": "linear"})
        confirmation = _confirmation(experiment)
    finally:
        experiment.close()
    code_results = _sibling(ROOT, "code-only", base, MODEL, question)
    tree_results = _sibling(ROOT, "tree", engineered, TREE_MODEL, question,
                            [{"model": "tree", "max_depth": 3}])
    ablation_obs = record_observation(ablation, scope="dev", value=_mean(code_results, {"model": "linear"}))
    try:
        claim_from_observation(ablation_obs)
    except Exception as exc:
        if str(exc) != "消融观察不能升级为 Claim":
            raise
    else:
        raise SystemExit("消融观察写成了 Claim")
    report = {
        "schema": "popper.contract-loop-report/v1",
        "case": CASE,
        "milestone": "M-D",
        "status": "passed",
        "model_called": False,
        "source_raw_sha256": source["raw_sha256"],
        "holdout_row_set_sha256": engineered["holdout_row_set_sha256"],
        "budget_cap": BUDGET,
        "contract_evaluator_id": contract.evaluator_id,
        "problem_sha256": approved["problem_sha256"],
        "candidates": [_public(feature), _public(family), _public(ablation)],
        "dev": {
            "feature_engineering": feature_dev,
            "model_family": _mean(tree_results, {"model": "tree", "max_depth": 3}),
            "ablation": ablation_obs["value"],
            "code_only": _mean(code_results, {"model": "linear"}),
        },
        "confirmation": confirmation,
        "ablation_claim_written": False,
    }
    write_json(REPORT, report)
    print(f"写入 {REPORT}")


def _sibling(project, name, compiled, model_source, question, candidates=None):
    folder = project / name
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir()
    prefix = project / ".popper-intake" / "compiled" / compiled["recipe_sha256"]
    shutil.copyfile(prefix / "train.json", folder / "train.json")
    shutil.copyfile(prefix / "dev.json", folder / "dev.json")
    shutil.copyfile(prefix / "holdout.json", folder / "test.json")
    write_json(folder / "experiment.json", _spec(
        question, candidates or [{"model": "linear"}]))
    results, experiment = _search(folder, model_source)
    try:
        if experiment.state()["phase"] != "searching":
            raise SystemExit("对照实验离开了开发集")
        return results
    finally:
        experiment.close()


def _mean(results, config):
    match = [item for item in results if item["config"] == config]
    if len(match) != 1:
        raise SystemExit(f"没有唯一的开发集结果: {config}")
    return match[0]["mean"]


def _public(candidate):
    body = {
        "candidate_id": candidate["candidate_id"],
        "parent_id": candidate["parent_id"],
        "action": candidate["action"],
        "recipe_sha256": candidate["recipe_sha256"],
        "fingerprint": candidate["fingerprint"],
    }
    if candidate["template"] is not None:
        body["template"] = candidate["template"]["id"]
    return body


def _confirmation(experiment):
    rows = experiment.db.execute(
        "SELECT seq, kind, payload FROM events ORDER BY seq").fetchall()
    chain = [{"seq": seq, "kind": kind, "payload": json.loads(payload)}
             for seq, kind, payload in rows]
    consumed = [item for item in chain if item["kind"] == "test_consumed"]
    state = experiment.state()
    selected = state["selected"]
    observations = [item for item in chain
                    if item["kind"] == "run_completed"
                    and item["payload"]["result"]["split"] == "test"]
    by_config = {json.dumps(item["payload"]["result"]["config"], sort_keys=True): item
                 for item in observations}
    chosen = by_config[json.dumps(selected, sort_keys=True)]
    baseline = by_config[json.dumps({"model": "constant"}, sort_keys=True)]
    if len(consumed) != 1 or state["phase"] != "completed":
        raise SystemExit("没有恰好确认一次")
    return {
        "confirmation_calls": 1,
        "claim_status": state["claim"]["status"],
        "candidate_value": chosen["payload"]["result"]["mean"],
        "baseline_value": baseline["payload"]["result"]["mean"],
        "trust": chosen["payload"]["result"]["trust"],
    }


if __name__ == "__main__":
    main()

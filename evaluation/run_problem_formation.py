"""M-B：同一份公开表上的固定问题书、一次批准和一次确认。不调用模型。"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

from popper.core import Experiment, initialize, write_json
from popper.research.formulate import approve_problem, propose_problem
from popper.research.question_proposer import FixtureProposer
from popper.research.intake import IntakeLog, intake
from popper.research.recipe import approve_recipe, compile_recipe, propose_recipe

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_data_intake as published  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CASE = "heart-disease-cleveland"
PROJECT = REPO / "evaluation" / "data" / "problem-formation" / CASE
REPORT = REPO / "evaluation" / "runs" / "problem-formation" / CASE / "report.json"
MA_REPORT = REPO / "evaluation" / "runs" / "data-intake" / CASE / "report.json"

MODEL = '''"""问题书生成的基线与线性候选。不计算指标。"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    train = read_json(args.train)
    inputs = read_json(args.input)
    config = read_json(args.config)
    if config == {"model": "constant"}:
        counts = Counter(row["label"] for row in train)
        chosen = sorted(counts, key=lambda label: (-counts[label], label))[0]
        predictions = [chosen for _ in inputs]
    elif config == {"model": "linear"}:
        model = make_pipeline(StandardScaler(), LogisticRegression(
            C=1.0, max_iter=4000, random_state=int(args.seed)))
        model.fit([row["features"] for row in train], [row["label"] for row in train])
        predictions = [int(value) for value in model.predict(
            [row["features"] for row in inputs])]
    else:
        raise ValueError("未注册或字段不匹配的模型配置")
    Path(args.output).write_text(json.dumps(
        [{"id": row["id"], "prediction": int(prediction)}
         for row, prediction in zip(inputs, predictions)]), encoding="utf-8")


if __name__ == "__main__":
    main()
'''


def main():
    source_report = json.loads(MA_REPORT.read_text(encoding="utf-8"))
    sources = published.DATA / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    csv_path = published.prepare_heart(sources)
    source = published.screen_table(csv_path, published.HEART, published.TARGET)
    if source["raw_sha256"] != source_report["source"]["raw_sha256"]:
        raise SystemExit("公开表哈希与 M-A 报告不一致")
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
    PROJECT.mkdir(parents=True)
    local_csv = PROJECT / csv_path.name
    shutil.copyfile(csv_path, local_csv)
    intake(
        PROJECT, raw=local_csv.name, split_strategy="stratified", seed=published.SPLIT_SEED,
        train_ratio=published.TRAIN_RATIO, dev_ratio=published.DEV_RATIO,
        holdout_ratio=published.HOLDOUT_RATIO, source_note=published.SOURCE_NOTE,
        column=published.TARGET, target_column=published.TARGET)
    card = json.loads((PROJECT / ".popper-intake" / "data-card.json").read_text(encoding="utf-8"))
    evaluator, _metric, _candidate = published.choose_metric(card)
    recipe_path = PROJECT / "recipe.json"
    write_json(recipe_path, published.recipe_from_card(card))
    proposed_recipe = propose_recipe(PROJECT, recipe_path)
    approve_recipe(PROJECT, proposed_recipe["proposal_id"])
    compiled = compile_recipe(PROJECT, proposed_recipe["proposal_id"], evaluator)
    prefix = PROJECT / ".popper-intake" / "compiled" / proposed_recipe["recipe_sha256"]
    shutil.copyfile(prefix / "train.json", PROJECT / "train.json")
    shutil.copyfile(prefix / "dev.json", PROJECT / "dev.json")
    shutil.copyfile(prefix / "holdout.json", PROJECT / "test.json")
    seal = json.loads((PROJECT / ".popper-intake" / "holdout-seal.json").read_text(encoding="utf-8"))
    proposal = FixtureProposer("heart-disease-cleveland").propose(
        card, "UCI Heart Disease Cleveland", seal["split_declaration_sha256"])
    if proposal["metric"] != evaluator:
        raise SystemExit("夹具指标与数据卡选出的指标不一致")
    proposed = propose_problem(PROJECT, proposal)
    approved = approve_problem(PROJECT, proposed["proposal_id"])
    (PROJECT / "model.py").write_text(MODEL, encoding="utf-8")
    initialize(PROJECT)
    experiment = Experiment(PROJECT)
    try:
        experiment.search(trusted_local=True)
        experiment.freeze()
        claim = experiment.confirm(trusted_local=True)
        print(f"确认 claim_status={claim['status']} delta={claim['delta']}")
        report = _report(experiment, source, compiled, proposal, approved)
    finally:
        experiment.close()
    write_json(REPORT, report)
    print(f"写入 {REPORT}")
    if report["confirmation"]["confirmation_calls"] != 1:
        raise SystemExit("问题形成没有恰好确认一次")


def _report(experiment, source, compiled, proposal, approved):
    log = IntakeLog(PROJECT / ".popper-intake" / "intake.sqlite")
    try:
        events = log.events()
    finally:
        log.close()
    sealed = next(item for item in events if item["event_type"] == "holdout_sealed")
    approved_event = next(item for item in events if item["event_type"] == "problem_approved")
    if approved_event["seq"] <= sealed["seq"]:
        raise SystemExit("问题书批准不在封存之后")
    rows = experiment.db.execute(
        "SELECT seq, kind, payload FROM events ORDER BY seq").fetchall()
    chain = [{"seq": seq, "kind": kind, "payload": json.loads(payload)}
             for seq, kind, payload in rows]
    consumed = [item for item in chain if item["kind"] == "test_consumed"]
    state = experiment.state()
    selected = state["selected"]
    observations = [item for item in chain
                    if item["kind"] == "run_completed"
                    and item["payload"]["result"]["split"] == "test"
                    and item["payload"]["result"]["config"] == selected]
    if len(consumed) != 1 or not observations:
        raise SystemExit("确认没有恰好消费一次")
    observation = observations[-1]["payload"]["result"]
    return {
        "schema": "popper.problem-formation-report/v1",
        "case": CASE,
        "milestone": "M-B",
        "status": "passed",
        "model_called": False,
        "proposer": "fixture",
        "source_raw_sha256": source["raw_sha256"],
        "problem": proposal,
        "approval": {
            "proposal_id": approved["proposal_id"],
            "problem_sha256": approved["problem_sha256"],
            "experiment_sha256": approved["experiment_sha256"],
            "problem_approved_seq": approved_event["seq"],
            "holdout_sealed_seq": sealed["seq"],
        },
        "protocol_sha256": approved["experiment_sha256"],
        "input_identity_sha256": compiled["input_identity_sha256"],
        "holdout_row_set_sha256": compiled["holdout_row_set_sha256"],
        "confirmation": {
            "confirmation_calls": len(consumed),
            "confirmation_consumed_once": state["phase"] == "completed",
            "trust": observation["trust"],
            "evaluator_id": observation["evaluator_id"],
            "observation_seq": observations[-1]["seq"],
            "claim_status": state["claim"]["status"],
        },
    }


if __name__ == "__main__":
    main()

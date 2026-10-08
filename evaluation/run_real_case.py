"""真实案例：模型只看数据卡，内核杀掉一条解释，后继改机制。

复制队列在接入之前按行哈希留出，数据卡看不到它的标签。
阈值 0.05 在看任何确认集之前写死。模型校验失败时不改用夹具。
这不是临床结论。
"""
from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import urlparse

from popper.core import ProtocolError, initialize, read_json, write_json
from popper.research.agenda import (
    FROZEN_THRESHOLD, metric_for_card, ranked_associations, verdict)
from popper.research.followup import prepare_followup_project
from popper.research.formulate import approve_problem, propose_problem
from popper.research.intake import (
    _prepare_rows, intake, parse_bytes, resolve_adapter, split_rows)
from popper.research.models import EvidenceDrivenPolicy
from popper.research.question_proposer import mechanism_after_kill, solicit_card_problem
from popper.research.recipe import approve_recipe, compile_recipe, propose_recipe
from popper.research.recipe_runner import run_recipe
from popper.research.successor import (
    confirmation_row_set_sha256, read_successor_proposal, successor_for_failure,
    write_successor_proposal)
from popper.scoop import make_json_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_data_intake as published  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
CASE = "heart-disease-cleveland"
SOURCE = REPO / "evaluation" / "data" / "data-intake" / "sources" / "heart-disease.csv"
PROJECT = REPO / "evaluation" / "data" / "real-case" / CASE
FOLLOW_PROJECT = REPO / "evaluation" / "data" / "real-case" / f"{CASE}-followup"
RUN_ROOT = REPO / "evaluation" / "runs" / "real-case" / CASE
PARENT_RUN = RUN_ROOT / "parent"
FOLLOW_RUN = RUN_ROOT / "followup"
REPORT = RUN_ROOT / "report.json"
OBJECTIVE = "UCI Heart Disease Cleveland：线性候选相对常数基线能否超过冻结阈值"

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
    elif (isinstance(config, dict) and set(config) == {"model", "feature_indices"}
          and config.get("model") == "linear"):
        indices = config["feature_indices"]
        if (not isinstance(indices, list) or not indices
                or any(type(item) is not int or item < 0 for item in indices)
                or len(indices) != len(set(indices))):
            raise ValueError("未注册或字段不匹配的模型配置")
        width = len(train[0]["features"])
        if any(item >= width for item in indices):
            raise ValueError("未注册或字段不匹配的模型配置")

        def take(row):
            return [row["features"][item] for item in indices]

        model = make_pipeline(StandardScaler(), LogisticRegression(
            C=1.0, max_iter=4000, random_state=int(args.seed)))
        model.fit([take(row) for row in train], [row["label"] for row in train])
        predictions = [int(value) for value in model.predict([take(row) for row in inputs])]
    else:
        raise ValueError("未注册或字段不匹配的模型配置")
    Path(args.output).write_text(json.dumps(
        [{"id": row["id"], "prediction": int(prediction)}
         for row, prediction in zip(inputs, predictions)]), encoding="utf-8")


if __name__ == "__main__":
    main()
'''


class CardBackedPolicy(EvidenceDrivenPolicy):
    """开发集杀掉正预测时，后继句子必须改到第二特征。模型没写合格就由内核写。"""

    name = "card_backed_policy"

    def __init__(self, call, second_feature, target):
        self._call = call
        self.second_feature = second_feature
        self.target = target
        self.successor_author = None
        self.successor_error = None

    def revise_mechanism(self, *, killed_id, surviving_id, delta, threshold,
                         explanations, parent_question, parent_holdout_sha256):
        text, author, error = mechanism_after_kill(
            self._call, second_feature=self.second_feature, target=self.target,
            killed_id=killed_id, surviving_id=surviving_id, delta=delta,
            threshold=threshold, explanations=explanations,
            parent_question=parent_question, parent_holdout_sha256=parent_holdout_sha256)
        self.successor_author = author
        self.successor_error = error
        return text


def main():
    report = {
        "schema": "popper.real-case-report/v1",
        "case": CASE,
        "medical_claim": False,
        "model_called": False,
        "proposer": None,
        "note": (
            "这不是临床结论，也不是模型能力榜。阈值在看确认集之前写死。"
            "特征排名用的是研究表的训练加开发，复制队列的标签没有进入数据卡。"
            "后继开发集因此不是独立检验；复制队列上的确认才是。"
            "科学上没过阈值就保持为负结果。"
        ),
    }
    try:
        _run(report)
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        RUN_ROOT.mkdir(parents=True, exist_ok=True)
        write_json(REPORT, report)
        print(f"写入 {REPORT}")


def _run(report):
    _require_runtime()
    if not SOURCE.is_file():
        raise SystemExit(f"缺少本地表 {SOURCE}，本脚本不下载")
    _reset()
    study_csv, reserved_csv, split_counts = _reserve_source()
    report["split"] = split_counts
    intake(
        PROJECT, raw=study_csv.name, split_strategy="stratified", seed=published.SPLIT_SEED,
        train_ratio=published.TRAIN_RATIO, dev_ratio=published.DEV_RATIO,
        holdout_ratio=published.HOLDOUT_RATIO, source_note=published.SOURCE_NOTE,
        column=published.TARGET, target_column=published.TARGET)
    card = read_json(PROJECT / ".popper-intake" / "data-card.json")
    if card["label_stats"]["scope"] != "train_dev":
        raise ProtocolError("提案只能使用训练和开发范围的数据卡")
    if "labels" in card.get("holdout", {}):
        raise ProtocolError("提案不能读取确认集标签")
    ranked = ranked_associations(card)
    top, second = ranked[0], ranked[1]
    report["associations"] = [
        {"feature": row["feature"], "method": row["method"], "value": row["value"]}
        for row in ranked[:5]]
    seal = read_json(PROJECT / ".popper-intake" / "holdout-seal.json")
    metric, _unit = metric_for_card(card)
    recipe_path = PROJECT / "recipe.json"
    write_json(recipe_path, published.recipe_from_card(card))
    proposed_recipe = propose_recipe(PROJECT, recipe_path)
    approve_recipe(PROJECT, proposed_recipe["proposal_id"])
    compiled = compile_recipe(PROJECT, proposed_recipe["proposal_id"], metric)
    prefix = PROJECT / ".popper-intake" / "compiled" / proposed_recipe["recipe_sha256"]
    shutil.copyfile(prefix / "train.json", PROJECT / "train.json")
    shutil.copyfile(prefix / "dev.json", PROJECT / "dev.json")
    shutil.copyfile(prefix / "holdout.json", PROJECT / "test.json")
    base = os.environ["POPPER_CHAT_BASE_URL"]
    model_name = os.environ["POPPER_CHAT_MODEL"]
    call = make_json_client(base, model_name, RUN_ROOT / "model-calls")
    report["model"] = {
        "host": urlparse(base).hostname,
        "name": model_name,
    }
    report["model_called"] = True
    report["proposer"] = "model"
    proposal = solicit_card_problem(
        call, card, OBJECTIVE, seal["split_declaration_sha256"])
    report["proposal_accepted"] = True
    report["problem"] = {
        "question": proposal["question"],
        "metric": proposal["metric"],
        "min_improvement": proposal["min_improvement"]["value"],
        "explanations": proposal["hypothesis"]["explanations"],
        "strongest_feature": top["feature"],
        "second_feature": second["feature"],
    }
    if proposal["min_improvement"]["value"] != FROZEN_THRESHOLD:
        raise ProtocolError("模型不能改预注册阈值")
    proposed = propose_problem(PROJECT, proposal)
    approve_problem(PROJECT, proposed["proposal_id"])
    (PROJECT / "model.py").write_text(MODEL, encoding="utf-8")
    initialize(PROJECT)
    policy = CardBackedPolicy(call, second["feature"], card["target_column"])
    from popper.research.controller import ResearchController
    ResearchController.initialize(PROJECT, PARENT_RUN, policy=policy, budget_cap=8)
    controller = ResearchController(PARENT_RUN, policy=policy)
    try:
        status = controller.run(trusted_local=True)
        if status["phase"] == "ready_for_confirmation":
            status = controller.confirm(trusted_local=True)
        parent_dev = _effect(controller, "dev")
        parent_confirmation = _effect(controller, "confirmation")
        explanations = controller.manifest["explanations"]
        threshold = controller.manifest["min_meaningful_effect"]
        outcome = verdict(explanations, parent_dev["delta"], threshold)
        question = controller.store.get("study", controller.manifest["study_id"])["question"]
        holdout_sha = controller.manifest["input_identity"]["input_hashes"]["test.json"]
        successor = read_successor_proposal(PARENT_RUN)
        author = policy.successor_author
        revision_error = policy.successor_error
        if successor is None:
            text, author, revision_error = mechanism_after_kill(
                call, second_feature=second["feature"], target=card["target_column"],
                killed_id=outcome["killed_explanation_id"],
                surviving_id=outcome["surviving_explanation_id"],
                delta=parent_dev["delta"], threshold=threshold,
                explanations=explanations, parent_question=question,
                parent_holdout_sha256=holdout_sha)
            successor = successor_for_failure(
                explanations=explanations,
                tested_explanation_id=outcome["killed_explanation_id"],
                observation_refs=(parent_dev["baseline_observation_id"],
                                  parent_dev["candidate_observation_id"]),
                parent_threshold=threshold, parent_question=question,
                parent_holdout_sha256=holdout_sha, revised_mechanism=text)
            write_successor_proposal(PARENT_RUN, successor)
        report["parent"] = {
            "phase": status["phase"],
            "claim_status": _claim_status(controller),
            "killed_explanation_id": outcome["killed_explanation_id"],
            "surviving_explanation_id": outcome["surviving_explanation_id"],
            "positive_killed": outcome["positive_killed"],
            "dev": parent_dev,
            "confirmation": parent_confirmation,
            "threshold": threshold,
        }
    finally:
        controller.close()
    mechanism = successor["revised_mechanism"]
    if second["feature"] not in mechanism:
        raise ProtocolError("后继机制必须点名第二特征和同一阈值")
    if mechanism == question or any(mechanism == row["statement"] for row in explanations):
        raise ProtocolError("后继机制必须改写")
    replication = _project_replication(
        reserved_csv, seal, prefix / "recipe_fit.json", read_json(recipe_path))
    parent_rows = read_json(PROJECT / "test.json")
    report["holdout"] = {
        "parent_row_set_sha256": confirmation_row_set_sha256(parent_rows),
        "replication_row_set_sha256": confirmation_row_set_sha256(replication),
        "hashes_differ": confirmation_row_set_sha256(parent_rows) != confirmation_row_set_sha256(replication),
        "replication_rows": len(replication),
    }
    if not report["holdout"]["hashes_differ"]:
        raise ProtocolError("后继研究不能复用父确认集")
    replication_path = RUN_ROOT / "replication.json"
    write_json(replication_path, replication)
    columns = read_json(prefix / "recipe_fit.json")["output_feature_columns"]
    indices = _feature_indices(columns, second["feature"])
    candidate = {"model": "linear", "feature_indices": indices}
    prepare_followup_project(
        PARENT_RUN, FOLLOW_PROJECT, [candidate], replication_path,
        budget=1, objective=mechanism, min_improvement=threshold)
    ResearchController.initialize(FOLLOW_PROJECT, FOLLOW_RUN, budget_cap=8)
    follow = ResearchController(FOLLOW_RUN)
    try:
        follow_status = follow.run(trusted_local=True)
        if follow_status["phase"] == "ready_for_confirmation":
            follow_status = follow.confirm(trusted_local=True)
        follow_dev = _effect(follow, "dev")
        follow_confirmation = _effect(follow, "confirmation")
        follow_threshold = follow.manifest["min_meaningful_effect"]
        report["successor"] = {
            "author": author,
            "revision_error": revision_error,
            "revised_mechanism": mechanism,
            "feature": second["feature"],
            "feature_indices": indices,
            "threshold": follow_threshold,
            "threshold_unchanged": follow_threshold == threshold == FROZEN_THRESHOLD,
            "phase": follow_status["phase"],
            "claim_status": _claim_status(follow),
            "dev": follow_dev,
            "confirmation": follow_confirmation,
        }
    finally:
        follow.close()
    report["tokens_used"] = call.tokens_used()
    report["status"] = "completed"
    _print_summary(report)


def _require_runtime():
    try:
        import sklearn  # noqa: F401
    except ImportError as error:
        raise SystemExit("真实案例需要 sklearn，失败时不改用夹具") from error
    missing = [name for name in ("POPPER_API_KEY", "POPPER_CHAT_BASE_URL", "POPPER_CHAT_MODEL")
               if not os.environ.get(name)]
    if missing:
        raise SystemExit("缺少 " + "、".join(missing) + "，失败时不改用夹具")


def _reset():
    for path in (PROJECT, FOLLOW_PROJECT, RUN_ROOT):
        if path.exists():
            shutil.rmtree(path)
    PROJECT.mkdir(parents=True)
    RUN_ROOT.mkdir(parents=True)


def _reserve_source():
    """接入前留出约五分之一。某一标签若会因此少于 3 行，该行留在研究表里。"""
    raw = SOURCE.read_text(encoding="utf-8-sig")
    rows = list(csv.reader(io.StringIO(raw)))
    if not rows or published.TARGET not in rows[0]:
        raise ProtocolError("本地表没有目标列")
    header, body = rows[0], [row for row in rows[1:] if any(cell.strip() for cell in row)]
    target_at = header.index(published.TARGET)
    counts = {}
    for row in body:
        counts[row[target_at]] = counts.get(row[target_at], 0) + 1
    study, reserved = [], []
    reserved_counts = {}
    for row in body:
        line = ",".join(row)
        bucket = int(hashlib.sha256(line.encode("utf-8")).hexdigest()[:8], 16)
        label = row[target_at]
        take = bucket % 5 == 0 and counts[label] - reserved_counts.get(label, 0) - 1 >= 3
        if take:
            reserved.append(row)
            reserved_counts[label] = reserved_counts.get(label, 0) + 1
        else:
            study.append(row)
    if len(reserved) < 3:
        raise ProtocolError("复制队列行数不足")
    study_path = PROJECT / "heart-study.csv"
    reserved_path = RUN_ROOT / "heart-reserved.csv"
    _write_csv(study_path, header, study)
    _write_csv(reserved_path, header, reserved)
    return study_path, reserved_path, {
        "study_rows": len(study),
        "reserved_rows": len(reserved),
        "rule": "sha256(line)[:8] % 5 == 0，且研究表每个标签至少保留 3 行",
    }


def _write_csv(path, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    path.write_text(buffer.getvalue(), encoding="utf-8")


def _project_replication(reserved_csv, seal, fit_path, recipe):
    """用研究表的训练行重新拟合配方，只把复制队列投影成新确认集。"""
    study_csv = PROJECT / "heart-study.csv"
    declaration = seal["split_declaration"]
    train, _dev, train_ids, types = _split_study(study_csv, declaration)
    compiled_ids = {row["id"] for row in read_json(PROJECT / "train.json")}
    if train_ids != compiled_ids:
        raise ProtocolError("复制队列的配方拟合与父训练集不一致")
    columns, raw_rows, _reserved_types = _parsed(reserved_csv)
    reserved = _prepare_rows(columns, raw_rows, types, declaration)
    for index, row in enumerate(reserved):
        row["id"] = f"rep{index:06d}"
    labels = {row["id"]: row["values"][published.TARGET] for row in reserved}
    fit, _train_out, _dev_out, holdout_out = run_recipe(recipe, train, train[:1], reserved, types)
    expected = read_json(fit_path)["output_feature_columns"]
    if fit["output_feature_columns"] != expected:
        raise ProtocolError("复制队列的配方拟合与父训练集不一致")
    projected = []
    for row in holdout_out:
        projected.append({
            "id": row["id"],
            "features": [row["values"][name] for name in expected],
            "label": labels[row["id"]],
        })
    return projected


def _split_study(path, declaration):
    columns, raw_rows, types = _parsed(path)
    rows = _prepare_rows(columns, raw_rows, types, declaration)
    parts = split_rows(rows, declaration, declaration["ratios"])
    return parts["train"], parts["dev"], {row["id"] for row in parts["train"]}, types


def _parsed(path):
    raw = path.read_bytes()
    adapter = resolve_adapter(path, None, raw)
    return parse_bytes(path, adapter, raw)


def _feature_indices(columns, feature):
    indexes = [index for index, name in enumerate(columns)
               if name == feature or name.startswith(feature + "__")]
    if not indexes:
        raise ProtocolError(f"第二特征没有进入编译列: {feature}")
    return indexes


def _claim_status(controller):
    claims = [item for item in controller.store.list("claim")
              if item.get("study_id") == controller.manifest["study_id"]]
    if not claims:
        return None
    return claims[-1].get("status")


def _effect(controller, scope):
    manifest = controller.manifest
    control = manifest["control_hypothesis_id"]
    rows = [item for item in controller.store.list("observation")
            if item.get("scope") == scope and item.get("study_id") == manifest["study_id"]]
    if not rows:
        return None
    baseline = next(item for item in rows if item["hypothesis_id"] == control)
    candidate = next(item for item in rows if item["hypothesis_id"] != control)
    direction = manifest["metric"]["direction"]
    delta = (baseline["value"] - candidate["value"] if direction == "min"
             else candidate["value"] - baseline["value"])
    return {
        "delta": delta,
        "baseline": baseline["value"],
        "candidate": candidate["value"],
        "baseline_observation_id": baseline["observation_id"],
        "candidate_observation_id": candidate["observation_id"],
        "meets_threshold": delta >= manifest["min_meaningful_effect"],
    }


def _print_summary(report):
    parent = report["parent"]
    successor = report["successor"]
    print(
        f"模型={report['model']['name']} 调用={report['model_called']} "
        f"杀掉={parent['killed_explanation_id']} "
        f"父开发集效应={parent['dev']['delta']:.4f} "
        f"后继作者={successor['author']} "
        f"复制队列开发集效应="
        f"{None if successor['dev'] is None else round(successor['dev']['delta'], 4)} "
        f"复制队列确认="
        f"{None if successor['confirmation'] is None else round(successor['confirmation']['delta'], 4)}"
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        sys.exit(1)

"""独立基准校验：验证任务的隐藏证据情形带宽，不满足即拒发。

普通条件以开发集校验。范围边界还必须由同一候选在隐藏 holdout 上满足完整的
全局与预登记切片门，防止把仅在开发集偶然成立的边界写入 gold。校验发生在一次性
scratch 副本中；公开模板保持未初始化，所有校验预测均随 scratch 删除。
"""
from __future__ import annotations

import shutil
import inspect
import runpy
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from popper.core import (ProtocolError, canonical, dataset, initialize, read_json, score,
                         write_json)
from popper.research.evaluation_service import (_analysis_slices, _slice_rows, _slice_score,
                                                scoring_code_hash)


def _delta(direction, baseline_mean, candidate_mean):
    return (baseline_mean - candidate_mean if direction == "min"
            else candidate_mean - baseline_mean)


def _evaluate_registered(project, spec, evaluator_id, split):
    """Score registered implementations in-process for trusted gold construction only."""
    module = runpy.run_path(str(project / spec["entrypoint"]))
    estimator = module.get("estimator")
    if not callable(estimator):
        raise ProtocolError("任务入口缺少可校验的 estimator")
    train = dataset(project / spec["train"], evaluator_id)
    rows = dataset(project / spec[split], evaluator_id)
    accepts_seed = len(inspect.signature(estimator).parameters) == 2
    results = {}
    for config in [spec["baseline"], *spec["candidates"]]:
        points, predictions = [], {}
        for seed in spec["seeds"]:
            model = estimator(config, seed) if accepts_seed else estimator(config)
            if evaluator_id == "binary-accuracy-v1":
                if config.get("model") == "one_feature":
                    index = int(config.get("feature", 0))
                    train_x = [[row["features"][index]] for row in train]
                    test_x = [[row["features"][index]] for row in rows]
                else:
                    train_x = [row["features"] for row in train]
                    test_x = [row["features"] for row in rows]
                model.fit(train_x, [row["label"] for row in train])
                values = model.predict(test_x)
                predicted = [{"id": row["id"], "prediction": int(value)}
                             for row, value in zip(rows, values)]
            else:
                transform = config.get("transform")
                feature = (lambda x: abs(x)) if transform == "abs" else (
                    (lambda x: 1.0 if x >= 0 else -1.0) if transform == "sign" else lambda x: x)
                model.fit([[feature(row["x"])] for row in train], [row["y"] for row in train])
                values = model.predict([[feature(row["x"])] for row in rows])
                predicted = [{"id": row["id"], "prediction": float(value)}
                             for row, value in zip(rows, values)]
            value = score(rows, predicted, evaluator_id)
            points.append({"seed": seed, "value": value})
            predictions[seed] = predicted
        values = [point["value"] for point in points]
        results[canonical(config)] = {
            "mean": statistics.mean(values), "per_seed": points,
            "predictions": predictions}
    return rows, results


def _slice_effects(rows, registered, baseline_predictions, candidate_predictions,
                   seeds, evaluator_id, direction):
    effects = []
    for item in registered:
        selected = _slice_rows(rows, item["rule"])
        baseline = statistics.mean(_slice_score(
            selected, baseline_predictions[seed], evaluator_id) for seed in seeds)
        candidate = statistics.mean(_slice_score(
            selected, candidate_predictions[seed], evaluator_id) for seed in seeds)
        effects.append(_delta(direction, baseline, candidate))
    return effects


def _is_boundary(global_effect, slice_effects, threshold):
    return (abs(global_effect) < threshold and len(slice_effects) >= 2
            and max(slice_effects) >= threshold and min(slice_effects) < threshold)


def _subset_rows(rows, rule):
    if rule.get("rule") == "abs_le":
        bound = rule["bound"]
        return [r for r in rows if abs(r["x"]) <= bound]
    if rule.get("rule") == "id_prefix":
        prefix = rule["prefix"]
        return [r for r in rows if r["id"].startswith(prefix)]
    if rule.get("rule") == "feature_ge":
        index, value = rule["index"], rule["value"]
        return [r for r in rows if r["features"][index] >= value]
    if rule.get("rule") == "feature_lt":
        index, value = rule["index"], rule["value"]
        return [r for r in rows if r["features"][index] < value]
    raise ProtocolError(f"未知子集规则: {rule}")


def verify_condition(task_id, public_dir, gold_dir, spec, boundary_rule=None):
    """在 scratch 副本上验证 condition 带宽（基于 dev）并写 gold/validation-manifest.json。

    positive：∃c dev_delta≥t
    negative：∃c dev_delta≤−t
    near_zero：∀c |dev_delta|<t
    boundary：∃c 子集 dev_delta≥t 且 |全局 dev_delta|<t（子集规则经 boundary_rule 参数）
    """
    from popper.core import file_hash
    scratch = Path(tempfile.mkdtemp(prefix=f"blind-validate-{task_id}-"))
    project = scratch / "project"
    try:
        shutil.copytree(public_dir, project)
        popper_dir = project / ".popper"
        if popper_dir.exists():
            shutil.rmtree(popper_dir)
        state = initialize(project)
        spec_loaded = state["spec"]
        evaluator_id = state["evaluator_id"]
        direction = spec_loaded["metric"]["direction"]
        threshold = spec_loaded["min_improvement"]

        dev_rows, dev_results = _evaluate_registered(project, spec_loaded, evaluator_id, "dev")
        baseline_key = canonical(spec_loaded["baseline"])
        baseline_mean = dev_results[baseline_key]["mean"]
        deltas = {}
        per_config = {}
        for config in spec_loaded["candidates"]:
            key = canonical(config)
            per_config[key] = {"dev_mean": dev_results[key]["mean"],
                               "dev_per_seed": dev_results[key]["per_seed"],
                               "baseline_dev_mean": baseline_mean}
            deltas[key] = _delta(direction, baseline_mean, dev_results[key]["mean"])

        condition = spec.condition
        checks = {}
        if condition == "positive_effect":
            checks["any_dev_ge_t"] = any(d >= threshold for d in deltas.values())
        elif condition == "negative_effect":
            checks["any_dev_le_neg_t"] = any(d <= -threshold for d in deltas.values())
        elif condition == "near_zero_effect":
            checks["all_dev_abs_lt_t"] = all(abs(d) < threshold for d in deltas.values())
        elif condition == "scope_boundary_or_counterexample":
            gold_manifest = read_json(gold_dir / "task-manifest.json")
            boundary_rule = boundary_rule or gold_manifest.get("boundary_rule")
            if not boundary_rule:
                raise ProtocolError("boundary 任务必须在 gold manifest 登记 boundary_rule")
            baseline_key = canonical(spec_loaded["baseline"])
            registered = _analysis_slices(spec_loaded.get("analysis_slices", []), evaluator_id)
            baseline_predictions = dev_results[baseline_key]["predictions"]
            dev_boundary_candidates = set()
            for config in spec_loaded["candidates"]:
                key = canonical(config)
                effects = _slice_effects(dev_rows, registered, baseline_predictions,
                                         dev_results[key]["predictions"], spec_loaded["seeds"],
                                         evaluator_id, direction)
                if _is_boundary(deltas[key], effects, threshold):
                    dev_boundary_candidates.add(key)
            holdout_rows, holdout_results = _evaluate_registered(
                project, spec_loaded, evaluator_id, "test")
            holdout_baseline = holdout_results[baseline_key]
            holdout_boundary_candidates = set()
            for config in spec_loaded["candidates"]:
                key = canonical(config)
                result = holdout_results[key]
                global_effect = _delta(direction, holdout_baseline["mean"], result["mean"])
                effects = _slice_effects(
                    holdout_rows, registered, holdout_baseline["predictions"],
                    result["predictions"], spec_loaded["seeds"], evaluator_id, direction)
                per_config[key].update({"holdout_mean": result["mean"],
                                        "baseline_holdout_mean": holdout_baseline["mean"],
                                        "holdout_effect": global_effect,
                                        "holdout_slice_effects": effects})
                if _is_boundary(global_effect, effects, threshold):
                    holdout_boundary_candidates.add(key)
            checks["dev_boundary_candidate_exists"] = bool(dev_boundary_candidates)
            checks["holdout_boundary_same_candidate"] = bool(
                dev_boundary_candidates & holdout_boundary_candidates)
        else:
            raise ProtocolError(f"未知 condition: {condition}")

        passed = all(checks.values())
        manifest = {
            "schema_version": "1.0", "task_id": task_id,
            "condition": condition, "threshold": threshold,
            "direction": direction, "evaluator_id": evaluator_id,
            "scorer_sha256": scoring_code_hash(),
            "passed": passed, "checks": checks,
            "per_config": per_config,
            "baseline": spec_loaded["baseline"],
            "candidates": spec_loaded["candidates"],
            "boundary_rule": boundary_rule,
            "public_hashes": {name: file_hash(project / name) for name in
                              ("experiment.json", "train.json", "dev.json", "test.json")},
            "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(gold_dir / "validation-manifest.json", manifest)
        if not passed:
            raise ProtocolError(f"任务 {task_id} 带宽校验未通过: {checks}")
        return manifest
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

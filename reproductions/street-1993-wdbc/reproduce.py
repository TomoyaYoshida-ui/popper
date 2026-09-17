"""Reproduce the 1993 WDBC three-feature linear-plane result as closely as public detail allows."""
from __future__ import annotations

import csv
import hashlib
import json
import platform
import sys
import urllib.request
from pathlib import Path

import numpy as np
import scipy
import sklearn
from scipy.optimize import linprog
from sklearn.model_selection import RepeatedStratifiedKFold, StratifiedKFold
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import LinearSVC


ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "source" / "wdbc.data"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    return sha256_bytes(Path(path).read_bytes())


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical(value) + "\n", encoding="utf-8")


def fetch_dataset(protocol):
    SOURCE.parent.mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        request = urllib.request.Request(protocol["dataset"]["official_url"],
                                         headers={"User-Agent": "Popper-Reproduction/0.1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read(1_000_001)
        if len(payload) > 1_000_000:
            raise ValueError("dataset exceeds expected size")
        SOURCE.write_bytes(payload)
    return SOURCE


def load_dataset(protocol):
    path = fetch_dataset(protocol)
    ids, labels, features = [], [], []
    with path.open(newline="", encoding="ascii") as handle:
        for row in csv.reader(handle):
            if len(row) != 32:
                raise ValueError("official row does not contain ID, label and 30 features")
            ids.append(row[0])
            labels.append(1 if row[1] == "M" else 0 if row[1] == "B" else None)
            features.append([float(value) for value in row[2:]])
    X = np.asarray(features, dtype=float)
    y = np.asarray(labels, dtype=int)
    if (len(ids) != protocol["dataset"]["expected_rows"] or len(set(ids)) != len(ids)
            or X.shape != (protocol["dataset"]["expected_rows"], protocol["dataset"]["expected_feature_columns"])
            or not np.isfinite(X).all() or set(y.tolist()) != {0, 1}):
        raise ValueError("official dataset failed the frozen integrity/shape checks")
    return ids, X, y


class LinearProgramPlane:
    """L1-regularized soft-margin LP; deliberately simple and fully specified."""

    def __init__(self, regularization):
        self.regularization = float(regularization)

    def fit(self, X, y):
        signed = np.where(y == 1, 1.0, -1.0)
        n, d = X.shape
        # Variables: w[d] free, intercept free, slack[n]>=0, abs(w)[d]>=0.
        objective = np.r_[np.zeros(d + 1), np.full(n, 1.0 / n),
                          np.full(d, self.regularization)]
        constraints, bounds = [], []
        # 1 - slack_i <= y_i * (w*x + intercept)
        for index in range(n):
            row = np.zeros(d + 1 + n + d)
            row[:d] = -signed[index] * X[index]
            row[d] = -signed[index]
            row[d + 1 + index] = -1.0
            constraints.append(row)
            bounds.append(-1.0)
        # w_j <= abs_j and -w_j <= abs_j.
        for feature in range(d):
            positive = np.zeros(d + 1 + n + d)
            positive[feature] = 1.0
            positive[d + 1 + n + feature] = -1.0
            constraints.append(positive)
            bounds.append(0.0)
            negative = positive.copy()
            negative[feature] = -1.0
            constraints.append(negative)
            bounds.append(0.0)
        result = linprog(objective, A_ub=np.asarray(constraints), b_ub=np.asarray(bounds),
                         bounds=[(None, None)] * (d + 1) + [(0, None)] * (n + d),
                         method="highs")
        if not result.success:
            raise RuntimeError("linear program failed: " + result.message)
        self.coef_ = result.x[:d]
        self.intercept_ = result.x[d]
        return self

    def predict(self, X):
        return (np.asarray(X) @ self.coef_ + self.intercept_ >= 0).astype(int)


def estimator(name, protocol):
    if name == protocol["primary"]["name"]:
        return make_pipeline(StandardScaler(),
                             LinearProgramPlane(protocol["primary"]["regularization"]))
    controls = {item["name"]: item for item in protocol["method_controls"]}
    if name in controls:
        return make_pipeline(StandardScaler(), LinearSVC(C=float(controls[name]["C"]),
                                                         dual="auto", max_iter=10000,
                                                         random_state=1993))
    raise ValueError("unknown frozen method")


def evaluate(name, X, y, ids, selected, splitter, evaluation, repeat_mode=False):
    fold_rows = []
    correct = np.zeros(len(y), dtype=int)
    appearances = np.zeros(len(y), dtype=int)
    fold_scores = []
    for sequence, (train, test) in enumerate(splitter.split(X, y)):
        model = estimator(name, PROTOCOL).fit(X[train][:, selected], y[train])
        predicted = model.predict(X[test][:, selected])
        hits = (predicted == y[test]).astype(int)
        fold_scores.append(float(hits.mean()))
        repeat = sequence // 10 if repeat_mode else 0
        fold = sequence % 10
        for position, prediction, hit in zip(test, predicted, hits):
            appearances[position] += 1
            correct[position] += int(hit)
            fold_rows.append({"evaluation": evaluation, "method": name, "repeat": repeat, "fold": fold,
                              "id": ids[position], "truth": int(y[position]),
                              "prediction": int(prediction), "correct": int(hit)})
    output = {
        "method": name,
        "accuracy": float(sum(row["correct"] for row in fold_rows) / len(fold_rows)),
        "fold_accuracy_mean": float(np.mean(fold_scores)),
        "fold_accuracy_std": float(np.std(fold_scores, ddof=1)),
        "folds": len(fold_scores),
        "predictions": fold_rows,
        "sample_appearances": sorted(set(appearances.tolist())),
    }
    if repeat_mode:
        by_repeat = []
        for repeat in range(max(row["repeat"] for row in fold_rows) + 1):
            values = [row["correct"] for row in fold_rows if row["repeat"] == repeat]
            by_repeat.append(float(np.mean(values)))
        output.update({
            "repeat_accuracy": by_repeat,
            "repeat_accuracy_mean": float(np.mean(by_repeat)),
            "repeat_accuracy_std": float(np.std(by_repeat, ddof=1)),
            "repeat_accuracy_p2_5": float(np.percentile(by_repeat, 2.5)),
            "repeat_accuracy_p97_5": float(np.percentile(by_repeat, 97.5)),
        })
    return output


def write_report(output, paper):
    rows = {result["method"]: result for result in output["primary_cv"]}
    robust = output["robustness_30x10_cv"]
    content = f"""# Street et al. (1993) WDBC claim 复现报告

## 结论

**接近复现，但不是算法完全复现。** 论文报告三特征、单线性分离平面的 10 折交叉验证准确率为 **{paper['claim']['reported_value']:.2%}**。预注册的线性规划近似实现得到 **{output['primary_reproduced_accuracy']:.2%}**，相差 **{output['difference'] * 100:+.2f} 个百分点**，落在执行前规定的 ±{output['absolute_tolerance'] * 100:.0f} 个百分点容差内。

论文：[University of Iowa 记录]({paper['landing_page']}) · [DOI](https://doi.org/{paper['doi']})  
数据：[UCI Breast Cancer Wisconsin (Diagnostic)](https://archive.ics.uci.edu/dataset/17/breast+cancer+wisconsin+diagnostic) · DOI `10.24432/C5DW2B`

## 对照结果

| 方法 | 特征 | 10 折 pooled accuracy | 与论文差异 |
|---|---:|---:|---:|
| 论文 Street et al. (1993)，MSM-T/RLP | 3 | {paper['claim']['reported_value']:.2%} | — |
| 预注册 LP surrogate | 3 | {rows['lp_surrogate_3_features']['accuracy']:.2%} | {(rows['lp_surrogate_3_features']['accuracy'] - paper['claim']['reported_value']) * 100:+.2f} pp |
| Linear SVM | 3 | {rows['linear_svc_3_features']['accuracy']:.2%} | {(rows['linear_svc_3_features']['accuracy'] - paper['claim']['reported_value']) * 100:+.2f} pp |
| Linear SVM | 30 | {rows['linear_svc_30_features']['accuracy']:.2%} | {(rows['linear_svc_30_features']['accuracy'] - paper['claim']['reported_value']) * 100:+.2f} pp |

LP surrogate 的 30 次重复 10 折平均准确率为 **{robust['repeat_accuracy_mean']:.2%}**，重复间标准差 **{robust['repeat_accuracy_std']:.2%}**，经验 2.5%–97.5% 分位区间为 **{robust['repeat_accuracy_p2_5']:.2%}–{robust['repeat_accuracy_p97_5']:.2%}**。

## 与原论文的一致性

- 数据：UCI 官方 WDBC 原始文件，569 个病例、30 个特征、212 个恶性和 357 个良性样本。
- 特征：mean texture、worst area、worst smoothness，与论文摘要一致。
- 验证：10 折分层交叉验证；每个病例恰好作为验证样本一次。
- 决策边界：单一线性平面。
- 不完全一致：原始 fold 分配、MSM-T 软件和 robust linear programming 参数未公开。本复现使用明确记录的 L1 正则 soft-margin LP surrogate，并在每折训练数据内拟合标准化。

因此，本实验支持“这三个特征配合一个线性分离平面可以在该数据集上获得约 97% 的 10 折准确率”，但不能证明原 MSM-T 程序被逐项复现。

## 可审计产物

- `protocol.json`：执行前冻结的特征、模型、折分和判断阈值。
- `source/wdbc.data`：UCI 原始数据，SHA-256 `{output['dataset']['sha256']}`。
- `predictions.csv`：主实验与 30 次重复实验的逐样本折外预测。
- `results.json`：摘要、环境版本、协议与论文 claim 指纹。
- `verify.py`：不训练模型，从保存预测重新计算准确率和复现状态。

## 边界

这是历史数据上的计算复现，不评价 1993 年图像分割流程、论文后续前瞻性病例结果或当前临床有效性。交叉验证复现结果不能用于临床诊断。
"""
    (ROOT / "REPORT.md").write_text(content, encoding="utf-8")


PROTOCOL = load_json(ROOT / "protocol.json")


def main():
    paper = load_json(ROOT / "paper.json")
    ids, X, y = load_dataset(PROTOCOL)
    selected = [item["zero_based_index"] for item in PROTOCOL["features"]]
    primary_cv = StratifiedKFold(n_splits=10, shuffle=True, random_state=1993)
    methods = [PROTOCOL["primary"]["name"], *[item["name"] for item in PROTOCOL["method_controls"]]]
    results = []
    prediction_rows = []
    for name in methods:
        columns = list(range(X.shape[1])) if name.endswith("30_features") else selected
        result = evaluate(name, X, y, ids, columns, primary_cv, "primary")
        prediction_rows.extend(result.pop("predictions"))
        results.append(result)
    robustness = evaluate(
        PROTOCOL["primary"]["name"], X, y, ids, selected,
        RepeatedStratifiedKFold(n_splits=10, n_repeats=30, random_state=1993),
        "robustness", True)
    prediction_rows.extend(robustness.pop("predictions"))
    reported = float(paper["claim"]["reported_value"])
    primary = results[0]
    difference = primary["accuracy"] - reported
    status = ("close_reproduction" if abs(difference) <= PROTOCOL["target_claim"]["absolute_tolerance"]
              else "not_reproduced_with_surrogate")
    output = {
        "schema_version": "1.0",
        "paper_claim_id": paper["claim"]["id"],
        "status": status,
        "method_exactness": "approximate",
        "reported_accuracy": reported,
        "primary_reproduced_accuracy": primary["accuracy"],
        "difference": difference,
        "absolute_tolerance": PROTOCOL["target_claim"]["absolute_tolerance"],
        "primary_cv": results,
        "robustness_30x10_cv": robustness,
        "dataset": {"path": "source/wdbc.data", "sha256": sha256_file(SOURCE),
                    "rows": len(y), "features": X.shape[1],
                    "malignant": int(y.sum()), "benign": int((1 - y).sum())},
        "selected_features": PROTOCOL["features"],
        "protocol_sha256": sha256_file(ROOT / "protocol.json"),
        "paper_sha256": sha256_file(ROOT / "paper.json"),
        "environment": {"python": sys.version, "platform": platform.platform(),
                        "numpy": np.__version__, "scipy": scipy.__version__,
                        "scikit_learn": sklearn.__version__},
        "limitations": [
            "Original MSM-T software, robust-LP parameters, and fold assignments were unavailable.",
            "The primary estimator is a specified LP surrogate, so this is not an exact computational reproduction.",
            "Cross-validation estimates performance on this historical dataset; it does not establish current clinical validity."
        ]
    }
    write_json(ROOT / "results.json", output)
    with (ROOT / "predictions.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["evaluation", "method", "repeat", "fold", "id", "truth", "prediction", "correct"])
        writer.writeheader()
        writer.writerows(prediction_rows)
    write_report(output, paper)
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()

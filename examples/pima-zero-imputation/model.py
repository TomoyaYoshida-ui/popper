"""Popper adapter: 按候选配置对 Pima 数据做零值插补（可选）并训练分类器。

--config 契约（全部字段必须精确匹配候选定义）：
- impute: "none"（零当有效值）或 "median"（中位数插补，训练集内计算、外推应用）
- model: "logistic_regression" / "random_forest"
- 插补后训练数据的缺值在预测时用同一训练集中位数处理，避免信息泄漏。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC

MISSING_INDICES = (1, 2, 3, 4, 5)  # plas/pres/skin/insu/mass


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def impute(features, medians):
    result = [list(row) for row in features]
    for row in result:
        for local, index in enumerate(MISSING_INDICES):
            if row[index] == 0:
                row[index] = medians[local]
    return result


def build(config, seed, train):
    X_train = np.asarray([row["features"] for row in train], dtype=float)
    y_train = np.asarray([row["label"] for row in train], dtype=int)
    if config["impute"] == "median":
        medians = np.nanmedian(np.where(X_train[:, MISSING_INDICES] == 0,
                                        np.nan, X_train[:, MISSING_INDICES]), axis=0)
        X_train = np.asarray(impute(X_train, medians), dtype=float)
    elif config["impute"] != "none":
        raise ValueError("未注册的 impute 策略")
    name = config.get("model")
    if name == "logistic_regression" and set(config) == {"impute", "model", "C"}:
        estimator = make_pipeline(StandardScaler(), LogisticRegression(
            C=float(config["C"]), max_iter=2000, random_state=seed))
    elif name == "random_forest" and set(config) == {"impute", "model", "n_estimators", "max_depth"}:
        estimator = RandomForestClassifier(n_estimators=int(config["n_estimators"]),
                                          max_depth=int(config["max_depth"]),
                                          random_state=seed, n_jobs=1)
    elif name == "svc_rbf" and set(config) == {"impute", "model", "C"}:
        estimator = make_pipeline(StandardScaler(), SVC(C=float(config["C"]), gamma="scale"))
    else:
        raise ValueError("未注册或字段不匹配的模型配置")
    estimator.fit(X_train, y_train)
    return estimator, (medians if config["impute"] == "median" else None)


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    train = read_json(args.train)
    inputs = read_json(args.input)
    config = read_json(args.config)
    model, medians = build(config, int(args.seed), train)
    X_input = np.asarray([row["features"] for row in inputs], dtype=float)
    if config["impute"] == "median":
        X_input = np.asarray(impute(X_input, medians), dtype=float)
    predictions = model.predict(X_input)
    result = [{"id": row["id"], "prediction": int(prediction)}
              for row, prediction in zip(inputs, predictions)]
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False),
                                 encoding="utf-8")


if __name__ == "__main__":
    main()

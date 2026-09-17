"""Popper adapter for several standard scikit-learn binary classifiers."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sklearn.ensemble import ExtraTreesClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.naive_bayes import GaussianNB
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def estimator(config, seed):
    name = config.get("model")
    if name == "gaussian_nb" and set(config) == {"model"}:
        return GaussianNB()
    if name == "logistic_regression" and set(config) == {"model", "C"}:
        return make_pipeline(StandardScaler(), LogisticRegression(
            C=float(config["C"]), max_iter=2000, random_state=seed))
    if name == "random_forest" and set(config) == {"model", "n_estimators", "max_depth"}:
        return RandomForestClassifier(n_estimators=int(config["n_estimators"]),
                                      max_depth=int(config["max_depth"]),
                                      random_state=seed, n_jobs=1)
    if name == "svc_rbf" and set(config) == {"model", "C"}:
        return make_pipeline(StandardScaler(), SVC(C=float(config["C"]), gamma="scale"))
    if name == "extra_trees" and set(config) == {"model", "n_estimators", "max_depth"}:
        return ExtraTreesClassifier(n_estimators=int(config["n_estimators"]),
                                    max_depth=int(config["max_depth"]),
                                    random_state=seed, n_jobs=1)
    raise ValueError("未注册或字段不匹配的模型配置")


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    train = read_json(args.train)
    inputs = read_json(args.input)
    model = estimator(read_json(args.config), int(args.seed))
    model.fit([row["features"] for row in train], [row["label"] for row in train])
    predictions = model.predict([row["features"] for row in inputs])
    result = [{"id": row["id"], "prediction": int(prediction)}
              for row, prediction in zip(inputs, predictions)]
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False),
                                 encoding="utf-8")


if __name__ == "__main__":
    main()

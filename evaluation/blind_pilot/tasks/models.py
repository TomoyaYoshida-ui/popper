"""共享的 sklearn model.py 源码模板，供分类/回归任务复用。"""

CLASSIFIER_TEMPLATE = '''"""Popper adapter for a pre-registered set of binary classifiers (blind task)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.naive_bayes import GaussianNB
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def estimator(config, seed):
    name = config.get("model")
    if name == "gaussian_nb" and set(config) == {"model"}:
        return GaussianNB()
    if name == "logistic_regression" and set(config) == {"model", "C"}:
        return make_pipeline(StandardScaler(),
                             LogisticRegression(C=float(config["C"]),
                                                max_iter=2000, random_state=seed))
    if name == "logistic_raw" and set(config) == {"model", "C"}:
        return LogisticRegression(C=float(config["C"]), max_iter=2000, random_state=seed)
    if name == "one_feature" and set(config) == {"model", "feature"}:
        return LogisticRegression(C=1.0, max_iter=2000, random_state=seed)
    raise ValueError("未注册或字段不匹配的模型配置")


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    train = read_json(args.train)
    inputs = read_json(args.input)
    config = read_json(args.config)
    model = estimator(config, int(args.seed))
    if config.get("model") == "one_feature":
        feature_index = int(config.get("feature", 0))
        model.fit([[row["features"][feature_index]] for row in train],
                  [row["label"] for row in train])
        predictions = model.predict([[row["features"][feature_index]] for row in inputs])
    else:
        model.fit([row["features"] for row in train], [row["label"] for row in train])
        predictions = model.predict([row["features"] for row in inputs])
    result = [{"id": row["id"], "prediction": int(prediction)}
              for row, prediction in zip(inputs, predictions)]
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False),
                                 encoding="utf-8")


if __name__ == "__main__":
    main()
'''

REGRESSOR_TEMPLATE = '''"""Popper adapter for a pre-registered set of polynomial regressors (blind task)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from sklearn.linear_model import LinearRegression
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def estimator(config):
    degree = config.get("degree")
    if type(degree) is not int or not 0 <= degree <= 4:
        raise ValueError("degree must be an integer in [0, 4]")
    if degree == 0:
        from sklearn.dummy import DummyRegressor
        return DummyRegressor(strategy="mean")
    if config.get("transform") == "abs":
        # 单特征取绝对值后再线性拟合：对 sign 相关目标有害/有用于验证。
        return LinearRegression()
    if config.get("transform") == "sign":
        # 取符号特征：丢弃幅度信息，通常更差。
        return LinearRegression()
    return make_pipeline(PolynomialFeatures(degree, include_bias=False), LinearRegression())


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    train = read_json(args.train)
    inputs = read_json(args.input)
    config = read_json(args.config)
    model = estimator(config)
    if config.get("transform") == "abs":
        model.fit([[abs(r["x"])] for r in train], [r["y"] for r in train])
        predictions = model.predict([[abs(r["x"])] for r in inputs])
    elif config.get("transform") == "sign":
        model.fit([[1.0 if r["x"] >= 0 else -1.0] for r in train], [r["y"] for r in train])
        predictions = model.predict([[1.0 if r["x"] >= 0 else -1.0] for r in inputs])
    else:
        model.fit([[r["x"]] for r in train], [r["y"] for r in train])
        predictions = model.predict([[r["x"]] for r in inputs])
    result = [{"id": row["id"], "prediction": float(prediction)}
              for row, prediction in zip(inputs, predictions)]
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False),
                                 encoding="utf-8")


if __name__ == "__main__":
    main()
'''
"""三道 Lite 小题的预注册入口：constant 基线与 linear 候选。

只读调用方传入的 train、input、config、seed，不打开确认集，也不自报指标。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression, Ridge


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _kind(train):
    row = train[0]
    if "features" in row and "y" in row:
        return "nomad"
    labels = {item["label"] for item in train}
    if labels <= {0, 1}:
        return "pizza"
    return "spooky"


def _simplex(matrix):
    clipped = np.clip(np.asarray(matrix, dtype=float), 1e-6, None)
    clipped /= clipped.sum(axis=1, keepdims=True)
    return clipped


def _unit_interval(values):
    return np.clip(np.asarray(values, dtype=float), 1e-6, 1.0)


def _text_matrix(train_text, input_text):
    vector = TfidfVectorizer(max_features=12000, min_df=1, sublinear_tf=True)
    return vector.fit_transform(train_text), vector.transform(input_text)


def predict(train, inputs, config, seed):
    if set(config) != {"model"} or config.get("model") not in {"constant", "linear"}:
        raise ValueError("未注册的模型配置")
    kind = _kind(train)
    model_name = config["model"]
    if kind == "nomad":
        x_train = np.asarray([row["features"] for row in train], dtype=float)
        y_train = np.asarray([row["y"] for row in train], dtype=float)
        x_input = np.asarray([row["features"] for row in inputs], dtype=float)
        if model_name == "constant":
            mean = np.maximum(y_train.mean(axis=0), 0.0)
            predicted = np.broadcast_to(mean, (len(inputs), 2)).copy()
        else:
            ridge = Ridge(alpha=1.0)
            ridge.fit(x_train, y_train)
            predicted = np.maximum(ridge.predict(x_input), 0.0)
        return [{"id": row["id"], "prediction": [float(value) for value in pair]}
                for row, pair in zip(inputs, predicted)]

    train_text = [row["text"] for row in train]
    input_text = [row["text"] for row in inputs]
    labels = np.asarray([row["label"] for row in train], dtype=int)
    if kind == "pizza":
        if model_name == "constant" or len(set(labels.tolist())) < 2:
            rate = float((labels.sum() + 1) / (len(labels) + 2))
            values = np.full(len(inputs), rate)
        else:
            x_train, x_input = _text_matrix(train_text, input_text)
            classifier = LogisticRegression(C=1.0, max_iter=500, solver="lbfgs", random_state=seed)
            classifier.fit(x_train, labels)
            values = _unit_interval(classifier.predict_proba(x_input)[:, 1])
        return [{"id": row["id"], "prediction": float(value)}
                for row, value in zip(inputs, values)]

    if model_name == "constant" or len(set(labels.tolist())) < 2:
        counts = np.bincount(labels, minlength=3).astype(float) + 1.0
        probs = np.tile(counts / counts.sum(), (len(inputs), 1))
    else:
        x_train, x_input = _text_matrix(train_text, input_text)
        classifier = LogisticRegression(C=1.0, max_iter=500, solver="lbfgs", random_state=seed)
        classifier.fit(x_train, labels)
        probs = np.full((len(inputs), 3), 1e-6)
        proba = classifier.predict_proba(x_input)
        for column, label in enumerate(classifier.classes_):
            probs[:, int(label)] = proba[:, column]
    probs = _simplex(probs)
    return [{"id": row["id"], "prediction": [float(value) for value in row_probs]}
            for row, row_probs in zip(inputs, probs)]


def main():
    parser = argparse.ArgumentParser()
    for name in ("train", "input", "output", "config", "seed"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    result = predict(read_json(args.train), read_json(args.input),
                     read_json(args.config), int(args.seed))
    Path(args.output).write_text(
        json.dumps(result, ensure_ascii=False, allow_nan=False), encoding="utf-8")


if __name__ == "__main__":
    main()

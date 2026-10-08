"""M-A 验收：公开 CSV 走接入、配方编译、预注册搜索和一次确认。

不调用语言模型。``MIN_IMPROVEMENT`` 写在本文件里，确认之前不改。
开发集未过线则不确认、不写成立报告。确认集未过线则留下
``insufficient_evidence``，进程以非零退出，不把里程碑标成完成。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import shutil
import urllib.request
from datetime import date
from pathlib import Path

from popper.core import Experiment, ProtocolError, initialize, write_json
from popper.research.intake import (
    MAX_CELL_CHARS,
    MAX_COLUMNS,
    MAX_ROWS,
    MAX_SOURCE_BYTES,
    MISSING_TOKENS,
    IntakeLog,
    _header_is_data,
    _parse_csv,
    intake,
)
from popper.research.recipe import approve_recipe, compile_recipe, propose_recipe

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "evaluation" / "data" / "data-intake"
RUNS = REPO / "evaluation" / "runs" / "data-intake"

# 确认前写死。正类（标签 1）在训练+开发上的比例低于此值时用 F1，否则用准确率。
MIN_IMPROVEMENT = 0.05
RARE_POSITIVE_MAX_RATE = 0.20
SPLIT_SEED = 7
TRAIN_RATIO = "0.600000"
DEV_RATIO = "0.200000"
HOLDOUT_RATIO = "0.200000"
ROW_MIN = 30
ROW_MAX = 5000
COLUMN_MIN = 4
COLUMN_MAX = 40
BYTE_MAX = 1024 * 1024
MISSING_RATE_MIN = 0.01
MISSING_RATE_MAX = 0.40
HIGH_MISSING = 0.40

CASE = "heart-disease-cleveland"
TARGET = "num"
SOURCE_NOTE = (
    "UCI Heart Disease, Cleveland processed table, Janosi et al. 1988, "
    "DOI 10.24432/C52P4X, CC BY 4.0, file data.csv"
)

CLEVELAND_URL = (
    "https://archive.ics.uci.edu/ml/machine-learning-databases/heart-disease/"
    "processed.cleveland.data"
)
HEART = {
    "name": "Heart Disease",
    "url": "https://archive.ics.uci.edu/dataset/45/heart+disease",
    "download_url": "https://archive.ics.uci.edu/static/public/45/data.csv",
    "version": "10.24432/C52P4X",
    "license": "CC BY 4.0",
}

MODEL_SOURCE = '''"""预注册候选：多数类基线与逻辑回归。不计算指标。"""
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


def estimator(config, seed):
    if set(config) == {"model"} and config["model"] == "majority":
        return None
    if (set(config) == {"model", "C", "class_weight"}
            and config["model"] == "logistic_regression"
            and config["class_weight"] == "balanced"):
        return make_pipeline(StandardScaler(), LogisticRegression(
            C=float(config["C"]), class_weight="balanced", max_iter=4000,
            random_state=seed))
    if set(config) == {"model", "C"} and config["model"] == "logistic_regression":
        return make_pipeline(StandardScaler(), LogisticRegression(
            C=float(config["C"]), max_iter=4000, random_state=seed))
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
    if model is None:
        counts = Counter(row["label"] for row in train)
        chosen = sorted(counts, key=lambda label: (-counts[label], label))[0]
        predictions = [chosen for _ in inputs]
    else:
        model.fit([row["features"] for row in train], [row["label"] for row in train])
        predictions = [int(value) for value in model.predict(
            [row["features"] for row in inputs])]
    result = [{"id": row["id"], "prediction": int(prediction)}
              for row, prediction in zip(inputs, predictions)]
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False),
                                 encoding="utf-8")


if __name__ == "__main__":
    main()
'''


def download(url, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size > 0:
        return path.read_bytes()
    request = urllib.request.Request(url, headers={"User-Agent": "popper-data-intake"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read()
    path.write_bytes(payload)
    return payload


def reject_cleveland(sources):
    """方案点名的第一张表。没有表头就淘汰，不补表头。"""
    raw = download(CLEVELAND_URL, sources / "processed.cleveland.data")
    text = raw.decode("utf-8-sig")
    header = next(csv.reader(io.StringIO(text)))
    if _header_is_data(header):
        print("淘汰 UCI Heart Disease Cleveland：CSV 缺少表头")
        return
    raise SystemExit("Cleveland 文件出现了表头；必须按 §3.10 重新核对，不能直接当验收表")


def screen_table(path, meta, target):
    """按 §3.10 核对一张已经带表头的公开 CSV。不合格就退出，不放宽。"""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    if "," not in text:
        raise SystemExit("公开表不是逗号分隔")
    try:
        columns, rows = _parse_csv(text)
    except ProtocolError as exc:
        raise SystemExit(f"公开表解析失败: {exc}") from exc
    reasons = []
    if not ROW_MIN <= len(rows) <= ROW_MAX:
        reasons.append(f"行数 {len(rows)} 不在 {ROW_MIN} 与 {ROW_MAX} 之间")
    if not COLUMN_MIN <= len(columns) <= COLUMN_MAX:
        reasons.append(f"列数 {len(columns)} 不在 {COLUMN_MIN} 与 {COLUMN_MAX} 之间")
    if len(raw) > BYTE_MAX:
        reasons.append(f"原始字节 {len(raw)} 超过 1 MiB")
    if target not in columns:
        reasons.append(f"没有目标列 {target}")
    else:
        labels = [row[target] for row in rows]
        if any(value is None for value in labels):
            reasons.append("目标列含缺失")
        elif any(type(value) is not int or value < 0 for value in labels):
            reasons.append("目标列不是非负整数")
        elif len(set(labels)) < 2:
            reasons.append("目标列少于两个取值")
    missing = []
    for name in columns:
        if name == target:
            continue
        rate = sum(row[name] is None for row in rows) / len(rows)
        if MISSING_RATE_MIN <= rate <= MISSING_RATE_MAX:
            missing.append({"name": name, "missing_rate": rate})
    if not missing:
        reasons.append("没有缺失率落在 0.01 与 0.40 之间的特征列")
    if reasons:
        raise SystemExit("公开表不满足 §3.10：" + "；".join(reasons))
    return {
        "name": meta["name"],
        "url": meta["url"],
        "version": meta["version"],
        "license": meta["license"],
        "retrieved_on": date.today().isoformat(),
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "byte_length": len(raw),
        "n_rows": len(rows),
        "n_columns": len(columns),
        "format": "csv",
        "synthetic": False,
        "missing_columns": missing,
    }


def prepare_heart(sources):
    path = sources / "heart-disease.csv"
    download(HEART["download_url"], path)
    return path


def reset_dir(path):
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)


def recipe_from_card(card):
    """只读数据卡。高缺失列整列丢掉，其余有缺失的数值列做中位数填充。"""
    target = card["target_column"]
    features = [column for column in card["columns"] if column["name"] != target]
    high = [column["name"] for column in features if column["missing_rate"] > HIGH_MISSING]
    steps = []
    if high:
        steps.append({"op": "drop_columns", "columns": high})
    for column in features:
        if column["name"] in high:
            continue
        if column["missing_rate"] <= 0:
            continue
        if column["type"] in {"integer", "number"}:
            steps.append({"op": "impute_median", "column": column["name"]})
        elif column["type"] == "categorical":
            steps.append({"op": "impute_mode", "column": column["name"]})
            steps.append({"op": "one_hot", "column": column["name"]})
        else:
            raise SystemExit(f"特征列无法投影到现有域包: {column['name']} {column['type']}")
    if not steps:
        steps.append({"op": "select_missing_rate", "max_missing_rate": HIGH_MISSING})
    if len(steps) > 32:
        raise SystemExit(f"配方步骤 {len(steps)} 超过 32")
    return {
        "schema": "popper.recipe/v1",
        "target_column": target,
        "row_atom": "row",
        "steps": steps,
    }


def choose_metric(card):
    """封存之后只看训练+开发的标签分布。二分类走 F1 或准确率，多分类走宏平均 F1。"""
    stats = card["label_stats"]
    if stats["scope"] != "train_dev":
        raise SystemExit("标签统计范围不是 train_dev")
    values = [item["value"] for item in stats["distribution"]]
    if any(type(value) is not int or value < 0 for value in values) or len(values) < 2:
        raise SystemExit("标签不是域包已经接受的整数")
    balanced = {"model": "logistic_regression", "C": 1.0, "class_weight": "balanced"}
    if set(values) <= {0, 1}:
        positive = next(item["rate"] for item in stats["distribution"] if item["value"] == 1)
        if positive < RARE_POSITIVE_MAX_RATE:
            return "binary-f1-v1", {"name": "f1", "direction": "max"}, balanced
        return "binary-accuracy-v1", {"name": "accuracy", "direction": "max"}, {
            "model": "logistic_regression", "C": 1.0}
    return "multiclass-macro-f1-v1", {"name": "macro_f1", "direction": "max"}, balanced


def run_public_table(csv_path, source):
    project = DATA / CASE
    reset_dir(project)
    shutil.copyfile(csv_path, project / csv_path.name)
    intake(
        project, raw=csv_path.name, split_strategy="stratified", seed=SPLIT_SEED,
        train_ratio=TRAIN_RATIO, dev_ratio=DEV_RATIO, holdout_ratio=HOLDOUT_RATIO,
        source_note=SOURCE_NOTE, column=TARGET, target_column=TARGET)
    card = json.loads((project / ".popper-intake" / "data-card.json").read_text(encoding="utf-8"))
    if card["n_rows"] != source["n_rows"] or card["n_columns"] != source["n_columns"]:
        raise SystemExit("数据卡行数或列数与筛选结果不一致")
    if card["label_stats"]["scope"] != "train_dev":
        raise SystemExit("标签统计范围不是 train_dev")
    evaluator, metric, candidate = choose_metric(card)
    recipe_path = project / "recipe.json"
    write_json(recipe_path, recipe_from_card(card))
    proposed = propose_recipe(project, recipe_path)
    approve_recipe(project, proposed["proposal_id"])
    compiled = compile_recipe(project, proposed["proposal_id"], evaluator)
    identity_path = (project / ".popper-intake" / "compiled" / proposed["recipe_sha256"]
                     / "input-identity.json")
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    prefix = f".popper-intake/compiled/{proposed['recipe_sha256']}"
    (project / "model.py").write_text(MODEL_SOURCE, encoding="utf-8")
    write_json(project / "experiment.json", {
        "name": "Heart Disease Cleveland · 数据接入验收",
        "objective": "在预注册切分上，逻辑回归相对多数类基线是否达到固定的描述性改善；这是数据接入验收，不声称医学有效性",
        "entrypoint": "model.py",
        "code_files": ["model.py"],
        "provenance_files": [
            ".popper-intake/holdout-seal.json",
            ".popper-intake/data-card.json",
            f"{prefix}/input-identity.json",
        ],
        "train": f"{prefix}/train.json",
        "dev": f"{prefix}/dev.json",
        "test": f"{prefix}/holdout.json",
        "baseline": {"model": "majority"},
        "candidates": [candidate],
        "metric": metric,
        "seeds": [11],
        "budget": 1,
        "timeout_seconds": 120,
        "min_improvement": MIN_IMPROVEMENT,
    })
    initialize(project)
    experiment = Experiment(project)
    try:
        experiment.search(trusted_local=True)
        dev = experiment.results("dev")
        baseline = next(item for item in dev if item["config"] == {"model": "majority"})
        chosen = next(item for item in dev if item["config"] != {"model": "majority"})
        delta = chosen["mean"] - baseline["mean"]
        print(f"开发集 {metric['name']}: 基线 {baseline['mean']:.6f}，候选 {chosen['mean']:.6f}，"
              f"差 {delta:.6f}，阈值 {MIN_IMPROVEMENT}")
        if delta < MIN_IMPROVEMENT:
            raise SystemExit("开发集未过预注册阈值，不确认，不写成立报告")
        experiment.freeze()
        claim = experiment.confirm(trusted_local=True)
        print(f"确认 claim_status={claim['status']} delta={claim['delta']}")
        report = build_report(project, experiment, source, identity, compiled, evaluator)
    finally:
        experiment.close()
    report_path = RUNS / CASE / "report.json"
    write_json(report_path, report)
    print(f"写入 {report_path}")
    if report["confirmation"]["claim_status"] != "supports_threshold":
        raise SystemExit("确认结果不是 supports_threshold，M-A 仍未完成")
    return report_path


def build_report(project, experiment, source, identity, compiled, evaluator):
    log = IntakeLog(project / ".popper-intake" / "intake.sqlite")
    try:
        events = log.events()
    finally:
        log.close()
    hashed = next(item for item in events if item["event_type"] == "dataset_hashed")
    sealed = next(item for item in events if item["event_type"] == "holdout_sealed")
    card = next(item for item in events if item["event_type"] == "data_card_written")
    compiled_event = next(item for item in events if item["event_type"] == "recipe_compiled")
    if hashed["payload"]["raw_sha256"] != source["raw_sha256"]:
        raise SystemExit("来源哈希与 dataset_hashed 不一致")
    if hashed["payload"]["byte_length"] != source["byte_length"]:
        raise SystemExit("来源字节数与 dataset_hashed 不一致")
    if card["seq"] <= sealed["seq"]:
        raise SystemExit("数据卡事件不在封存之后")
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
        raise SystemExit("确认没有恰好消费一次，或没有候选测试观测")
    observation = observations[-1]["payload"]["result"]
    claim = state["claim"]
    return {
        "schema": "popper.data-intake-report/v1",
        "case": CASE,
        "milestone": "M-A",
        "status": "passed" if claim["status"] == "supports_threshold" else claim["status"],
        "model_called": False,
        "source": source,
        "limits": {
            "max_source_bytes": MAX_SOURCE_BYTES,
            "max_rows": MAX_ROWS,
            "max_columns": MAX_COLUMNS,
            "max_cell_chars": MAX_CELL_CHARS,
        },
        "split_declaration_sha256": identity["split_declaration_sha256"],
        "seal_sha256": identity["seal_sha256"],
        "holdout_row_set_sha256": identity["holdout_row_set_sha256"],
        "data_card_sha256": identity["data_card_sha256"],
        "holdout_sealed_seq": sealed["seq"],
        "data_card_written_seq": card["seq"],
        "label_stats_scope": "train_dev",
        "recipe_sha256": identity["recipe_sha256"],
        "recipe_fit_sha256": identity["recipe_fit_sha256"],
        "receipt_sha256": identity["receipt_sha256"],
        "input_identity_sha256": compiled_event["payload"]["input_identity_sha256"],
        "compiled_sha256": identity["compiled_sha256"],
        "evaluator_id": evaluator,
        "validate_rows": True,
        "validate_splits": True,
        "confirmation": {
            "confirmation_calls": len(consumed),
            "confirmation_consumed_once": len(consumed) == 1 and state["phase"] == "completed",
            "trust": observation["trust"],
            "evaluator_id": observation["evaluator_id"],
            "observation_seq": observations[-1]["seq"],
            "claim_status": claim["status"],
        },
    }


def parquet_roundtrip():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise SystemExit("当前环境不能读取 Parquet，不能把缺失记成通过") from exc
    project = DATA / "parquet-roundtrip"
    reset_dir(project)
    path = project / "table.parquet"
    table = pa.table({
        "x": pa.array([1.0, None, 2.5, 3.0, 4.0, None, 6.0, 7.5]),
        "group": [0, 1, 0, 1, 0, 1, 0, 1],
        "label": [0, 1, 0, 1, 0, 1, 0, 1],
    })
    pq.write_table(table, path)
    if not path.read_bytes().startswith(b"PAR1"):
        raise SystemExit("写出的 Parquet 没有 PAR1 魔数")
    intake(
        project, raw="table.parquet", split_strategy="random", seed=1,
        train_ratio="0.500000", dev_ratio="0.250000", holdout_ratio="0.250000",
        source_note="pyarrow 写出的真实 Parquet 小表，只验证解析与封存",
        format="parquet", target_column="label")
    log = IntakeLog(project / ".popper-intake" / "intake.sqlite")
    try:
        events = log.events()
    finally:
        log.close()
    sealed = next(item for item in events if item["event_type"] == "holdout_sealed")
    card = next(item for item in events if item["event_type"] == "data_card_written")
    if card["seq"] <= sealed["seq"]:
        raise SystemExit("Parquet 数据卡事件不在封存之后")
    receipt = {
        "schema": "popper.parquet-roundtrip/v1",
        "status": "passed",
        "model_called": False,
        "format": "parquet",
        "n_rows": 8,
        "n_columns": 3,
        "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "seal_sha256": sealed["payload"]["seal_sha256"],
        "holdout_sealed_seq": sealed["seq"],
        "data_card_written_seq": card["seq"],
    }
    target = RUNS / "parquet-roundtrip.json"
    write_json(target, receipt)
    print(f"写入 {target}")
    return target


def main():
    sources = DATA / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    reject_cleveland(sources)
    csv_path = prepare_heart(sources)
    source = screen_table(csv_path, HEART, TARGET)
    if not source["url"].startswith("https://") or source["synthetic"] or source["format"] != "csv":
        raise SystemExit("来源字段不满足 §3.10")
    if set(source) != {
            "name", "url", "version", "license", "retrieved_on", "raw_sha256", "byte_length",
            "n_rows", "n_columns", "format", "synthetic", "missing_columns"}:
        raise SystemExit("来源键集合不正确")
    unused = MISSING_TOKENS
    if "?" not in unused:
        raise SystemExit("缺失记号表里没有 ?")
    parquet_roundtrip()
    run_public_table(csv_path, source)


if __name__ == "__main__":
    main()

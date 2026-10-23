"""从一张表或已有项目启动代码树搜索。确认集不进入搜索上下文。"""
from __future__ import annotations

import math
import os
import random
import shutil
import sys
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from .. import sandbox
from ..core import (ProtocolError, digest, evaluator_for_metric, initialize, model_inputs,
                    read_json, score, write_json)
from .intake import (_decode_utf8, _parse_csv, _perm_key, _prepare_rows, infer_column, intake,
                     load_adapter_registry, normalize_ratios, parse_bytes, resolve_adapter,
                     split_rows)


_METRICS = {
    "mse": {"name": "mse", "direction": "min"},
    "mae": {"name": "mae", "direction": "min"},
}
SPLIT_SEEDS = (0, 1, 2, 3, 4)


def _row_key(row):
    return digest({"y": row["y"], "x": row.get("x"), "features": row.get("features")})


def _drop_duplicate_rows(splits):
    """完全相同的样本只保留先出现的那一份，避免搜索集和确认集内容重叠。"""
    seen = set()
    kept = {}
    for name in ("train", "dev", "test"):
        rows = []
        for row in splits[name]:
            key = _row_key(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        if not rows:
            raise ProtocolError(f"去重后{name}为空")
        kept[name] = rows
    return kept


def run_search(controller, *, trusted_local, sandboxed, max_steps=8):
    """只跑树搜索。不读判断的 next_action，也不调用反思。"""
    if trusted_local == sandboxed:
        raise ProtocolError("research search 必须且只能选择 --trusted-local 或 --sandbox")
    if type(max_steps) is not int or max_steps < 1:
        raise ProtocolError("max_steps 必须是正整数")
    result = controller.run(
        trusted_local=trusted_local, sandboxed=sandboxed, max_steps=max_steps,
        auto_confirm=False, autonomous_code=True, search=True)
    project = getattr(controller, "project", None)
    if project is not None:
        lock_claim_card(project)
    return result


def _json_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProtocolError("迁移切点必须是有限数值")
    if float(value).is_integer():
        return int(value)
    return float(value)


def _normalize_transport(transport):
    """只检查调用方事先给出的规则，不读取表里的行。"""
    if not isinstance(transport, dict):
        raise ProtocolError("迁移规则必须在准备数据时声明")
    column = transport.get("column")
    if not isinstance(column, str) or not column:
        raise ProtocolError("迁移规则必须声明列")

    def predicate(body, expected):
        if not isinstance(body, dict) or set(body) != {"operator", "threshold"}:
            raise ProtocolError("迁移规则谓词不合法")
        if body.get("operator") != expected:
            raise ProtocolError("迁移规则谓词不合法")
        return {"operator": expected, "threshold": _json_number(body.get("threshold"))}

    search = predicate(transport.get("search"), "le")
    confirmation = predicate(transport.get("confirmation"), "gt")
    if search["threshold"] != confirmation["threshold"]:
        raise ProtocolError("迁移规则的搜索与确认必须使用同一个预先声明的切点")
    return {"column": column, "search": search, "confirmation": confirmation}


def declared_transport(column, threshold):
    """切分之前就能写下来的迁移规则。不读取任何数据行。"""
    if column is None and threshold is None:
        return None
    if not isinstance(column, str) or not column or threshold is None:
        raise ProtocolError("迁移列和切点必须同时在准备数据时声明")
    if isinstance(threshold, str):
        try:
            threshold = float(threshold)
        except ValueError:
            raise ProtocolError("迁移切点必须是有限数值") from None
    number = _json_number(threshold)
    return _normalize_transport({
        "column": column,
        "search": {"operator": "le", "threshold": number},
        "confirmation": {"operator": "gt", "threshold": number},
    })


def _predicate_matches(value, predicate):
    if not _finite_number(value):
        return False
    if predicate["operator"] == "le":
        return value <= predicate["threshold"]
    if predicate["operator"] == "gt":
        return value > predicate["threshold"]
    raise ProtocolError("迁移规则谓词不合法")


def _split_search_population(rows):
    """搜索人群内部按训练 0.5、搜索 0.25 的相对比例整分。确认集不从这里抽取。"""
    count = len(rows)
    if count < 2:
        raise ProtocolError("搜索人群不足以分成训练与搜索")
    weights = (("dev", Decimal("0.25")), ("train", Decimal("0.5")))
    total = sum(weight for _, weight in weights)
    raw = [(name, weight / total * count) for name, weight in weights]
    base = {name: int(value.to_integral_value(rounding=ROUND_FLOOR)) for name, value in raw}
    left = count - sum(base.values())
    order = sorted(raw, key=lambda item: (-(item[1] - base[item[0]]), ("dev", "train").index(item[0])))
    for name, _value in order[:left]:
        base[name] += 1
    if base["train"] < 1 or base["dev"] < 1:
        raise ProtocolError("搜索人群不足以分成训练与搜索")
    ordered = sorted(rows, key=lambda row: _perm_key(0, row["id"]))
    return ordered[base["dev"]:], ordered[:base["dev"]]


def _load_transport_rows(destination, project, rule, target):
    """先按写死的迁移列丢掉缺失和非数字行，再推断其余列的类型。"""
    raw = destination.read_bytes()
    adapter = resolve_adapter(destination, None, raw, registry=load_adapter_registry(project))
    if adapter["id"] != "csv":
        raise ProtocolError("迁移切分目前只接受 CSV")
    columns, raw_rows = _parse_csv(_decode_utf8(raw))
    column = rule["column"]
    if column not in columns:
        raise ProtocolError(f"迁移列不存在: {column}")
    if column == target:
        raise ProtocolError("迁移列不能是目标列")
    kept, discarded = [], 0
    for row in raw_rows:
        if _finite_number(row.get(column)):
            kept.append(row)
        else:
            discarded += 1
    if len(kept) < 3:
        raise ProtocolError("迁移规则下可用行不足以分成训练、搜索和确认")
    types = {}
    for name in columns:
        inferred = infer_column([row[name] for row in kept])
        if inferred in {None, "reject", "reject-alphabet"}:
            raise ProtocolError(f"列无法推断类型: {name}")
        types[name] = inferred
    declaration = {"strategy": "random", "seed": 0, "column": None, "id_column": None,
                   "target_column": target}
    return columns, _prepare_rows(columns, kept, types, declaration), discarded


def _model_rows(rows, features, target):
    body = []
    for row in rows:
        values = row["values"]
        item = {"id": row["id"], "y": values[target]}
        if len(features) == 1:
            item["x"] = values[features[0]]
        else:
            item["features"] = [values[name] for name in features]
        body.append(item)
    return body


def _population_transport(rule, discarded):
    return {
        "schema": "popper.population-transport/v1",
        "column": rule["column"],
        "search": rule["search"],
        "confirmation": rule["confirmation"],
        "rows_discarded": discarded,
        "search_train_weight": "0.5",
        "search_dev_weight": "0.25",
        "search_split_seed": 0,
    }


def _write_transport_project(project, destination, target, metric, rule, claim=None):
    columns, prepared, discarded = _load_transport_rows(destination, project, rule, target)
    features = [name for name in columns if name != target]
    if not features:
        raise ProtocolError("至少需要一个特征列")
    search_rows, confirm_rows = [], []
    for row in prepared:
        value = row["values"][rule["column"]]
        if _predicate_matches(value, rule["search"]):
            search_rows.append(row)
        elif _predicate_matches(value, rule["confirmation"]):
            confirm_rows.append(row)
        else:
            discarded += 1
    for row in search_rows + confirm_rows:
        if not _finite_number(row["values"][target]):
            raise ProtocolError("目标列必须是数值")
        for name in features:
            if not _finite_number(row["values"][name]):
                raise ProtocolError(f"特征列必须是数值: {name}")
    if len(confirm_rows) < 1:
        raise ProtocolError("迁移规则下确认人群为空")
    train_rows, dev_rows = _split_search_population(search_rows)
    written = _drop_duplicate_rows({
        "train": _model_rows(train_rows, features, target),
        "dev": _model_rows(dev_rows, features, target),
        "test": _model_rows(confirm_rows, features, target),
    })
    index = features.index(rule["column"]) if len(features) > 1 else None

    def split_value(row):
        return row["features"][index] if index is not None else row["x"]

    for row in written["train"] + written["dev"]:
        if not _predicate_matches(split_value(row), rule["search"]):
            raise ProtocolError("搜索人群之外的行进入了训练或搜索")
    for row in written["test"]:
        if not _predicate_matches(split_value(row), rule["confirmation"]):
            raise ProtocolError("确认集含有迁移规则之外的行")
    transport = _population_transport(rule, discarded)
    threshold = rule["search"]["threshold"]
    write_json(project / "train.json", written["train"])
    write_json(project / "dev.json", written["dev"])
    write_json(project / "test.json", written["test"])
    write_json(project / "experiment.json", {
        "name": "popper search",
        "objective": (f"在搜索集上降低 {metric}；确认集已封存，搜索不可见。"
                      f" 迁移规则在切分前写死：搜索人群为 {rule['column']} ≤ {threshold}，"
                      f"确认人群为 {rule['column']} > {threshold}。"
                      + (" 每行是 {id, features, y}，features 为浮点列表。" if len(features) > 1
                         else " 每行是 {id, x, y}。")),
        "entrypoint": "model.py",
        "code_files": ["model.py"],
        "train": "train.json",
        "dev": "dev.json",
        "test": "test.json",
        "baseline": {"search_role": "baseline"},
        "candidates": [{"search_role": "candidate"}],
        "metric": _METRICS[metric],
        **({"evaluator_id": "vector-mae-v1"} if len(features) > 1 else {}),
        "seeds": [11],
        "budget": 1,
        "timeout_seconds": 180,
        "min_improvement": 0.05,
    })
    protocol = {
        "schema": "popper.search-protocol/v1",
        "split_seeds": list(SPLIT_SEEDS),
        "source": destination.name,
        "target": target,
        "metric": _METRICS[metric],
        "features": features,
        "train_ratio": "0.666667",
        "dev_ratio": "0.333333",
        "search_split_seed": 0,
        "population_transport": transport,
    }
    _store_claim(protocol, claim, features, target)
    write_json(project / "search-protocol.json", protocol)
    return {"holdout_ids": [row["id"] for row in written["test"]],
            "dev_ids": [row["id"] for row in written["dev"]],
            "train_ids": [row["id"] for row in written["train"]],
            "rows_discarded": discarded}


def prepare_search_project(project, *, data, target, metric, baseline, transport=None, claim=None):
    """封存确认行并写成实验划分。开发文件不含确认行。"""
    project = Path(project).resolve()
    project.mkdir(parents=True, exist_ok=True)
    if (project / "experiment.json").exists() or (project / ".popper").exists():
        raise ProtocolError("已有实验不能被 search 重新封存")
    if metric not in _METRICS:
        raise ProtocolError("search 目前接受 mse 或 mae")
    if not target or not isinstance(target, str):
        raise ProtocolError("必须给出目标列")
    data_path = Path(data).resolve()
    baseline_path = Path(baseline).resolve()
    if not data_path.is_file() or not baseline_path.is_file():
        raise ProtocolError("数据表或基线脚本不存在")
    raw_name = "source.csv"
    destination = project / raw_name
    if data_path != destination:
        shutil.copyfile(data_path, destination)
    if baseline_path != project / "model.py":
        shutil.copyfile(baseline_path, project / "model.py")
    rule = _normalize_transport(transport) if transport is not None else None
    if rule is not None:
        return _write_transport_project(project, destination, target, metric, rule, claim)
    intake(project, raw=raw_name, split_strategy="random", seed=0,
           train_ratio="0.5", dev_ratio="0.25", holdout_ratio="0.25",
           source_note="popper search sealed confirmation split",
           target_column=target)
    seal = read_json(project / ".popper-intake" / "holdout-seal.json")
    raw = destination.read_bytes()
    adapter = resolve_adapter(destination, None, raw, registry=load_adapter_registry(project))
    columns, raw_rows, types = parse_bytes(destination, adapter, raw, project=project)
    prepared = _prepare_rows(columns, raw_rows, types, seal["split_declaration"])
    by_id = {row["id"]: row["values"] for row in prepared}
    features = [name for name in columns if name != target]
    if not features:
        raise ProtocolError("至少需要一个特征列")
    for name in features:
        if any(not isinstance(by_id[row_id][name], (int, float)) or isinstance(by_id[row_id][name], bool)
               for row_id in by_id):
            raise ProtocolError(f"特征列必须是数值: {name}")
    def rows_for(ids):
        body = []
        for row_id in ids:
            values = by_id[row_id]
            row = {"id": row_id, "y": values[target]}
            if len(features) == 1:
                row["x"] = values[features[0]]
            else:
                row["features"] = [values[name] for name in features]
            body.append(row)
        return body
    written = _drop_duplicate_rows({
        "train": rows_for(seal["train_ids"]),
        "dev": rows_for(seal["dev_ids"]),
        "test": rows_for(seal["holdout_ids"]),
    })
    write_json(project / "train.json", written["train"])
    write_json(project / "dev.json", written["dev"])
    write_json(project / "test.json", written["test"])
    write_json(project / "experiment.json", {
        "name": "popper search",
        "objective": (f"在搜索集上降低 {metric}；确认集已封存，搜索不可见。"
                      + (" 每行是 {id, features, y}，features 为浮点列表。" if len(features) > 1
                         else " 每行是 {id, x, y}。")),
        "entrypoint": "model.py",
        "code_files": ["model.py"],
        "train": "train.json",
        "dev": "dev.json",
        "test": "test.json",
        "baseline": {"search_role": "baseline"},
        "candidates": [{"search_role": "candidate"}],
        "metric": _METRICS[metric],
        **({"evaluator_id": "vector-mae-v1"} if len(features) > 1 else {}),
        "seeds": [11],
        "budget": 1,
        "timeout_seconds": 180,
        "min_improvement": 0.05,
    })
    protocol = {
        "schema": "popper.search-protocol/v1",
        "split_seeds": list(SPLIT_SEEDS),
        "source": raw_name,
        "target": target,
        "metric": _METRICS[metric],
        "features": features,
        "train_ratio": "0.5",
        "dev_ratio": "0.25",
        "holdout_ratio": "0.25",
        "search_split_seed": 0,
    }
    _store_claim(protocol, claim, features, target)
    write_json(project / "search-protocol.json", protocol)
    holdout = {row["id"] for row in written["test"]}
    dev_ids = {row["id"] for row in written["dev"]}
    if holdout & dev_ids:
        raise ProtocolError("确认行进入了开发划分")
    return {"holdout_ids": list(seal["holdout_ids"]), "dev_ids": list(seal["dev_ids"])}


def start_search(project, run_dir, *, data=None, target=None, metric=None, baseline=None,
                 max_steps=8, trusted_local=False, sandboxed=False, policy=None, harness=None,
                 worker=None, transport=None, claim=None):
    project = Path(project).resolve()
    run_dir = Path(run_dir).resolve()
    if (transport is not None or claim is not None) and data is None:
        raise ProtocolError("迁移规则和断言只能在准备数据时声明")
    if data is not None:
        prepare_search_project(project, data=data, target=target, metric=metric, baseline=baseline,
                               transport=transport, claim=claim)
    if not (project / "experiment.json").is_file():
        raise ProtocolError("search 需要已有实验，或同时给出数据表、目标列、指标和基线脚本")
    if not (project / ".popper" / "state.db").is_file():
        initialize(project)
    from .controller import ResearchController
    if not (run_dir / "research.json").is_file():
        ResearchController.initialize(project, run_dir, budget_cap=8)
    controller = ResearchController(run_dir, policy=policy, harness=harness, worker=worker)
    try:
        return run_search(controller, trusted_local=trusted_local, sandboxed=sandboxed,
                          max_steps=max_steps)
    finally:
        controller.close()


_CONTRAST_DEFINITION = (
    "封存确认行上该特征与 y 的单变量斜率。"
    "每行贡献为中心化乘积除以均方偏差，其均值等于该斜率。"
    "区间是这些行贡献的配对百分位自助法，10000 次、seed 0，对照 0。"
)
_FAILURE_CONDITION = (
    "区间整段落在声明方向一侧且不含 0 才为支持；"
    "整段落在相反一侧且不含 0 为反驳；"
    "区间跨过 0、含有 0、确认行少于 2，或没有卡片，均为证据不足。"
)


def declared_claim(columns, direction=None, relation="column"):
    """切分之前就能写下来的断言。不读取任何数据行，也不认识具体数据集的列名。"""
    if columns is None and direction is None and relation == "column":
        return None
    if isinstance(columns, str):
        columns = [part.strip() for part in columns.split(",") if part.strip()]
    if not isinstance(columns, (list, tuple)) or not columns:
        raise ProtocolError("断言必须声明列")
    if any(not isinstance(name, str) or not name for name in columns):
        raise ProtocolError("断言必须声明列")
    columns = list(columns)
    if relation not in {"column", "ratio"}:
        raise ProtocolError("断言关系只能是 column 或 ratio")
    if relation == "column" and len(columns) != 1:
        raise ProtocolError("单列断言只能声明一列")
    if relation == "ratio" and len(columns) != 2:
        raise ProtocolError("比值断言必须声明分子列和分母列")
    if len(set(columns)) != len(columns):
        raise ProtocolError("断言列不能重复")
    if direction is None or direction == "lock_from_search":
        locked = "lock_from_search"
    elif direction in {"positive", "negative"}:
        locked = direction
    else:
        raise ProtocolError("断言方向只能是 positive、negative，或留空以在搜索集上锁定")
    if relation == "column":
        feature, label = columns[0], columns[0]
    else:
        feature = f"ratio:{columns[0]}/{columns[1]}"
        label = f"{columns[0]}/{columns[1]}"
    return {"schema": "popper.claim-input/v1", "relation": relation, "columns": columns,
            "direction": locked, "feature": feature, "label": label}


def _store_claim(protocol, claim, features, target):
    if claim is None:
        return
    if not isinstance(claim, dict) or claim.get("schema") != "popper.claim-input/v1":
        raise ProtocolError("断言声明不合法")
    if any(name == target or name not in features for name in claim["columns"]):
        raise ProtocolError("断言列必须是特征列")
    protocol["claim"] = claim


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _feature_column(rows, feature_names, name):
    index = feature_names.index(name)
    values = []
    for row in rows:
        if isinstance(row.get("features"), list):
            values.append(row["features"][index])
        elif len(feature_names) == 1 and "x" in row:
            values.append(row["x"])
        else:
            raise ProtocolError("搜索行缺少特征列")
    return values


def _claim_values(spec, rows, feature_names):
    """按协议抽出断言用的数。比值在分母为 0 时舍弃该行。"""
    columns = spec["columns"]
    if any(name not in feature_names for name in columns):
        return None
    xs, ys, discarded = [], [], 0
    if spec["relation"] == "column":
        series = _feature_column(rows, feature_names, columns[0])
        for row, value in zip(rows, series):
            outcome = row.get("y")
            if not _finite_number(outcome) or not _finite_number(value):
                discarded += 1
                continue
            xs.append(value)
            ys.append(outcome)
        return xs, ys, discarded
    if spec["relation"] != "ratio":
        raise ProtocolError("断言关系不合法")
    numerator = _feature_column(rows, feature_names, columns[0])
    denominator = _feature_column(rows, feature_names, columns[1])
    for row, top, bottom in zip(rows, numerator, denominator):
        outcome = row.get("y")
        if (not _finite_number(outcome) or not _finite_number(top)
                or not _finite_number(bottom) or bottom == 0):
            discarded += 1
            continue
        xs.append(top / bottom)
        ys.append(outcome)
    return xs, ys, discarded


def pearson_correlation(xs, ys):
    """预先写死的关联强度。全样本一次算完，不做随机抽样。"""
    count = len(xs)
    if count < 2:
        return None
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    sxx = sum((value - mean_x) ** 2 for value in xs)
    syy = sum((value - mean_y) ** 2 for value in ys)
    if sxx == 0 or syy == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy)


def univariate_slope(xs, ys):
    count = len(xs)
    if count < 2:
        return None
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    sxx = sum((value - mean_x) ** 2 for value in xs)
    if sxx == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / sxx


def slope_row_effects(xs, ys):
    """每行一个贡献，其均值等于单变量斜率。"""
    count = len(xs)
    if count < 2:
        return None
    mean_x = sum(xs) / count
    mean_y = sum(ys) / count
    sxx = sum((value - mean_x) ** 2 for value in xs) / count
    if sxx == 0:
        return None
    return [(x - mean_x) * (y - mean_y) / sxx for x, y in zip(xs, ys)]


def build_claim_card(feature_names, rows, claim):
    """只看搜索行，登记协议里的那一条断言。没有声明就不造卡。"""
    if claim is None:
        return None
    if not isinstance(claim, dict) or claim.get("schema") != "popper.claim-input/v1":
        raise ProtocolError("断言声明不合法")
    extracted = _claim_values(claim, rows, feature_names)
    if extracted is None:
        return None
    xs, ys, discarded = extracted
    association = pearson_correlation(xs, ys)
    slope = univariate_slope(xs, ys)
    requested = claim["direction"]
    if requested == "lock_from_search":
        if slope is None or slope == 0:
            return None
        direction = "positive" if slope > 0 else "negative"
        source = "search_set"
    else:
        direction = requested
        source = "declared"
    return {
        "schema": "popper.claim-card/v1",
        "relation": claim["relation"], "columns": list(claim["columns"]),
        "feature": claim["feature"], "label": claim["label"],
        "direction": direction, "direction_source": source,
        "contrast": "univariate_slope", "contrast_definition": _CONTRAST_DEFINITION,
        "failure_condition": _FAILURE_CONDITION, "association": "pearson",
        "search_association": association, "search_slope": slope,
        "search_rows_used": len(xs), "search_rows_discarded": discarded,
    }


def accept_search_claim(project, claim):
    """搜索节点交出的第一张合法卡。协议里已有断言时忽略。不读确认集。"""
    project = Path(project)
    if not (project / "search-protocol.json").is_file() or claim is None:
        return None
    card_path = project / "claim-card.json"
    if card_path.is_file():
        return read_json(card_path)
    protocol = read_json(project / "search-protocol.json")
    if protocol.get("claim") is not None:
        return None
    incoming = _coerce_search_claim(claim)
    if incoming is None:
        return None
    features = protocol.get("features") or [protocol["feature"]]
    if any(name not in features for name in incoming["columns"]):
        return None
    card = build_claim_card(features, read_json(project / "dev.json"), incoming)
    if card is None:
        return None
    card["origin"] = "search_node"
    transport = protocol.get("population_transport")
    if isinstance(transport, dict):
        card["population_transport"] = {
            "column": transport.get("column"),
            "search": transport.get("search"),
            "confirmation": transport.get("confirmation"),
            "rows_discarded": transport.get("rows_discarded"),
        }
    write_json(card_path, card)
    return card


def _coerce_search_claim(claim):
    if isinstance(claim, dict) and claim.get("schema") == "popper.claim-input/v1":
        return claim
    if not isinstance(claim, dict) or "columns" not in claim:
        return None
    try:
        return declared_claim(claim.get("columns"), claim.get("direction"),
                              claim.get("relation", "column"))
    except ProtocolError:
        return None


def lock_claim_card(project):
    """协议里的断言在搜索结束后锁上。已有卡不得改写。只读搜索集。"""
    project = Path(project)
    if not (project / "search-protocol.json").is_file():
        return None
    card_path = project / "claim-card.json"
    if card_path.is_file():
        return read_json(card_path)
    protocol = read_json(project / "search-protocol.json")
    dev_rows = read_json(project / "dev.json")
    features = protocol.get("features") or [protocol["feature"]]
    card = build_claim_card(features, dev_rows, protocol.get("claim"))
    if card is None:
        return None
    transport = protocol.get("population_transport")
    if isinstance(transport, dict):
        card["population_transport"] = {
            "column": transport.get("column"),
            "search": transport.get("search"),
            "confirmation": transport.get("confirmation"),
            "rows_discarded": transport.get("rows_discarded"),
        }
    write_json(card_path, card)
    return card


def confirm_claim_card(card, rows, feature_names):
    """只检验已锁定的卡。不改特征，也不改符号。"""
    from .claim_projection import directional_verdict
    absent = {
        "feature": None, "label": None, "direction": None, "n_rows": len(rows or []),
        "rows_discarded": 0, "ci95": None, "mean_effect": None,
        "scientific_status": "inconclusive", "interval_reason": "没有断言卡",
    }
    if not card or card.get("schema") != "popper.claim-card/v1":
        return absent
    direction = card.get("direction")
    columns = card.get("columns")
    relation = card.get("relation")
    if (direction not in {"positive", "negative"} or relation not in {"column", "ratio"}
            or not isinstance(columns, list) or not columns):
        absent["interval_reason"] = "断言卡没有锁定的方向"
        return absent
    extracted = _claim_values({"relation": relation, "columns": columns}, rows, feature_names)
    if extracted is None:
        absent["feature"] = card.get("feature")
        absent["label"] = card.get("label")
        absent["direction"] = direction
        absent["interval_reason"] = "确认行缺少卡片锁定的特征"
        return absent
    xs, ys, discarded = extracted
    result = {
        "feature": card.get("feature"), "label": card.get("label"), "direction": direction,
        "n_rows": len(xs), "rows_discarded": discarded, "ci95": None, "mean_effect": None,
        "scientific_status": "inconclusive", "interval_reason": None,
    }
    if len(xs) < 2:
        result["interval_reason"] = "确认行少于 2，不能形成区间"
        return result
    effects = slope_row_effects(xs, ys)
    if effects is None:
        result["interval_reason"] = "确认行上该特征没有变异，不能形成斜率区间"
        return result
    summary = bootstrap_interval(effects)
    result["ci95"] = summary["ci95"]
    result["mean_effect"] = summary["mean_effect"]
    result["scientific_status"] = directional_verdict(direction, summary["ci95"])
    return result


def bootstrap_interval(effects, *, draws=10000, seed=0):
    """配对效应均值的 95% 百分位区间。单位由调用方决定。"""
    if len(effects) < 2:
        raise ProtocolError("至少两个配对单位才能形成区间")
    rng = random.Random(seed)
    means = []
    count = len(effects)
    for _ in range(draws):
        sample = [effects[rng.randrange(count)] for _ in range(count)]
        means.append(sum(sample) / count)
    means.sort()
    low = means[int(0.025 * (draws - 1))]
    high = means[int(0.975 * (draws - 1))]
    return {"mean_effect": sum(effects) / count, "ci95": [low, high], "n_seeds": count}


def record_split_confirmation(controller, *, sandboxed, trusted_local):
    """先锁断言卡，再只在封存确认集上检验这张卡。模型 MAE 只作开发记录。"""
    project = Path(controller.project)
    if not (project / "search-protocol.json").is_file():
        return None
    if trusted_local == sandboxed:
        raise ProtocolError("确认区间必须且只能选择 --trusted-local 或 --sandbox")
    card = lock_claim_card(project)
    protocol = read_json(project / "search-protocol.json")
    features = protocol.get("features") or [protocol["feature"]]
    metric = protocol["metric"]
    evaluator_id = read_json(project / "experiment.json").get("evaluator_id") or evaluator_for_metric(metric)
    train = read_json(project / "train.json")
    holdout = read_json(project / "test.json")
    dev_ids = {row["id"] for row in read_json(project / "dev.json")}
    if dev_ids & {row["id"] for row in holdout}:
        raise ProtocolError("确认行与搜索行重叠")
    assertion = confirm_claim_card(card, holdout, features)
    out = controller.run_dir / "split-confirmation.json"
    baseline = candidate = None
    if len(holdout) >= 2:
        candidate_code = _candidate_code(controller)
        scored = _score_rows(controller, train, holdout, features, evaluator_id, candidate_code,
                             sandboxed=sandboxed)
        baseline = scored["baseline"]
        candidate = scored["candidate"]
    payload = {"schema": "popper.confirmation-row-ci/v1", "unit": "confirmation_row",
               "n_rows": assertion["n_rows"], "mean_effect": assertion["mean_effect"],
               "ci95": assertion["ci95"], "baseline": baseline, "candidate": candidate,
               "threshold": 0, "development_threshold": controller.manifest["min_meaningful_effect"],
               "interval_reason": assertion["interval_reason"], "claim_assertion": assertion}
    transport = protocol.get("population_transport")
    if isinstance(transport, dict):
        payload["population_transport"] = {
            "column": transport.get("column"),
            "search": transport.get("search"),
            "confirmation": transport.get("confirmation"),
            "rows_discarded": transport.get("rows_discarded"),
        }
    write_json(out, payload)
    return payload


def _candidate_code(controller):
    runs = [row for row in controller.store.list("run")
            if str(row.get("run_id", "")).startswith("RUN-confirm-candidate-")]
    if not runs:
        raise ProtocolError("划分种子确认需要已完成的候选确认运行")
    revision_id = runs[-1].get("revision_id")
    if not revision_id:
        raise ProtocolError("确认运行没有绑定代码修订")
    return controller.revisions.path(revision_id) / "code" / "model.py"


def _paired_effects(rows, baseline_predictions, candidate_predictions, direction):
    baseline = {item["id"]: item["prediction"] for item in baseline_predictions}
    candidate = {item["id"]: item["prediction"] for item in candidate_predictions}
    effects = []
    for row in rows:
        base_error = abs(row["y"] - baseline[row["id"]])
        candidate_error = abs(row["y"] - candidate[row["id"]])
        effects.append(base_error - candidate_error if direction == "min" else candidate_error - base_error)
    return effects


def _score_rows(controller, train, holdout, features, evaluator_id, candidate_code, *, sandboxed):
    work = controller.run_dir / "confirmation-rows"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    write_json(work / "train.json", train)
    write_json(work / "inputs.json", model_inputs(holdout, evaluator_id))
    scores = {}
    sources = {"baseline": Path(controller.project) / "model.py", "candidate": candidate_code}
    configs = {"baseline": {"search_role": "baseline"}, "candidate": {"search_role": "candidate"}}
    for role in ("baseline", "candidate"):
        code_dir = work / role / "code"
        code_dir.mkdir(parents=True)
        shutil.copyfile(sources[role], code_dir / "model.py")
        write_json(work / role / "config.json", configs[role])
        prediction = work / role / "predictions.json"
        command = [sys.executable, str(code_dir / "model.py"),
                   "--train", str(work / "train.json"), "--input", str(work / "inputs.json"),
                   "--output", str(prediction), "--config", str(work / role / "config.json"),
                   "--seed", "11"]
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        environment["PYTHONHASHSEED"] = "0"
        if sandboxed:
            sandbox.label_write_scope(work / role)
        try:
            with (work / role / "stdout.log").open("wb") as out, (work / role / "stderr.log").open("wb") as err:
                sandbox.launch(command, cwd=str(code_dir), env=environment,
                               stdout=out, stderr=err, timeout_seconds=180, sandboxed=sandboxed)
        finally:
            if sandboxed:
                sandbox.unlabel_write_scope(work / role)
        predictions = read_json(prediction)
        scores[role] = score(holdout, predictions, evaluator_id)
        scores[role + "_predictions"] = predictions
    return scores


def _score_split(controller, parts, features, target, evaluator_id, metric, candidate_code,
                 split_seed, *, sandboxed):
    def rows_for(name):
        body = []
        for row in parts[name]:
            item = {"id": row["id"], "y": row["values"][target]}
            if len(features) == 1:
                item["x"] = row["values"][features[0]]
            else:
                item["features"] = [row["values"][name] for name in features]
            body.append(item)
        return body
    work = controller.run_dir / "split-confirmation" / f"seed-{split_seed}"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    train = rows_for("train")
    holdout = rows_for("holdout")
    write_json(work / "train.json", train)
    write_json(work / "inputs.json", model_inputs(holdout, evaluator_id))
    scores = {}
    sources = {"baseline": Path(controller.project) / "model.py", "candidate": candidate_code}
    configs = {"baseline": {"search_role": "baseline"}, "candidate": {"search_role": "candidate"}}
    for role in ("baseline", "candidate"):
        code_dir = work / role / "code"
        code_dir.mkdir(parents=True)
        shutil.copyfile(sources[role], code_dir / "model.py")
        write_json(work / role / "config.json", configs[role])
        prediction = work / role / "predictions.json"
        command = [sys.executable, str(code_dir / "model.py"),
                   "--train", str(work / "train.json"), "--input", str(work / "inputs.json"),
                   "--output", str(prediction), "--config", str(work / role / "config.json"),
                   "--seed", "11"]
        stdout = work / role / "stdout.log"
        stderr = work / role / "stderr.log"
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
        environment["PYTHONHASHSEED"] = "0"
        if sandboxed:
            sandbox.label_write_scope(work / role)
        try:
            with stdout.open("wb") as out, stderr.open("wb") as err:
                sandbox.launch(command, cwd=str(code_dir), env=environment,
                               stdout=out, stderr=err, timeout_seconds=180, sandboxed=sandboxed)
        finally:
            if sandboxed:
                sandbox.unlabel_write_scope(work / role)
        scores[role] = score(holdout, read_json(prediction), evaluator_id)
    return scores

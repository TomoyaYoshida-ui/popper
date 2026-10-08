"""配方执行器。只使用标准库，以便整文件复制进沙箱后仍能运行。

内核用同一份函数在沙箱外重算拟合。本文件不写 Observation，也不写 Claim。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path


# 与 intake.py 的同名常数一致。本文件会整份复制进沙箱，不能导入 intake。
MAX_COLUMNS = 128
MAX_KMER_K = 6


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def fail(message):
    raise RuntimeError(message)


def json_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail("结果不是有限数")
    if isinstance(value, float):
        if not math.isfinite(value):
            fail("结果不是有限数")
        if value.is_integer() and abs(value) < 2**53:
            return int(value)
        return value
    return value


def quantile(sorted_values, probability):
    """§3.3：在已升序的非缺失值上取分位数。空数组返回 None。"""
    count = len(sorted_values)
    if count == 0:
        return None
    if probability == 0:
        return json_number(sorted_values[0])
    if probability == 1:
        return json_number(sorted_values[-1])
    position = probability * (count - 1)
    low = math.floor(position)
    high = math.ceil(position)
    left = sorted_values[low]
    right = sorted_values[high]
    return json_number(left * (high - position) + right * (position - low))


def quantiles(values):
    present = sorted(value for value in values if value is not None)
    if not present:
        return None
    return {name: quantile(present, probability)
            for name, probability in (("p0", 0), ("p25", 0.25), ("p50", 0.5),
                                      ("p75", 0.75), ("p100", 1))}


def level_token(value):
    text = value if isinstance(value, str) else canonical(value)
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", text):
        return text
    return "c" + digest(value)[:12]


def _copy_rows(rows):
    return [{"id": row["id"], "values": dict(row["values"])} for row in rows]


def _require_column(rows, column):
    if any(column not in row["values"] for row in rows):
        fail(f"列不存在: {column}")


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _feature_columns(rows, target):
    if not rows:
        fail("切分后某一份为空")
    return [name for name in rows[0]["values"] if name != target]


def _apply_keep(train, dev, holdout, kept, target, types):
    """按训练拟合得到的保留清单删特征列。目标列留在原位，确认集不另算。"""
    order = [name for name in train[0]["values"] if name == target or name in set(kept)]
    for row in train + dev + holdout:
        row["values"] = {name: row["values"][name] for name in order}
    for name in list(types):
        if name not in order:
            types.pop(name, None)


def _population_variance(values):
    count = len(values)
    if count == 0:
        fail("切分后某一份为空")
    mean = sum(values) / count
    return sum((value - mean) ** 2 for value in values) / count


def _expanded_names(column, tokens, rows):
    names = []
    for token in list(tokens) + ["__other__"]:
        name = f"{column}__{level_token(token)}"
        if name in names or any(name in row["values"] for row in rows):
            fail("one-hot 列名冲突")
        names.append(name)
    return names


def _sequence_text(row, column):
    value = row["values"].get(column)
    if value == "":
        fail("序列文件含空序列")
    if not isinstance(value, str):
        fail(f"列无法推断类型: {column}")
    return value


def _output_name(output):
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", output):
        fail("配方步骤含未声明字段")
    return output


def _ready_numbers(rows, column, types):
    if types.get(column) not in {"integer", "number"}:
        fail(f"列无法推断类型: {column}")
    if any(row["values"].get(column) is None for row in rows):
        fail("派生列仍有缺失；先填充或删行")
    values = [row["values"][column] for row in rows]
    if any(not _finite_number(value) for value in values):
        fail("结果不是有限数")
    return values


def _add_output(train, dev, holdout, types, output):
    _output_name(output)
    if any(output in row["values"] for row in train + dev + holdout):
        fail("one-hot 列名冲突")
    if train and len(train[0]["values"]) + 1 > MAX_COLUMNS:
        fail(f"列数超过 {MAX_COLUMNS}")
    types[output] = "number"


def _derived_column(op, step, train, dev, holdout, types, target, estimated, reject_target):
    """受控派生列：标准化、两列乘积、整数次方。统计量只从训练行来。"""
    output = step["output"]
    if output == target:
        fail(f"目标列禁止该变换: {op}")
    rows = train + dev + holdout
    if op == "product":
        left, right = step["left"], step["right"]
        reject_target(op, left)
        reject_target(op, right)
        _require_column(rows, left)
        _require_column(rows, right)
        _ready_numbers(rows, left, types)
        _ready_numbers(rows, right, types)
        _add_output(train, dev, holdout, types, output)
        for row in rows:
            row["values"][output] = json_number(row["values"][left] * row["values"][right])
        return
    column = step["column"]
    reject_target(op, column)
    _require_column(rows, column)
    if op == "scale":
        values = _ready_numbers(train, column, types)
        _ready_numbers(dev + holdout, column, types)
        mean = sum(values) / len(values)
        variance = sum((value - mean) ** 2 for value in values) / len(values)
        std = math.sqrt(variance)
        if std == 0:
            fail("常数列不能标准化")
        mean, std = json_number(mean), json_number(std)
        estimated.append({"op": op, "column": column, "mean": mean, "std": std})
        _add_output(train, dev, holdout, types, output)
        for row in rows:
            row["values"][output] = json_number((row["values"][column] - mean) / std)
        return
    if op == "power":
        exponent = step["exponent"]
        if exponent not in {2, 3, 4}:
            fail("幂次只能是 2、3 或 4")
        _ready_numbers(rows, column, types)
        _add_output(train, dev, holdout, types, output)
        for row in rows:
            row["values"][output] = json_number(row["values"][column] ** exponent)
        return
    fail(f"配方含词汇表外操作: {op}")


def run_recipe(recipe, train, dev, holdout, types, type_out=None):
    """按配方变换三份划分。统计量只从训练行拟合。确认集不删行。

    确认集里若有一行会被 `drop_rows` 去掉，直接拒绝。训练行的删除不改确认集。
    `type_out` 若给出，则写入执行结束后的列类型。
    返回 (fit, train_out, dev_out, holdout_out)。fit 不含科学结论。
    """
    target = recipe["target_column"]
    train, dev, holdout = _copy_rows(train), _copy_rows(dev), _copy_rows(holdout)
    types = dict(types)
    dropped_train, dropped_dev = [], []
    estimated = []

    def reject_target(op, column):
        if column == target and op != "drop_rows":
            fail(f"目标列禁止该变换: {op}")

    for step in recipe["steps"]:
        op = step["op"]
        if op == "drop_rows":
            column = step["column"]
            if step.get("predicate") != "missing":
                fail("删行只允许按缺失")
            _require_column(train + dev + holdout, column)
            if any(row["values"][column] is None for row in holdout):
                if column == target:
                    fail("确认集含目标列缺失，行集合不可变，不能删行")
                fail("确认集行集合不可变")
            gone_train = [row["id"] for row in train if row["values"][column] is None]
            gone_dev = [row["id"] for row in dev if row["values"][column] is None]
            gone = set(gone_train + gone_dev)
            if gone & {row["id"] for row in holdout}:
                fail("确认集行集合不可变")
            dropped_train.extend(gone_train)
            dropped_dev.extend(gone_dev)
            train = [row for row in train if row["id"] not in gone]
            dev = [row for row in dev if row["id"] not in gone]
            if not train or not dev:
                fail("切分后某一份为空")
            continue
        if op == "drop_columns":
            for column in step["columns"]:
                reject_target(op, column)
                _require_column(train + dev + holdout, column)
            for row in train + dev + holdout:
                for column in step["columns"]:
                    row["values"].pop(column, None)
            for column in step["columns"]:
                types.pop(column, None)
            continue
        if op == "impute_constant":
            column, value = step["column"], step["value"]
            reject_target(op, column)
            _require_column(train + dev + holdout, column)
            _check_constant(types.get(column), value)
            for row in train + dev + holdout:
                if row["values"].get(column) is None:
                    row["values"][column] = value
            continue
        if op == "impute_median":
            column = step["column"]
            reject_target(op, column)
            _require_column(train, column)
            if types.get(column) not in {"integer", "number"}:
                fail(f"目标列禁止该变换: {op}" if column == target
                     else f"列无法推断类型: {column}")
            present = sorted(row["values"][column] for row in train
                             if row["values"].get(column) is not None)
            if not present:
                fail(f"训练集没有可用于拟合的非缺失值: {column}")
            filled = quantile(present, 0.5)
            estimated.append({"op": op, "column": column, "p50": filled})
            for row in train + dev + holdout:
                if row["values"].get(column) is None:
                    row["values"][column] = filled
            if isinstance(filled, float):
                types[column] = "number"
            continue
        if op == "impute_mode":
            column = step["column"]
            reject_target(op, column)
            _require_column(train, column)
            if types.get(column) not in {"categorical", "integer"}:
                fail(f"列无法推断类型: {column}")
            present = [row["values"][column] for row in train
                       if row["values"].get(column) is not None]
            if not present:
                fail(f"训练集没有可用于拟合的非缺失值: {column}")
            counts = {}
            for value in present:
                counts[canonical(value)] = counts.get(canonical(value), 0) + 1
            mode_key = sorted(counts, key=lambda key: (-counts[key], key))[0]
            mode = next(value for value in present if canonical(value) == mode_key)
            estimated.append({"op": op, "column": column, "mode": mode})
            for row in train + dev + holdout:
                if row["values"].get(column) is None:
                    row["values"][column] = mode
            continue
        if op == "missing_indicator":
            column, output = step["column"], step["output"]
            reject_target(op, column)
            _require_column(train, column)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", output):
                fail("配方步骤含未声明字段")
            if any(output in row["values"] for row in train + dev + holdout):
                fail("one-hot 列名冲突")
            for row in train + dev + holdout:
                row["values"][output] = 1 if row["values"].get(column) is None else 0
            types[output] = "integer"
            continue
        if op == "one_hot":
            column = step["column"]
            reject_target(op, column)
            _require_column(train, column)
            if types.get(column) != "categorical":
                fail(f"列无法推断类型: {column}")
            if any(row["values"].get(column) is None for row in train + dev + holdout):
                fail("one-hot 列仍有缺失；先填充或删行")
            levels = sorted({row["values"][column] for row in train}, key=canonical)
            names = []
            for level in list(levels) + ["__other__"]:
                name = f"{column}__{level_token(level)}"
                if name in names or any(name in row["values"] for row in train):
                    fail("one-hot 列名冲突")
                names.append(name)
            level_set = set(levels)
            for row in train + dev + holdout:
                value = row["values"].pop(column)
                chosen = value if value in level_set else "__other__"
                for level, name in zip(list(levels) + ["__other__"], names):
                    row["values"][name] = 1 if level == chosen else 0
            types.pop(column, None)
            for name in names:
                types[name] = "integer"
            estimated.append({"op": op, "column": column, "levels": levels})
            continue
        if op == "select_columns":
            for column in step["columns"]:
                reject_target(op, column)
                _require_column(train, column)
            keep = list(step["columns"])
            for row in train + dev + holdout:
                row["values"] = {column: row["values"][column] for column in keep}
            types = {column: types[column] for column in keep}
            continue
        if op == "ordinal":
            column = step["column"]
            reject_target(op, column)
            categories = step["categories"]
            if (not isinstance(categories, list) or not categories
                    or any(type(item) is not str for item in categories)
                    or len(categories) != len(set(categories))):
                fail("配方步骤含未声明字段")
            _require_column(train + dev + holdout, column)
            index = {item: position for position, item in enumerate(categories)}
            for row in train + dev + holdout:
                value = row["values"].get(column)
                if type(value) is not str or value not in index:
                    fail(f"序数编码遇到未声明的取值: {column}")
                row["values"][column] = index[value]
            types[column] = "integer"
            continue
        if op == "select_missing_rate":
            limit = step["max_missing_rate"]
            if not _finite_number(limit) or not 0 <= limit <= 1:
                fail("配方步骤含未声明字段")
            names = _feature_columns(train, target)
            total = len(train)
            kept = []
            for name in names:
                _require_column(train + dev + holdout, name)
                missing = sum(row["values"].get(name) is None for row in train)
                if missing / total <= limit:
                    kept.append(name)
            _apply_keep(train, dev, holdout, kept, target, types)
            estimated.append({"op": op, "columns": kept})
            continue
        if op == "select_variance":
            threshold = step["min_variance"]
            if not _finite_number(threshold) or threshold < 0:
                fail("配方步骤含未声明字段")
            names = _feature_columns(train, target)
            kept = []
            for name in names:
                _require_column(train + dev + holdout, name)
                if types.get(name) not in {"integer", "number"}:
                    kept.append(name)
                    continue
                present = [row["values"].get(name) for row in train + dev + holdout]
                if any(value is None for value in present):
                    fail("方差选择要求列已无缺失")
                if any(isinstance(value, bool) or not isinstance(value, (int, float))
                       for value in present):
                    fail(f"列无法推断类型: {name}")
                variance = _population_variance([row["values"][name] for row in train])
                if variance <= threshold:
                    continue
                kept.append(name)
            _apply_keep(train, dev, holdout, kept, target, types)
            estimated.append({"op": op, "columns": kept})
            continue
        if op == "kmer":
            column, k = step["column"], step["k"]
            reject_target(op, column)
            if type(k) is not int or not 1 <= k <= MAX_KMER_K:
                fail(f"k-mer 的 k 必须是 1 到 {MAX_KMER_K} 的整数")
            _require_column(train + dev + holdout, column)
            if types.get(column) != "sequence":
                fail(f"列无法推断类型: {column}")
            for row in train + dev + holdout:
                _sequence_text(row, column)
            alphabet = set()
            vocab = set()
            for row in train:
                text = row["values"][column]
                alphabet.update(text)
                if len(text) >= k:
                    vocab.update(text[index:index + k] for index in range(len(text) - k + 1))
            vocabulary = sorted(vocab)
            names = _expanded_names(column, vocabulary, train)
            if len(train[0]["values"]) - 1 + len(names) > MAX_COLUMNS:
                fail("k-mer 展开后列数超过上限")
            other = names[-1]
            known = {gram: f"{column}__{level_token(gram)}" for gram in vocabulary}
            for row in train + dev + holdout:
                text = row["values"].pop(column)
                counts = {name: 0 for name in names}
                if len(text) >= k:
                    for index in range(len(text) - k + 1):
                        gram = text[index:index + k]
                        if gram in known and all(char in alphabet for char in gram):
                            counts[known[gram]] += 1
                        else:
                            counts[other] += 1
                row["values"].update(counts)
            types.pop(column, None)
            for name in names:
                types[name] = "integer"
            estimated.append({"op": op, "column": column, "k": k, "vocabulary": vocabulary})
            continue
        if op == "length":
            column, output = step["column"], step["output"]
            reject_target(op, column)
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", str(output)):
                fail("配方步骤含未声明字段")
            _require_column(train + dev + holdout, column)
            if types.get(column) != "sequence":
                fail(f"列无法推断类型: {column}")
            if any(output in row["values"] for row in train + dev + holdout):
                fail("one-hot 列名冲突")
            for row in train + dev + holdout:
                row["values"][output] = len(_sequence_text(row, column))
            types[output] = "integer"
            continue
        if op == "composition":
            column = step["column"]
            reject_target(op, column)
            _require_column(train + dev + holdout, column)
            if types.get(column) != "sequence":
                fail(f"列无法推断类型: {column}")
            for row in train + dev + holdout:
                _sequence_text(row, column)
            letters = sorted({char.upper() for row in train for char in row["values"][column]})
            names = _expanded_names(column, letters, train)
            if len(train[0]["values"]) + len(names) > MAX_COLUMNS:
                fail(f"列数超过 {MAX_COLUMNS}")
            other = names[-1]
            named = {letter: f"{column}__{level_token(letter)}" for letter in letters}
            for row in train + dev + holdout:
                text = row["values"][column].upper()
                counts = {name: 0 for name in names}
                for char in text:
                    if char in named:
                        counts[named[char]] += 1
                    else:
                        counts[other] += 1
                for name in names:
                    row["values"][name] = json_number(counts[name] / len(text))
            for name in names:
                types[name] = "number"
            estimated.append({"op": op, "column": column, "letters": letters})
            continue
        if op in {"scale", "product", "power"}:
            _derived_column(op, step, train, dev, holdout, types, target, estimated, reject_target)
            continue
        fail(f"配方含词汇表外操作: {op}")

    output_columns = list(train[0]["values"]) if train else []
    if target in output_columns:
        output_columns = [column for column in output_columns if column != target]
        for row in train + dev + holdout:
            row["values"].pop(target, None)
    fit = {
        "schema": "popper.recipe-fit/v1",
        "recipe_sha256": digest(recipe),
        "dropped_train_ids": dropped_train,
        "dropped_dev_ids": dropped_dev,
        "steps": estimated,
        "output_feature_columns": output_columns,
    }
    if type_out is not None:
        type_out.clear()
        type_out.update(types)
    return fit, train, dev, holdout


def _check_constant(column_type, value):
    if column_type == "integer":
        if type(value) is not int:
            fail("配方步骤含未声明字段")
        return
    if column_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (
                isinstance(value, float) and not math.isfinite(value)):
            fail("配方步骤含未声明字段")
        return
    if column_type == "categorical":
        if not isinstance(value, str):
            fail("配方步骤含未声明字段")
        return
    fail("配方步骤含未声明字段")


def _read_jsonl(path):
    rows = []
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if line:
            rows.append(json.loads(line))
    return rows


def _write_jsonl(path, rows):
    payload = "".join(canonical(row) + "\n" for row in rows)
    Path(path).write_text(payload, encoding="utf-8")


def run_workspace(root):
    """从工作区输入拟合训练行，并写出训练与开发的变换结果。"""
    root = Path(root)
    recipe = json.loads((root / "inputs" / "recipe.json").read_text(encoding="utf-8"))
    columns = json.loads((root / "inputs" / "columns.json").read_text(encoding="utf-8"))
    types = {item["name"]: item["type"] for item in columns["columns"]}
    train = _read_jsonl(root / "inputs" / "train.jsonl")
    dev = _read_jsonl(root / "inputs" / "dev.jsonl")
    fit, train_out, dev_out, _holdout = run_recipe(recipe, train, dev, [], types)
    output = root / "outputs"
    output.mkdir(parents=True, exist_ok=True)
    (output / "recipe_fit.json").write_text(canonical(fit) + "\n", encoding="utf-8")
    _write_jsonl(output / "train.jsonl", train_out)
    _write_jsonl(output / "dev.jsonl", dev_out)
    return fit


def main():
    try:
        run_workspace(Path.cwd())
    except Exception as exc:
        import sys
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

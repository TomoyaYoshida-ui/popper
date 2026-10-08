"""预处理配方的提案、批准和编译。

配方只变换特征。它不写 Observation，也不写 Claim。
统计量只从训练行拟合。确认集行集合在编译前后保持不变。
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

from ..core import ProtocolError, canonical, digest, evaluator_pack, file_hash, read_json, write_json
from . import recipe_runner
from .intake import (MAX_KMER_K, MAX_RECIPE_STEPS, MAX_UNBUDGETED_COMPILES, STUDY_BUDGET_FLOOR,
                     TABLE_EVALUATORS, IntakeLog, _project_dir, _unbudgeted_in, load_intake,
                     load_study_budget, parse_bytes, split_rows)
from .recipe_runner import run_recipe, run_workspace

_STEP_KEYS = {
    "drop_rows": {"op", "column", "predicate"},
    "drop_columns": {"op", "columns"},
    "impute_constant": {"op", "column", "value"},
    "impute_median": {"op", "column"},
    "impute_mode": {"op", "column"},
    "missing_indicator": {"op", "column", "output"},
    "one_hot": {"op", "column"},
    "ordinal": {"op", "column", "categories"},
    "select_columns": {"op", "columns"},
    "select_missing_rate": {"op", "max_missing_rate"},
    "select_variance": {"op", "min_variance"},
    "kmer": {"op", "column", "k"},
    "length": {"op", "column", "output"},
    "composition": {"op", "column"},
    "scale": {"op", "column", "output"},
    "product": {"op", "left", "right", "output"},
    "power": {"op", "column", "exponent", "output"},
}
_OUTPUT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_FROZEN_PHASES = frozenset({"frozen", "confirming", "completed", "confirmation_failed"})
_IMPLEMENTED_OPS = frozenset(_STEP_KEYS)


def _log(project):
    return IntakeLog(Path(project) / ".popper-intake" / "intake.sqlite")


def _as_runtime(recipe, train, dev, holdout, types, type_out=None):
    try:
        return run_recipe(recipe, train, dev, holdout, types, type_out=type_out)
    except RuntimeError as exc:
        raise ProtocolError(str(exc)) from exc


def _split_at_last_drop(steps):
    """最后一个 `drop_rows` 及其之前的步骤在进工作区前执行。之后的步骤交给 runner。"""
    last = None
    for index, step in enumerate(steps):
        if step["op"] == "drop_rows":
            last = index
    if last is None:
        return [], list(steps)
    return list(steps[:last + 1]), list(steps[last + 1:])


def validate_recipe(recipe, declaration):
    if not isinstance(recipe, dict):
        raise ProtocolError("配方步骤含未声明字段")
    atom = recipe.get("row_atom")
    expected = {"schema", "target_column", "row_atom", "steps"}
    if atom in {"group", "time_block"}:
        expected.add("row_atom_column")
    if set(recipe) != expected or recipe.get("schema") != "popper.recipe/v1":
        raise ProtocolError("配方步骤含未声明字段")
    if not isinstance(recipe.get("target_column"), str):
        raise ProtocolError("目标列不存在: " + str(recipe.get("target_column")))
    steps = recipe.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_RECIPE_STEPS:
        raise ProtocolError(f"配方步骤超过 {MAX_RECIPE_STEPS}")
    strategy, column = declaration["strategy"], declaration["column"]
    if atom == "row" and strategy not in {"random", "stratified", "id_list"}:
        raise ProtocolError("配方的重复单位列与切分声明不一致")
    if atom == "group" and (strategy != "group" or recipe.get("row_atom_column") != column):
        raise ProtocolError("配方的重复单位列与切分声明不一致")
    if atom == "time_block" and (strategy != "temporal" or recipe.get("row_atom_column") != column):
        raise ProtocolError("配方的重复单位列与切分声明不一致")
    if atom not in {"row", "group", "time_block"}:
        raise ProtocolError("配方的重复单位列与切分声明不一致")
    target = recipe["target_column"]
    if declaration.get("target_column") not in {None, target}:
        raise ProtocolError("目标列已经写入数据卡；更改目标列必须新建 Study")
    for step in steps:
        if not isinstance(step, dict) or "op" not in step:
            raise ProtocolError("配方步骤含未声明字段")
        op = step["op"]
        if op not in _IMPLEMENTED_OPS:
            raise ProtocolError(f"配方含词汇表外操作: {op}")
        if set(step) != _STEP_KEYS[op]:
            raise ProtocolError("配方步骤含未声明字段")
        if op == "drop_rows" and step.get("predicate") != "missing":
            raise ProtocolError("删行只允许按缺失")
        names = []
        if "column" in step:
            names.append(step["column"])
        for key in ("left", "right", "output"):
            if key in step:
                names.append(step[key])
        if "columns" in step:
            if (not isinstance(step["columns"], list) or not step["columns"]
                    or len(step["columns"]) != len(set(step["columns"]))
                    or any(not isinstance(name, str) for name in step["columns"])):
                raise ProtocolError("配方步骤含未声明字段")
            names.extend(step["columns"])
        for name in names:
            if name == target and op != "drop_rows":
                raise ProtocolError(f"目标列禁止该变换: {op}")
        _validate_step_values(step)
    return recipe


def _finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _validate_step_values(step):
    op = step["op"]
    if op == "ordinal":
        categories = step["categories"]
        if (not isinstance(categories, list) or not categories
                or any(type(item) is not str for item in categories)
                or len(categories) != len(set(categories))):
            raise ProtocolError("配方步骤含未声明字段")
    elif op == "select_missing_rate":
        rate = step["max_missing_rate"]
        if not _finite(rate) or not 0 <= rate <= 1:
            raise ProtocolError("配方步骤含未声明字段")
    elif op == "select_variance":
        bound = step["min_variance"]
        if not _finite(bound) or bound < 0:
            raise ProtocolError("配方步骤含未声明字段")
    elif op == "kmer":
        k = step["k"]
        if type(k) is not int or not 1 <= k <= MAX_KMER_K:
            raise ProtocolError(f"k-mer 的 k 必须是 1 到 {MAX_KMER_K} 的整数")
    elif op == "length" and (type(step["output"]) is not str or not _OUTPUT_NAME.fullmatch(step["output"])):
        raise ProtocolError("配方步骤含未声明字段")
    elif op in {"scale", "product", "power"}:
        if type(step["output"]) is not str or not _OUTPUT_NAME.fullmatch(step["output"]):
            raise ProtocolError("配方步骤含未声明字段")
        if op == "power" and step["exponent"] not in {2, 3, 4}:
            raise ProtocolError("幂次只能是 2、3 或 4")


def _reserve_study_compile(link, item):
    from .store import ResearchStore
    store = ResearchStore(Path(link["run_dir"]) / "research.sqlite")
    try:
        store.budget(link["study_id"]).reserve(link["family_id"], item, 1.0)
    finally:
        store.close()


def _assert_compile_allowed(project):
    """Study 进入 frozen 之后不能再编译。没有协议状态文件时仍允许夹具编译。"""
    if not (Path(project) / ".popper" / "state.db").is_file():
        return
    from ..core import Experiment
    experiment = Experiment(project, readonly=True)
    try:
        phase = experiment.state().get("phase")
    finally:
        experiment.close()
    if phase in _FROZEN_PHASES:
        raise ProtocolError("协议已冻结，不能再编译新配方")


def propose_recipe(project, recipe_path):
    project = _project_dir(project)
    seal, card, _directory = load_intake(project)
    recipe = validate_recipe(read_json(recipe_path), seal["split_declaration"])
    if recipe["target_column"] not in {item["name"] for item in card["columns"]}:
        raise ProtocolError(f"目标列不存在: {recipe['target_column']}")
    recipe_sha = digest(recipe)
    proposal_id = "RCP-" + recipe_sha[:16]
    stored = project / ".popper-intake" / "recipes" / f"{recipe_sha}.json"
    if not stored.is_file():
        write_json(stored, recipe)
    log = _log(project)
    try:
        existing = [item for item in log.events()
                    if item["event_type"] == "recipe_proposed"
                    and item["payload"]["proposal_id"] == proposal_id]
        if not existing:
            log.append(seal["raw_sha256"], "recipe_proposed",
                       {"proposal_id": proposal_id, "recipe_sha256": recipe_sha})
            log.commit()
    finally:
        log.close()
    return {"proposal_id": proposal_id, "recipe_sha256": recipe_sha}


def approve_recipe(project, proposal_id):
    project = _project_dir(project)
    seal, _card, _directory = load_intake(project)
    log = _log(project)
    try:
        proposed = [item for item in log.events()
                    if item["event_type"] == "recipe_proposed"
                    and item["payload"]["proposal_id"] == proposal_id]
        if not proposed:
            raise ProtocolError("配方尚未批准")
        approved = [item for item in log.events()
                    if item["event_type"] == "recipe_approved"
                    and item["payload"]["proposal_id"] == proposal_id]
        if not approved:
            log.append(seal["raw_sha256"], "recipe_approved", proposed[0]["payload"])
            log.commit()
        payload = proposed[0]["payload"]
    finally:
        log.close()
    return {"proposal_id": payload["proposal_id"], "recipe_sha256": payload["recipe_sha256"]}


def _materialize(project, seal):
    """按封存声明重读原始表。封存件只保存确认集行，训练标签仍在原始文件里。"""
    raw_sha = seal["raw_sha256"]
    found = None
    for path in Path(project).rglob("*"):
        if not path.is_file() or ".popper-intake" in path.parts or path.name.startswith("."):
            continue
        if file_hash(path) == raw_sha:
            found = path
            break
    if found is None:
        raise ProtocolError("原始文件不存在")
    from .intake import load_adapter_registry, resolve_adapter
    raw = found.read_bytes()
    adapter = resolve_adapter(found, None, raw, registry=load_adapter_registry(project))
    columns, raw_rows, types = parse_bytes(found, adapter, raw, project=project)
    declaration = seal["split_declaration"]
    from .intake import _prepare_rows
    rows = _prepare_rows(columns, raw_rows, types, declaration)
    lists = None
    parts = split_rows(rows, declaration, declaration["ratios"], lists)
    if [row["id"] for row in parts["holdout"]] != seal["holdout_ids"]:
        raise ProtocolError("接入事件链与封存件不一致")
    return columns, types, parts


def _feature_rows(rows, target):
    return [{"id": row["id"],
             "values": {key: value for key, value in row["values"].items() if key != target}}
            for row in rows]


def _write_jsonl(path, rows):
    """保留 values 的列顺序。规范 JSON 会按键排序，那会让沙箱读回的列序和内存不一致。"""
    lines = []
    for row in rows:
        lines.append(json.dumps(
            {"id": row["id"], "values": row["values"]},
            ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    path.write_text("".join(lines), encoding="utf-8")


def _read_jsonl(path):
    return [read_json_line for read_json_line in (
        __import__("json").loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line)]


def _workspace_has_holdout(workspace, holdout_ids):
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for row_id in holdout_ids:
            if row_id and row_id in text:
                raise ProtocolError("配方沙箱工作区出现确认集行")


def _execute(workspace, executor, sandboxed):
    if executor is not None:
        started = time.perf_counter()
        executor(workspace)
        return time.perf_counter() - started, "injected_executor"
    if not sandboxed:
        raise ProtocolError("配方编译必须在沙箱中执行")
    from .. import sandbox
    import os
    import subprocess
    import sys
    if not sandbox.available():
        raise ProtocolError(sandbox.no_backend_message())
    output = workspace / "outputs"
    output.mkdir(parents=True, exist_ok=True)
    sandbox.label_write_scope(workspace)
    sandbox.label_write_scope(output)
    env = {key: value for key, value in os.environ.items()
           if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    env["PYTHONHASHSEED"] = "0"
    started = time.perf_counter()
    try:
        with (output / "stdout.log").open("wb") as stdout, (output / "stderr.log").open("wb") as stderr:
            sandbox.launch(
                [sys.executable, str(workspace / "code" / "recipe_runner.py")],
                cwd=str(workspace), env=env, stdout=stdout, stderr=stderr,
                timeout_seconds=60, sandboxed=True)
    except subprocess.CalledProcessError as exc:
        detail = (output / "stderr.log").read_text(encoding="utf-8", errors="replace").strip()
        raise ProtocolError(detail or "配方拟合与训练集重算不一致") from exc
    finally:
        try:
            sandbox.unlabel_write_scope(output)
            sandbox.unlabel_write_scope(workspace)
        except OSError:
            pass
    return time.perf_counter() - started, sandbox.execution_backend_name()


def compile_recipe(project, proposal_id, evaluator, *, executor=None, sandboxed=True):
    if evaluator not in TABLE_EVALUATORS:
        raise ProtocolError("配方编译只能投影到已有的五个表格域包")
    project = _project_dir(project)
    _assert_compile_allowed(project)
    seal, card, directory = load_intake(project)
    log = _log(project)
    try:
        study_budget = load_study_budget(project)
        unbudgeted = _unbudgeted_in(log)
        if study_budget is None:
            if unbudgeted >= MAX_UNBUDGETED_COMPILES:
                raise ProtocolError("尚未建立 Study，配方编译次数已达上限")
            budget_reserved = False
        else:
            if unbudgeted and float(study_budget["budget_cap"]) < STUDY_BUDGET_FLOOR + unbudgeted:
                raise ProtocolError("未入账编译次数超过 Study 预算下限")
            budget_reserved = True
        approved = [item for item in log.events()
                    if item["event_type"] == "recipe_approved"
                    and item["payload"]["proposal_id"] == proposal_id]
        if not approved:
            raise ProtocolError("配方尚未批准")
        recipe_sha = approved[0]["payload"]["recipe_sha256"]
        recipe = validate_recipe(
            read_json(directory / "recipes" / f"{recipe_sha}.json"), seal["split_declaration"])
        if card["target_column"] != recipe["target_column"]:
            raise ProtocolError("目标列已经写入数据卡；更改目标列必须新建 Study")
        columns, types, parts = _materialize(project, seal)
        target = recipe["target_column"]
        label_by_id = {
            row["id"]: row["values"][target]
            for row in parts["train"] + parts["dev"] + parts["holdout"]
        }
        before_ids = [row["id"] for row in parts["holdout"]]
        before_labels = [label_by_id[row_id] for row_id in before_ids]
        before_row_set = seal["row_set_sha256"]
        prefix_steps, suffix_steps = _split_at_last_drop(recipe["steps"])
        full_train, full_dev, full_holdout = parts["train"], parts["dev"], parts["holdout"]
        prefix_fit = None
        type_out = {}
        if prefix_steps:
            probe = dict(recipe)
            probe["steps"] = prefix_steps
            prefix_fit, full_train, full_dev, full_holdout = _as_runtime(
                probe, full_train, full_dev, full_holdout, types, type_out)
            if [row["id"] for row in full_holdout] != before_ids:
                raise ProtocolError("确认集行集合不可变")
        feature_recipe = dict(recipe)
        feature_recipe["steps"] = suffix_steps
        if prefix_fit is None:
            train = _feature_rows(full_train, target)
            dev = _feature_rows(full_dev, target)
            holdout = _feature_rows(full_holdout, target)
            workspace_columns = [name for name in columns if name != target]
            feature_types = {name: types[name] for name in workspace_columns}
        else:
            train, dev, holdout = full_train, full_dev, full_holdout
            workspace_columns = list(train[0]["values"])
            feature_types = {name: type_out[name] for name in workspace_columns}
        workspace = directory / "workspaces" / recipe_sha
        if workspace.exists():
            import shutil
            shutil.rmtree(workspace)
        (workspace / "code").mkdir(parents=True)
        (workspace / "inputs").mkdir()
        (workspace / "outputs").mkdir()
        source = Path(recipe_runner.__file__)
        target_runner = workspace / "code" / "recipe_runner.py"
        target_runner.write_bytes(source.read_bytes())
        if file_hash(target_runner) != file_hash(source):
            raise ProtocolError("执行回执与产物哈希不一致")
        write_json(workspace / "inputs" / "recipe.json", feature_recipe)
        write_json(workspace / "inputs" / "columns.json",
                   {"columns": [{"name": name, "type": feature_types[name]}
                                for name in workspace_columns]})
        _write_jsonl(workspace / "inputs" / "train.jsonl", train)
        _write_jsonl(workspace / "inputs" / "dev.jsonl", dev)
        allowed = {"code/recipe_runner.py", "inputs/recipe.json", "inputs/train.jsonl",
                   "inputs/dev.jsonl", "inputs/columns.json"}
        present = {path.relative_to(workspace).as_posix()
                   for path in workspace.rglob("*") if path.is_file()}
        if not present <= allowed | {"outputs"} and any(
                item.startswith("outputs/") for item in present):
            pass
        extra = {item for item in present if item != "outputs" and not item.startswith("outputs")}
        if extra - allowed:
            raise ProtocolError("配方沙箱工作区出现确认集行")
        _workspace_has_holdout(workspace, seal["holdout_ids"])
        for path in (workspace / "inputs").glob("*.jsonl"):
            for row in _read_jsonl(path):
                if target in row["values"]:
                    raise ProtocolError("配方沙箱工作区出现确认集行")
        elapsed, backend = _execute(workspace, executor, sandboxed)
        for name in ("stdout.log", "stderr.log"):
            handle = workspace / "outputs" / name
            if not handle.is_file():
                handle.write_text("", encoding="utf-8")
        declared = ["outputs/recipe_fit.json", "outputs/train.jsonl", "outputs/dev.jsonl",
                    "outputs/stdout.log", "outputs/stderr.log"]
        missing = [name for name in declared if not (workspace / name).is_file()]
        if missing:
            raise ProtocolError(f"Job 缺少声明输出: {missing}")
        runner_fit = read_json(workspace / "outputs" / "recipe_fit.json")
        refit, _refit_train, _refit_dev, _unused = _as_runtime(
            feature_recipe, train, dev, [], feature_types)
        if canonical(refit) != canonical(runner_fit):
            raise ProtocolError("配方拟合与训练集重算不一致")
        _again, _train_out, _dev_out, holdout_out = _as_runtime(
            feature_recipe, train, dev, holdout, feature_types)
        if canonical(_again) != canonical(runner_fit):
            raise ProtocolError("配方拟合与训练集重算不一致")
        train_out = _read_jsonl(workspace / "outputs" / "train.jsonl")
        dev_out = _read_jsonl(workspace / "outputs" / "dev.jsonl")
        labels = label_by_id
        dropped_train = list(runner_fit["dropped_train_ids"])
        dropped_dev = list(runner_fit["dropped_dev_ids"])
        estimated = list(runner_fit["steps"])
        if prefix_fit is not None:
            dropped_train = list(prefix_fit["dropped_train_ids"]) + dropped_train
            dropped_dev = list(prefix_fit["dropped_dev_ids"]) + dropped_dev
            estimated = list(prefix_fit["steps"]) + estimated
        if [row["id"] for row in holdout_out] != before_ids:
            raise ProtocolError("确认集行集合不可变")
        if [labels[row["id"]] for row in holdout_out] != before_labels:
            raise ProtocolError("确认集行集合不可变")
        if any(row_id in {item["id"] for item in holdout_out}
               for row_id in dropped_train + dropped_dev):
            raise ProtocolError("确认集行集合不可变")
        published = dict(runner_fit)
        published["dropped_train_ids"] = dropped_train
        published["dropped_dev_ids"] = dropped_dev
        published["steps"] = estimated
        published["recipe_sha256"] = recipe_sha
        compiled = directory / "compiled" / recipe_sha
        compiled.mkdir(parents=True, exist_ok=True)
        projected = {
            "train": _project(evaluator, train_out, labels, published["output_feature_columns"]),
            "dev": _project(evaluator, dev_out, labels, published["output_feature_columns"]),
            "holdout": _project(evaluator, holdout_out, labels, published["output_feature_columns"]),
        }
        pack = evaluator_pack(evaluator)
        sets = [projected["train"], projected["dev"], projected["holdout"]]
        for split, rows in zip(("train", "dev", "test"), sets):
            pack.validate_rows(rows, split)
        pack.validate_splits(sets)
        for index, left in enumerate(sets):
            for right in sets[index + 1:]:
                if {row["id"] for row in left} & {row["id"] for row in right}:
                    raise ProtocolError("训练/开发/测试 id 存在交集")
                if {pack.row_identity(row) for row in left} & {pack.row_identity(row) for row in right}:
                    raise ProtocolError("数据划分存在完全相同的样本")
        write_json(compiled / "train.json", projected["train"])
        write_json(compiled / "dev.json", projected["dev"])
        write_json(compiled / "holdout.json", projected["holdout"])
        write_json(compiled / "recipe_fit.json", published)
        compiled_sha = {
            "train": file_hash(compiled / "train.json"),
            "dev": file_hash(compiled / "dev.json"),
            "holdout": file_hash(compiled / "holdout.json"),
        }
        artifacts = {name: file_hash(workspace / name) for name in declared}
        receipt = {
            "job_id": "JOB-" + digest({"proposal_id": proposal_id})[:24],
            "status": "succeeded",
            "revision_id": proposal_id,
            "revision_manifest_sha256": recipe_sha,
            "artifacts": artifacts,
            "elapsed_seconds": elapsed,
            "execution_backend": backend,
            "error_type": None,
        }
        write_json(compiled / "receipt.json", receipt)
        identity = {
            "schema": "popper.input-identity/v1",
            "raw_sha256": seal["raw_sha256"],
            "split_declaration_sha256": seal["split_declaration_sha256"],
            "seal_sha256": file_hash(directory / "holdout-seal.json"),
            "holdout_row_set_sha256": before_row_set,
            "data_card_sha256": digest(card),
            "recipe_sha256": recipe_sha,
            "recipe_fit_sha256": file_hash(compiled / "recipe_fit.json"),
            "runner_sha256": file_hash(source),
            "receipt_sha256": file_hash(compiled / "receipt.json"),
            "compiled_sha256": compiled_sha,
            "evaluator_id": evaluator,
        }
        write_json(compiled / "input-identity.json", identity)
        identity_sha = digest(identity)
        if budget_reserved:
            _reserve_study_compile(
                study_budget, f"recipe-compile:{proposal_id}:{log.count('recipe_compiled')}")
        log.append(seal["raw_sha256"], "recipe_compiled", {
            "input_identity_sha256": identity_sha,
            "seal_sha256": identity["seal_sha256"],
            "holdout_row_set_sha256": before_row_set,
            "recipe_sha256": recipe_sha,
            "recipe_fit_sha256": identity["recipe_fit_sha256"],
            "receipt_sha256": identity["receipt_sha256"],
            "budget_reserved": budget_reserved,
        })
        log.commit()
        if file_hash(directory / "holdout-seal.json") != identity["seal_sha256"]:
            raise ProtocolError("确认集行集合不可变")
        if seal["row_set_sha256"] != before_row_set:
            raise ProtocolError("确认集行集合不可变")
        return {
            "input_identity_sha256": identity_sha,
            "seal_sha256": identity["seal_sha256"],
            "holdout_row_set_sha256": before_row_set,
            "compiled_sha256": compiled_sha,
        }
    finally:
        log.close()


def _project(evaluator, rows, labels, feature_columns):
    if not feature_columns:
        raise ProtocolError("编译结果含非数值特征，过不了今天的域包")
    projected = []
    for row in rows:
        values = [row["values"][name] for name in feature_columns]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
            raise ProtocolError("编译结果含非数值特征，过不了今天的域包")
        label = labels[row["id"]]
        if evaluator in {"mse-v1", "mae-v1"}:
            if len(values) != 1:
                raise ProtocolError("现有 mse-v1 / mae-v1 只接受单列数值 x；多特征回归要等任务契约")
            projected.append({"id": row["id"], "x": values[0], "y": label})
            continue
        if evaluator in {"binary-accuracy-v1", "binary-f1-v1"}:
            if type(label) is not int or label not in {0, 1}:
                raise ProtocolError("二分类标签必须已经是整数 0 或 1")
        projected.append({"id": row["id"], "features": values, "label": label})
    return projected

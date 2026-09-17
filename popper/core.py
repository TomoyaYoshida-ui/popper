"""Trusted-local M0 runner. Integrity checks are not a hostile-code sandbox."""
from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
import platform
import random
import re
import shutil
import sqlite3
import statistics
import sys
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import sandbox


class _LazyEvaluators(Mapping):
    """``EVALUATORS`` 的惰性视图：访问时才导入域包注册表。

    域包需要 ``core`` 的校验助手（``ProtocolError``/``digest``/``number``），
    所以 ``core`` 不能在模块级导入 ``popper.domains``，否则成环。键为评估器 ID，
    值为冻结的评估器身份记录。

    视图不缓存快照：每次查询都反映当前注册表，避免「先取了一份副本、之后注册的
    域包被静默忽略」。
    """

    def _load(self):
        from .domains import registered_evaluators
        return registered_evaluators()

    def __getitem__(self, key):
        return self._load()[key]

    def __iter__(self):
        return iter(self._load())

    def __len__(self):
        return len(self._load())


EVALUATORS = _LazyEvaluators()


class ProtocolError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 临时名只需在写出与替换之间短暂唯一；8 位 hex 足够，同时为深层嵌套路径
    # 留出 Windows MAX_PATH(260) 余量。
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex[:8] + ".tmp")
    tmp.write_text(canonical(value) + "\n", encoding="utf-8")
    # Windows 覆盖已存在文件时，杀软/索引器可能短暂持有目标句柄，令 replace 抛
    # PermissionError(winerror 5/32)。退避重试消除该竞态；仍失败则清理临时文件并如实抛错。
    delays = (0.0, 0.05, 0.1, 0.2, 0.4, 0.8)
    for index, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if index == len(delays) - 1:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise


ARCHIVE_TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"


def _archive_timestamp(name):
    """Return the UTC timestamp embedded in an archive filename, or None."""
    if not name.startswith("events-") or not name.endswith(".jsonl.gz"):
        return None
    raw = name[len("events-"):-len(".jsonl.gz")]
    try:
        return datetime.strptime(raw, ARCHIVE_TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def evaluator_pack(evaluator_id):
    """按评估器 ID 取领域包；未知契约立即失败，不落到任何默认分支。"""
    from .domains import protocol
    try:
        return protocol.get(evaluator_id)
    except protocol.UnsupportedEvaluator as error:
        raise ProtocolError(str(error)) from error


def evaluator_metric(evaluator_id, metric_id=None):
    """取领域包的指标契约；``metric_id`` 为 None 时取 primary 指标。"""
    from .domains import metric_spec, protocol, primary_metric
    pack = evaluator_pack(evaluator_id)
    try:
        return primary_metric(pack) if metric_id is None else metric_spec(pack, metric_id)
    except protocol.UnsupportedEvaluator as error:
        raise ProtocolError(str(error)) from error


def evaluator_for_metric(metric):
    for evaluator_id, contract in EVALUATORS.items():
        if metric == contract["metric"]:
            return evaluator_id
    raise ProtocolError(f"未支持的指标契约: {canonical(metric)}")


def inside(root, relative):
    if not isinstance(relative, str) or Path(relative).is_absolute():
        raise ProtocolError("项目路径必须是相对路径")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ProtocolError(f"文件缺失或路径越界: {relative}")
    return path


def validate_spec(spec):
    required = {"name", "objective", "entrypoint", "code_files", "train", "dev", "test",
                "baseline", "candidates", "budget", "timeout_seconds",
                "min_improvement", "metric"}
    if not isinstance(spec, dict) or not required <= spec.keys():
        raise ProtocolError("experiment.json 缺少必需字段")
    evaluator_id = evaluator_for_metric(spec["metric"])
    pack = evaluator_pack(evaluator_id)
    if not isinstance(spec["code_files"], list) or spec["entrypoint"] not in spec["code_files"]:
        raise ProtocolError("entrypoint 必须注册在 code_files")
    if any(not isinstance(p, str) or not p.endswith(".py") for p in spec["code_files"]):
        raise ProtocolError("code_files 只接受 Python 源文件")
    if len(set(spec["code_files"])) != len(spec["code_files"]):
        raise ProtocolError("重复 code_files")
    provenance = spec.get("provenance_files", [])
    if (not isinstance(provenance, list) or any(not isinstance(path, str) for path in provenance)
            or len(set(provenance)) != len(provenance)):
        raise ProtocolError("provenance_files 必须是无重复的相对路径列表")
    slices = spec.get("analysis_slices", [])
    if not isinstance(slices, list) or len(slices) > 16:
        raise ProtocolError("analysis_slices must be a list with at most 16 items")
    slice_ids = set()
    for item in slices:
        if (not isinstance(item, dict) or set(item) != {"slice_id", "rule"}
                or not isinstance(item["slice_id"], str)
                or not re.fullmatch(r"s[0-9]{1,2}", item["slice_id"])
                or item["slice_id"] in slice_ids or not isinstance(item["rule"], dict)):
            raise ProtocolError("analysis_slices require unique neutral slice IDs and exact rules")
        slice_ids.add(item["slice_id"])
        rule = item["rule"]
        kind = rule.get("kind")
        if kind in {"id_prefix", "not_id_prefix"}:
            valid_rule = (set(rule) == {"kind", "value"}
                          and isinstance(rule["value"], str)
                          and 0 < len(rule["value"]) <= 32)
        elif kind in {"abs_x_le", "abs_x_gt"}:
            valid_rule = (set(rule) == {"kind", "value"}
                          and number(rule["value"]) and rule["value"] >= 0)
        else:
            valid_rule = False
        if not valid_rule:
            raise ProtocolError("invalid analysis_slices rule")
    if not isinstance(spec["baseline"], dict) or not isinstance(spec["candidates"], list):
        raise ProtocolError("baseline 必须是配置对象，candidates 必须是列表")
    if not spec["candidates"] or any(not isinstance(c, dict) for c in spec["candidates"]):
        raise ProtocolError("至少注册一个候选配置")
    keys = [digest(c) for c in [spec["baseline"], *spec["candidates"]]]
    if len(set(keys)) != len(keys):
        raise ProtocolError("基线/候选配置不得重复")
    if getattr(pack, "units_from_data", False):
        # 分析单元域包：重复单元由每个数据划分导出（区块 id 随划分不同而不同），
        # 预注册的是统计判据本身（方向比例 = 1−α、最少单元数），不是一组种子。
        seeds = spec.get("seeds", [])
        if not isinstance(seeds, list) or seeds:
            raise ProtocolError(
                "分析单元域包的重复单元由数据导出：experiment.json 必须缺省 seeds（或为空列表）")
        ratio = spec.get("significance_ratio")
        if not number(ratio) or not 0.5 < ratio < 1:
            raise ProtocolError(
                "分析单元域包必须预注册 significance_ratio：(0.5, 1) 内的方向判据 1−α")
        min_units = spec.get("min_units_for_significance", 2)
        if type(min_units) is not int or min_units < 2:
            raise ProtocolError("min_units_for_significance 必须是 ≥ 2 的整数")
    else:
        seeds = spec.get("seeds")
        if not isinstance(seeds, list) or not seeds or any(type(s) is not int for s in seeds):
            raise ProtocolError("seeds 必须是非空整数列表")
        if len(set(seeds)) != len(seeds):
            raise ProtocolError("种子不得重复")
    ratio = spec.get("significance_ratio")
    if ratio is not None and (not number(ratio) or not 0 < ratio < 1):
        raise ProtocolError("significance_ratio 必须是 (0,1) 内的有限数")
    if type(spec["budget"]) is not int or not 1 <= spec["budget"] <= len(spec["candidates"]):
        raise ProtocolError("budget 必须在 1 与候选数量之间（不含基线）")
    if not number(spec["timeout_seconds"]) or not 0 < spec["timeout_seconds"] <= 3600:
        raise ProtocolError("每个种子的 timeout_seconds 必须在 (0,3600]")
    if spec.get("mem_limit_mb") is not None and (not number(spec["mem_limit_mb"]) or spec["mem_limit_mb"] <= 0):
        raise ProtocolError("mem_limit_mb 必须是正整数（MB）；缺省不设内存上限")
    if not number(spec["min_improvement"]) or spec["min_improvement"] < 0:
        raise ProtocolError("min_improvement 必须是非负的绝对改善阈值（按主指标单位）")
    canonical(spec)


def dataset(path, evaluator_id):
    """读取并校验带标签的数据集；形状与取值完全由领域包声明。"""
    rows = read_json(path)
    evaluator_pack(evaluator_id).validate_rows(rows, None)
    return rows


def score(rows, predictions, evaluator_id, unit=None):
    """按领域包的主指标独立计分。

    ``unit`` 是当前重复单位的取值（训练种子 / 独立重复序号 / 分析单元 id）；
    逐样本与独立重复域包忽略它，分析单元域包需要据此定位单元内真值。
    """
    return evaluator_pack(evaluator_id).score(rows, predictions, None, unit=unit)


def repeat_values(pack, rows, spec):
    """本次运行在该数据划分上的重复单位取值。

    分析单元域包（``units_from_data=True``）从划分数据导出单元（如区块 id），
    训练种子/独立重复域包取预注册的 ``spec["seeds"]``。两种取值都进入同一条
    per-unit 执行与配对统计管线。
    """
    if getattr(pack, "units_from_data", False):
        values = list(pack.unit_values(rows))
        if not values:
            raise ProtocolError("该数据划分未导出任何分析单元")
        if len(values) != len(set(values)):
            raise ProtocolError("分析单元取值重复")
        if any(type(value) is not int for value in values):
            raise ProtocolError("分析单元取值必须是整数")
        return values
    return list(spec["seeds"])


def model_inputs(rows, evaluator_id):
    """候选可见的输入：去掉标签，只留领域包声明的输入字段。"""
    return evaluator_pack(evaluator_id).project_inputs(rows)


def sample_signature(row, evaluator_id):
    """去重与泄漏检查用的样本身份；未知契约在此立即失败。"""
    return evaluator_pack(evaluator_id).row_identity(row)


def _migrate_state(db):
    """把 state.db 交给统一迁移入口（表结构只定义在 popper.research.schema）。

    延迟导入：popper.core 是更底层模块，模块级导入 popper.research 会在
    `import popper.core` 时触发 research 包初始化，而后者反向导入 core，
    形成循环导入。
    """
    from .research.schema import migrate_state
    return migrate_state(db)


def _require_holdout_seal(sandboxed, split):
    """``--sandbox`` 的测试划分必须真的封得住保留集标签，封不住就显式失败。

    保留集读隔离是 ``--sandbox`` 的组成部分：只在 ``evaluate`` 里检查会让
    ``confirm`` 先提交 ``test_consumed`` 再失败，用户会在没有任何隔离的情况下
    白丢一次测试访问。因此两个入口都先过这道门，且都不静默降级为「候选可读」。
    """
    if sandboxed and split == "test" and not sandbox.seal_read_available():
        raise ProtocolError(
            "本平台无法对保留集标签做内核级禁读（Windows 强制完整性标签不可用）："
            "候选进程将能直接读取测试标签，隔离不成立。"
            "请改用 --trusted-local 并自行保证保留集不在候选进程可读范围内")


class Experiment:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / ".popper"
        if not (self.home / "state.db").is_file():
            raise ProtocolError("未初始化，请先运行 init")
        self.db = sqlite3.connect(self.home / "state.db", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        # 打开即迁移：缺表的老库在这里补齐，并写明 schema 版本。
        _migrate_state(self.db)

    def close(self):
        self.db.close()

    def state(self):
        return json.loads(self.db.execute("SELECT data FROM state WHERE id=1").fetchone()[0])

    def archive_events(self, target_dir):
        """Export the audit event chain to a gzip JSONL cold archive.

        The archive never deletes the source database; it exists so the event
        chain can be replayed and verified offline.
        """
        target_dir = Path(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        rows = self.db.execute(
            "SELECT seq, kind, payload, previous, hash FROM events ORDER BY seq").fetchall()
        timestamp = datetime.now(timezone.utc).strftime(ARCHIVE_TIMESTAMP_FORMAT)
        archive_path = target_dir / f"events-{timestamp}.jsonl.gz"
        with gzip.open(archive_path, "wt", encoding="utf-8") as handle:
            for row in rows:
                record = {"seq": row["seq"], "kind": row["kind"],
                          "payload": json.loads(row["payload"]),
                          "previous": row["previous"], "hash": row["hash"]}
                handle.write(canonical(record) + "\n")
        manifest = {
            "schema_version": "1.0",
            "count": len(rows),
            "first_hash": rows[0]["hash"] if rows else None,
            "last_hash": rows[-1]["hash"] if rows else None,
            "source_db": str(self.home / "state.db"),
            "note": "归档不删除原库，可离线重放校验",
        }
        write_json(target_dir / "manifest.json", manifest)
        moved_cold = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        for path in sorted(target_dir.glob("events-*.jsonl.gz")):
            stamp = _archive_timestamp(path.name)
            if stamp is not None and stamp < cutoff:
                cold_dir = target_dir / "cold"
                cold_dir.mkdir(exist_ok=True)
                shutil.move(str(path), str(cold_dir / path.name))
                moved_cold.append(path.name)
        return {"status": "archived", "count": len(rows),
                "path": str(archive_path), "moved_cold": moved_cold}

    def _event(self, kind, payload):
        row = self.db.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row[0] if row else "0" * 64
        item = {"kind": kind, "payload": payload, "previous": previous}
        self.db.execute("INSERT INTO events(kind,payload,previous,hash) VALUES (?,?,?,?)",
                        (kind, canonical(payload), previous, digest(item)))

    def _save(self, state, kind, payload):
        self.db.execute("UPDATE state SET data=? WHERE id=1", (canonical(state),))
        self._event(kind, payload)

    # 需用户裁决的拒稿风险判定（放行/驳回）：只记录裁定，不篡改机器评测。
    # 全程入事件溯源链（append-only + hash 校验），满足"介入留痕可回放"约束。
    ADJUDICATABLE_RISKS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")
    ADJUDICATION_VERDICTS = ("allow", "reject")

    def adjudicate(self, risk_id, verdict, reason):
        if risk_id not in self.ADJUDICATABLE_RISKS:
            raise ProtocolError(f"未知拒稿风险: {risk_id}")
        if verdict not in self.ADJUDICATION_VERDICTS:
            raise ProtocolError("裁定必须为 allow 或 reject")
        if not isinstance(reason, str) or not reason.strip():
            raise ProtocolError("裁定必须留痕理由")
        reason = reason.strip()
        state = self.state()
        ledger = state.setdefault("adjudications", {})
        entry = {"verdict": verdict, "reason": reason,
                 "at": datetime.now(timezone.utc).isoformat()}
        ledger[risk_id] = entry
        with self.db:
            self._save(state, "gate_adjudicated",
                       {"risk_id": risk_id, "verdict": verdict,
                        "reason": reason, "at": entry["at"]})
        return {"status": "adjudicated", "risk_id": risk_id,
                "verdict": verdict, "reason": reason}

    def verify_inputs(self):
        state = self.state()
        for name, expected in state["input_hashes"].items():
            if file_hash(inside(self.root, name)) != expected:
                raise ProtocolError(f"已注册输入发生修改: {name}；请建立新的实验")
        evaluator_id = state["evaluator_id"]
        if (evaluator_id not in EVALUATORS or digest(EVALUATORS[evaluator_id]) != state["evaluator_hash"]
                or evaluator_for_metric(state["spec"]["metric"]) != evaluator_id):
            raise ProtocolError("评估器版本已改变；旧实验不能静默继续")
        current = {"python": sys.version, "platform": platform.platform(), "executable": sys.executable}
        if current != state["environment"]:
            raise ProtocolError("运行环境与初始化时不同")
        return state

    def recover(self):
        """Explicit recovery only after the user ensures no other runner is active."""
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.state()
            if state["phase"] == "confirming":
                state["phase"] = "confirmation_failed"
            elif state["phase"] != "searching":
                raise ProtocolError("当前没有可恢复的执行阶段")
            rows = self.db.execute("SELECT id FROM runs WHERE status='running'").fetchall()
            for row in rows:
                self.db.execute("UPDATE runs SET status='interrupted' WHERE id=?", (row[0],))
            self._save(state, "recovered", {"interrupted": [r[0] for r in rows],
                                           "test_retry_allowed": False})
        return state

    def evaluate(self, config, split, trusted_local=False, sandboxed=False):
        if not (trusted_local or sandboxed):
            raise ProtocolError("M0 非沙箱；请使用 --trusted-local 或 --sandbox")
        if sandboxed and not sandbox.available():
            raise ProtocolError(sandbox.no_backend_message())
        _require_holdout_seal(sandboxed, split)
        rid, state, prior = self.register_run(config, split)
        if prior is not None:
            return prior
        run_dir = self.home / "runs" / rid
        run_dir.mkdir(parents=True)
        started = time.monotonic()
        try:
            evaluator_id = state["evaluator_id"]
            spec = state["spec"]
            pack = evaluator_pack(evaluator_id)
            invocation = pack.invocation()
            rows = dataset(inside(self.root, spec[split]), evaluator_id)
            # 重复单位取值：分析单元域包从本划分数据导出（区块 id），其余取预注册 seeds。
            units = repeat_values(pack, rows, spec)
            # 输入制品名由域包声明，落盘位置由本处（run_dir 布局）决定：输入与输出
            # 都直接在 run_dir 下，与既有的运行档案、bundle 证据保持一致。
            input_paths = {}
            for role, basename in invocation.inputs:
                path = run_dir / basename
                if role == "train":
                    shutil.copyfile(inside(self.root, spec["train"]), path)
                elif role == "inputs":
                    write_json(path, model_inputs(rows, evaluator_id))
                elif role == "config":
                    write_json(path, config)
                else:
                    raise ProtocolError(f"未声明的输入角色: {role!r}")
                input_paths[role] = str(path)
            # Each execution uses a fresh copy of the explicitly registered code.
            code_dir = run_dir / "code"
            for name in spec["code_files"]:
                target = code_dir / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(inside(self.root, name), target)
            points = []
            environment = {k: v for k, v in os.environ.items()
                           if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            environment.update({"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1"})
            if sandboxed:
                # 文件写作用域：仅在运行工作区（run_dir）可写。在数据/代码复制完成后
                # 标记 Low，使既有的 train/config/code 副本保持 Medium（候选无法篡改输入），
                # 候选只能在 run_dir 内新建输出文件。
                sandbox.label_write_scope(run_dir)
                (run_dir / "tmp").mkdir(exist_ok=True)
                environment.update({
                    "TMP": str(run_dir / "tmp"), "TEMP": str(run_dir / "tmp"),
                    "PYTHONPYCACHEPREFIX": str(run_dir / "pycache"),
                    "PYTHONDONTWRITEBYTECODE": "1",
                })
            try:
                # 保留集读隔离：候选进程以 Low 完整性运行，封读后它在内核层读不到测试标签
                # （控制器与用户是 Medium，不受影响）。数据已在上面读入内存，控制器在
                # 封读窗口内不需要再读该文件。
                sealed = [inside(self.root, spec["test"])] if sandboxed and split == "test" else []
                with sandbox.sealed_reads(sealed):
                    for unit in units:
                        values = dict(input_paths)
                        values["prediction"] = str(run_dir / invocation.prediction_name(unit))
                        values["seed"] = str(unit)
                        command = [sys.executable, str(code_dir / spec["entrypoint"]),
                                   *invocation.render(values)]
                        output = Path(values["prediction"])
                        with (run_dir / f"stdout-{unit}.log").open("wb") as stdout, \
                                (run_dir / f"stderr-{unit}.log").open("wb") as stderr:
                            # 执行边界只有一个入口：sandboxed 决定是否降完整性。
                            sandbox.launch(command, cwd=str(code_dir), env=environment,
                                           stdout=stdout, stderr=stderr,
                                           timeout_seconds=spec["timeout_seconds"],
                                           mem_limit_mb=spec.get("mem_limit_mb"),
                                           cpu_time_seconds=spec["timeout_seconds"],
                                           sandboxed=sandboxed)
                        points.append({"seed": unit,
                                       "value": score(rows, read_json(output), evaluator_id,
                                                      unit=unit)})
            finally:
                if sandboxed:
                    try:
                        sandbox.unlabel_write_scope(run_dir)
                    except Exception:
                        pass
            self.verify_inputs()
            # Detect accidental mutation of the executed copy as well as source inputs.
            for name in spec["code_files"]:
                if file_hash(code_dir / name) != state["input_hashes"][name]:
                    raise ProtocolError("执行副本发生变更")
            result = self.build_result(rid, state, config, split, points, started, sandboxed)
            self.complete_run(rid, result)
            return result
        except Exception as error:
            self.fail_run(rid, type(error).__name__)
            raise ProtocolError(f"执行失败 {rid}: {type(error).__name__}: {error}") from error

    def register_run(self, config, split):
        """开始一次实验运行（原子事务），返回 (rid, state, prior)。

        prior 非 None 表示该 (config, split) 已完成过，调用方直接复用其结果、无需执行；
        否则返回新的 rid 与 state，供调用方驱动执行。把「执行」从这条 DB 生命周期契约
        中分离出去，是控制器把执行迁移到 Worker（异步/可重连）而不破坏 freeze/confirm 的前提。
        """
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.verify_inputs()
            spec = state["spec"]
            if split == "dev" and state["phase"] != "searching":
                raise ProtocolError("方案冻结后禁止继续开发集搜索")
            if split == "test" and state["phase"] != "confirming":
                raise ProtocolError("测试集只能通过 confirm 入口消费一次")
            if split not in {"dev", "test"}:
                raise ProtocolError("未知数据划分")
            if split == "test" and config not in [spec["baseline"], state["selected"]]:
                raise ProtocolError("测试只接受已冻结候选及其基线")
            if self.db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise ProtocolError("已有运行中的实验；中断后显式 recover")
            config_hash = digest(config)
            allowed = [digest(spec["baseline"]), *map(digest, spec["candidates"])]
            if config_hash not in allowed:
                raise ProtocolError("未注册的候选配置")
            prior = self.db.execute(
                "SELECT result FROM runs WHERE config_hash=? AND split=? AND status='completed'",
                (config_hash, split)).fetchone()
            if prior:
                return (None, state, json.loads(prior[0]))
            baseline = config_hash == digest(spec["baseline"])
            if split == "dev" and baseline:
                attempts = self.db.execute(
                    "SELECT COUNT(*) FROM runs WHERE split='dev' AND config_hash=?",
                    (config_hash,)).fetchone()[0]
                if attempts >= 2:
                    raise ProtocolError("基线最多尝试两次，请修复项目后建立新实验")
            if split == "dev" and not baseline:
                count = self.db.execute("SELECT COUNT(*) FROM runs WHERE split='dev' AND config_hash!=?",
                                        (digest(spec["baseline"]),)).fetchone()[0]
                if count >= spec["budget"]:
                    raise ProtocolError("候选执行预算已用尽（失败/中断同样计费）")
            if split == "test" and self.db.execute(
                    "SELECT 1 FROM runs WHERE config_hash=? AND split='test'",
                    (config_hash,)).fetchone():
                raise ProtocolError("该测试已被消费，包括失败的执行")
            rid = uuid.uuid4().hex
            self.db.execute("INSERT INTO runs(id,config_hash,split,status,result) VALUES (?,?,?,'running',NULL)",
                            (rid, config_hash, split))
            self._event("run_started", {"run_id": rid, "split": split, "config": config})
            return (rid, state, None)

    @staticmethod
    def build_result(rid, state, config, split, points, started, sandboxed):
        """从 per-seed 点集构造核心结果字典（与 evaluate 的同步路径字段一致）。"""
        spec = state["spec"]
        values = [p["value"] for p in points]
        return {"schema_version": "1.0", "run_id": rid, "split": split, "config": config,
                "metric": spec["metric"], "mean": statistics.mean(values),
                "std": statistics.stdev(values) if len(values) > 1 else 0.0,
                "per_seed": points, "n_seeds": len(points),
                "input_hashes": state["input_hashes"], "evaluator_id": state["evaluator_id"],
                "evaluator_hash": state["evaluator_hash"],
                "environment": state["environment"],
                "trust": "controller_scored_os_sandbox" if sandboxed
                else "controller_scored_trusted_local",
                "elapsed_seconds": time.monotonic() - started}

    def complete_run(self, rid, result):
        """把运行标为 completed，并落 results.json + 产物 manifest（供 replay 离线校验）。"""
        run_dir = self.home / "runs" / rid
        write_json(run_dir / "results.json", result)
        manifest = {str(p.relative_to(run_dir)).replace("\\", "/"): file_hash(p)
                    for p in run_dir.rglob("*") if p.is_file()}
        with self.db:
            self.db.execute("UPDATE runs SET status='completed',result=? WHERE id=?",
                            (canonical(result), rid))
            self._event("run_completed", {"run_id": rid, "artifacts": manifest, "result": result})

    def fail_run(self, rid, error_type):
        with self.db:
            self.db.execute("UPDATE runs SET status='failed' WHERE id=?", (rid,))
            self._event("run_failed", {"run_id": rid, "error_type": error_type})

    def search(self, trusted_local=False, proposer=None, sandboxed=False):
        state = self.verify_inputs()
        if state["phase"] != "searching":
            raise ProtocolError("冻结后不可继续搜索")
        spec = state["spec"]
        self.evaluate(spec["baseline"], "dev", trusted_local, sandboxed)
        while True:
            attempted = {r[0] for r in self.db.execute("SELECT config_hash FROM runs WHERE split='dev'")}
            remaining = [c for c in spec["candidates"] if digest(c) not in attempted]
            spent = self.db.execute("SELECT COUNT(*) FROM runs WHERE split='dev' AND config_hash!=?",
                                    (digest(spec["baseline"]),)).fetchone()[0]
            if not remaining or spent >= spec["budget"]:
                break
            feedback = self.results("dev")
            proposal = proposer(remaining, feedback, spec["objective"]) if proposer else {
                "index": 0, "hypothesis": "按预注册配置队列探索；不代表自主科研创新", "source": "registered_queue"}
            if (type(proposal.get("index")) is not int or not 0 <= proposal["index"] < len(remaining)
                    or not isinstance(proposal.get("hypothesis"), str) or not proposal["hypothesis"].strip()):
                raise ProtocolError("候选提案格式不正确")
            chosen = remaining[proposal["index"]]
            with self.db:
                self._event("candidate_proposed", {"config": chosen, "proposal": proposal})
            self.evaluate(chosen, "dev", trusted_local, sandboxed)
        return self.results("dev")

    def results(self, split=None):
        rows = self.db.execute("SELECT result FROM runs WHERE status='completed' ORDER BY rowid")
        return [value for row in rows if (value := json.loads(row[0])) and (split is None or value["split"] == split)]

    def freeze(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.verify_inputs()
            if state["phase"] != "searching":
                raise ProtocolError("只允许从 searching 冻结")
            if self.db.execute("SELECT 1 FROM runs WHERE status='running'").fetchone():
                raise ProtocolError("运行尚未完成")
            results = self.results("dev")
            spec = state["spec"]
            baselines = [r for r in results if r["config"] == spec["baseline"]]
            candidates = [r for r in results if r["config"] != spec["baseline"]]
            if not baselines or not candidates:
                raise ProtocolError("需要成功的基线和至少一个候选")
            direction = spec["metric"]["direction"]
            best = (min if direction == "min" else max)(
                candidates, key=lambda r: (r["mean"], digest(r["config"])))
            state.update({"phase": "frozen", "selected": best["config"], "selected_dev_run": best["run_id"]})
            self._save(state, "frozen", {"selected": best["config"], "dev_run_id": best["run_id"]})
        return state

    def begin_confirmation(self, trusted_local=False, sandboxed=False):
        """提交 test_consumed：phase frozen → confirming，返回已验证 state。

        调用方随后自行执行 baseline/candidate 测试（保留 holdout 封读窗口），
        然后调用 ``finalize_confirmation(baseline, candidate)`` 构造 claim 并落 completed。
        必须先调用本方法，再调用 ``evaluate(..., "test", ...)`` 或控制器路径的
        ``_evaluate_via_worker``——否则 ``evaluate`` / ``register_run`` 会以
        ``phase != confirming`` 拒绝执行 test split。

        与 ``confirm`` 同步包装器相比，本方法把「执行」从确认事务中分离出去，
        让控制器能在 candidate 来自生成代码 revision 时改走 Worker 路径
        （而非用 ``evaluate`` 跑原始注册代码冒充该 revision）。
        """
        if not (trusted_local or sandboxed):
            raise ProtocolError("M0 非沙箱；请使用 --trusted-local 或 --sandbox")
        if sandboxed and not sandbox.available():
            raise ProtocolError(sandbox.no_backend_message())
        # 必须在下面提交 test_consumed 之前：否则封读不可用时用户会白丢一次测试访问。
        _require_holdout_seal(sandboxed, "test")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            state = self.verify_inputs()
            if state["phase"] != "frozen":
                raise ProtocolError("最终测试只能在冻结后消费一次")
            state["phase"] = "confirming"
            self._save(state, "test_consumed", {"selected": state["selected"]})
        return state

    def finalize_confirmation(self, baseline, candidate):
        """从已完成的 baseline/candidate 测试结果构造 claim 并落 completed。

        ``baseline`` 与 ``candidate`` 是 ``evaluate(..., "test", ...)`` 或控制器路径
        ``_evaluate_via_worker(..., split="test", ...)`` 产出的核心 result dict；
        本方法只负责统计判据、claim 构造与最终状态推进，不再触发执行。

        异常路径：phase 已是 confirming 时落 confirmation_failed 并重新抛出，
        与原 ``confirm`` 同步包装器的 ``except`` 块语义一致——任一后续失败都
        视为「这次测试访问已被消费但未能成立 claim」，禁止再次消费测试集。
        """
        state = self.state()
        if state["phase"] != "confirming":
            raise ProtocolError("finalize_confirmation 需要 phase=confirming")
        try:
            direction = state["spec"]["metric"]["direction"]
            delta = (baseline["mean"] - candidate["mean"] if direction == "min"
                     else candidate["mean"] - baseline["mean"])
            # 统计口径完全由域包声明的重复单位决定：训练种子/独立重复只作描述性
            # bootstrap；分析单元域包在预注册判据满足时允许 statistical_claim=True，
            # 此时统计判据还是 supports_threshold 的必要条件。
            from .domains.protocol import REPEAT_ANALYSIS_UNIT
            from .gates import claim_outcome, claim_stats, l1_significance
            pack = evaluator_pack(state["evaluator_id"])
            repeat_unit = pack.repeat_unit()
            baseline_per_seed = {p["seed"]: p["value"] for p in baseline["per_seed"]}
            candidate_per_seed = {p["seed"]: p["value"] for p in candidate["per_seed"]}
            stats = l1_significance(
                {"baseline_per_seed": baseline_per_seed, "candidate_per_seed": candidate_per_seed,
                 "seeds": [p["seed"] for p in baseline["per_seed"]],
                 "significance_ratio": state["spec"].get("significance_ratio", 0.5),
                 "min_units_for_significance": state["spec"].get("min_units_for_significance", 2),
                 "repeat_unit": repeat_unit, "direction": direction},
                state, random.Random(0))
            outcome = claim_outcome(delta, state["spec"]["min_improvement"], stats, repeat_unit)
            stats_claim = claim_stats(stats, repeat_unit)
            if repeat_unit == REPEAT_ANALYSIS_UNIT:
                if stats.get("statistical_claim"):
                    scope_extra = (
                        f"{stats.get('n_units')} 个分析单元自举方向一致性 "
                        f"{stats.get('p_direction_consistent'):.3f} ≥ "
                        f"{state['spec'].get('significance_ratio', 0.5):g}"
                        "（单元层抽样不确定性）")
                else:
                    scope_extra = stats.get("sanity", "分析单元证据不满足预注册统计判据")
            else:
                scope_extra = ("未做显著性检验" if stats.get("descriptive")
                               else f"{stats.get('n_seeds')} 种子方向一致性 {stats.get('p_direction_consistent'):.3f}")
            claim = {"claim_id": "C001", "status": outcome, "delta": delta,
                     "unit": f"absolute_{state['spec']['metric']['name']}_{'reduction' if direction == 'min' else 'increase'}",
                     "threshold": state["spec"]["min_improvement"],
                     "evidence_ids": [baseline["run_id"], candidate["run_id"]],
                     "scope": f"仅当前数据划分与预注册配置；{scope_extra}，不证明机制或创新性",
                     "stats": stats_claim}
            with self.db:
                state.update({"phase": "completed", "claim": claim})
                self._save(state, "confirmed", claim)
            return claim
        except Exception:
            with self.db:
                state["phase"] = "confirmation_failed"
                self._save(state, "confirmation_failed", {"test_retry_allowed": False})
            raise

    def mark_confirmation_failed(self):
        """Best-effort transition confirming → confirmation_failed（用于异常路径）。

        幂等：phase 不是 confirming 时无操作。用于控制器在 ``begin_confirmation``
        之后、``finalize_confirmation`` 之前捕获异常，使 phase 不会卡在
        ``confirming``——``confirming`` 除了 ``recover()`` 之外没有任何推进路径，
        且 ``recover`` 也只是把它转成 ``confirmation_failed``。
        """
        state = self.state()
        if state["phase"] == "confirming":
            with self.db:
                state["phase"] = "confirmation_failed"
                self._save(state, "confirmation_failed", {"test_retry_allowed": False})

    def confirm(self, trusted_local=False, sandboxed=False):
        """同步路径：begin → evaluate baseline/candidate → finalize（向后兼容）。

        直接调用方（CLI ``popper experiment confirm``、campaign_nodes、tests）仍可
        一次调用完成。控制器路径需要精细控制 candidate 评估（生成代码 revision
        走 Worker 而非 ``evaluate``），改用 ``begin_confirmation`` +
        ``evaluate`` / ``_evaluate_via_worker`` + ``finalize_confirmation``。
        """
        state = self.begin_confirmation(trusted_local, sandboxed)
        try:
            baseline = self.evaluate(state["spec"]["baseline"], "test", trusted_local, sandboxed)
            candidate = self.evaluate(state["selected"], "test", trusted_local, sandboxed)
            return self.finalize_confirmation(baseline, candidate)
        except Exception:
            self.mark_confirmation_failed()
            raise

    def replay(self):
        """Recompute metrics/claim from saved predictions; never re-executes code or LLM."""
        previous = "0" * 64
        completions = {}
        confirmed = None
        decisions = []
        for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
            payload = json.loads(row["payload"])
            if row["previous"] != previous or digest({"kind": row["kind"], "payload": payload, "previous": previous}) != row["hash"]:
                raise ProtocolError("事件链校验失败")
            previous = row["hash"]
            if row["kind"] == "run_completed":
                completions[payload["run_id"]] = payload
            elif row["kind"] == "confirmed":
                confirmed = payload
            elif row["kind"] == "candidate_proposed":
                decisions.append(payload)
        state = self.verify_inputs()
        recomputed = {}
        recomputed_points = {}
        for rid, event in completions.items():
            run_dir = self.home / "runs" / rid
            for name, expected in event["artifacts"].items():
                if file_hash(inside(run_dir, name)) != expected:
                    raise ProtocolError(f"证据文件已修改: {rid}/{name}")
            result = event["result"]
            if read_json(run_dir / "results.json") != result:
                raise ProtocolError("结果文件与事件不一致")
            evaluator_id = state["evaluator_id"]
            pack = evaluator_pack(evaluator_id)
            rows = dataset(inside(self.root, state["spec"][result["split"]]), evaluator_id)
            values = [score(rows,
                            read_json(run_dir / pack.invocation().prediction_name(p["seed"])),
                            evaluator_id, unit=p["seed"])
                      for p in result["per_seed"]]
            if values != [p["value"] for p in result["per_seed"]] or statistics.mean(values) != result["mean"]:
                raise ProtocolError("预测重算与登记指标不一致")
            recomputed[rid] = statistics.mean(values)
            recomputed_points[rid] = [(p["seed"], value)
                                      for p, value in zip(result["per_seed"], values)]
        db_results = {r["run_id"]: r for r in self.results()}
        if db_results != {rid: e["result"] for rid, e in completions.items()}:
            raise ProtocolError("投影结果与事件不一致")
        if confirmed:
            b, c = confirmed["evidence_ids"]
            direction = state["spec"]["metric"]["direction"]
            delta = recomputed[b] - recomputed[c] if direction == "min" else recomputed[c] - recomputed[b]
            # 分析单元 claim 的结论依赖单元层统计判据，replay 必须从保存的预测重算它；
            # 训练种子/独立重复 claim 的结论只依赖效应量阈值（stats 传 None）。
            from .domains.protocol import REPEAT_ANALYSIS_UNIT
            from .gates import claim_outcome, l1_significance
            repeat_unit = evaluator_pack(state["evaluator_id"]).repeat_unit()
            stats = None
            if repeat_unit == REPEAT_ANALYSIS_UNIT:
                baseline_map = dict(recomputed_points[b])
                candidate_map = dict(recomputed_points[c])
                units = [unit for unit in baseline_map if unit in candidate_map]
                stats = l1_significance(
                    {"baseline_per_seed": baseline_map, "candidate_per_seed": candidate_map,
                     "seeds": units,
                     "significance_ratio": state["spec"].get("significance_ratio", 0.5),
                     "min_units_for_significance": state["spec"].get(
                         "min_units_for_significance", 2),
                     "repeat_unit": repeat_unit, "direction": direction},
                    state, random.Random(0))
            status = claim_outcome(delta, state["spec"]["min_improvement"], stats, repeat_unit)
            if delta != confirmed["delta"] or status != confirmed["status"] or state.get("claim") != confirmed:
                raise ProtocolError("claim 重算失败")
        decisions_replayed = [
            {"step": i + 1, "config": d.get("config"),
             "source": d.get("proposal", {}).get("source"),
             "hypothesis": d.get("proposal", {}).get("hypothesis")}
            for i, d in enumerate(decisions)]
        # 完整性守卫：决策选择次数应等于非基线 dev 候选运行数（搜索每轮先提案后执行）。
        nonbase_dev = sum(
            1 for ev in completions.values()
            if ev["result"]["split"] == "dev" and ev["result"]["config"] != state["spec"]["baseline"])
        if decisions and len(decisions) != nonbase_dev:
            raise ProtocolError("决策选择链与开发集候选运行不一致")
        return {"status": "verified", "runs_recomputed": len(recomputed), "claim_recomputed": bool(confirmed),
                "decisions_replayed": decisions_replayed,
                "event_head": previous, "scope": "离线证据重算；非实验重跑，非防恶意篡改证明"}

    def report(self):
        verified = self.replay()
        state = self.state()
        lines = [f"# Popper · {state['spec']['name']}", "", f"阶段：{state['phase']}", "",
                 "执行模式：可信本地代码；控制器独立计分；尚未提供容器安全隔离。", "",
                 "## 实验记录", ""]
        for result in self.results():
            lines.extend([f"- {result['split']} · {canonical(result['config'])} · {result['metric']['name']}={result['mean']:.8g}",
                          f"  - 证据：runs/{result['run_id']}/results.json"])
        if state.get("claim"):
            claim = state["claim"]
            lines.extend(["", "## 结论 C001", "", f"状态：{claim['status']}", "",
                          f"绝对 {state['spec']['metric']['name']} 改善：{claim['delta']:.8g}；预注册阈值：{claim['threshold']}", "",
                          claim["scope"]])
        lines.extend(["", "## 审计", "", f"已从预测重算 {verified['runs_recomputed']} 个运行。",
                      "数据 id/内容交集检查不能排除所有语义泄漏；重复种子不自动意味着独立实验。",
                      "历史导入数字、引用核验、自动代码生成尚未实现。", ""])
        path = self.home / "report.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return str(path)


def initialize(root):
    root = Path(root).resolve()
    spec = read_json(root / "experiment.json")
    validate_spec(spec)
    evaluator_id = evaluator_for_metric(spec["metric"])
    files = ["experiment.json", *spec["code_files"], *spec.get("provenance_files", []),
             spec["train"], spec["dev"], spec["test"]]
    hashes = {name: file_hash(inside(root, name)) for name in files}
    sets = [dataset(inside(root, spec[s]), evaluator_id) for s in ("train", "dev", "test")]
    pack = evaluator_pack(evaluator_id)
    pack.validate_splits(sets)
    for i, left in enumerate(sets):
        for right in sets[i + 1:]:
            if {r["id"] for r in left} & {r["id"] for r in right}:
                raise ProtocolError("训练/开发/测试 id 存在交集")
            if {pack.row_identity(r) for r in left} & {pack.row_identity(r) for r in right}:
                raise ProtocolError("数据划分存在完全相同的样本")
    home = root / ".popper"
    home.mkdir(exist_ok=False)
    db = sqlite3.connect(home / "state.db")
    _migrate_state(db)
    state = {"phase": "searching", "spec": spec, "input_hashes": hashes,
             "evaluator_id": evaluator_id, "evaluator_hash": digest(EVALUATORS[evaluator_id]),
             "environment": {"python": sys.version, "platform": platform.platform(), "executable": sys.executable}}
    with db:
        db.execute("INSERT INTO state VALUES (1,?)", (canonical(state),))
        db.execute("PRAGMA optimize")
    db.close()
    experiment = Experiment(root)
    with experiment.db:
        experiment._event("initialized", state)
    experiment.close()
    return state

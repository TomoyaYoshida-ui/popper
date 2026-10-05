"""Generic, auditable wrapper for paper reproduction tasks."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .core import (ProtocolError, canonical, digest, file_hash, number, portable_path, read_json,
                   write_json)
from .eventlog import GENESIS_HASH, chained_body, verify_hash_chain
from .storage import SQLiteStorageAdapter


MANIFEST = "reproduction.json"
STATE_DIR = ".popper-reproduction"
EVENT_DB = "reproduction.sqlite"


class ReproductionEventStore:
    """Transactional snapshot plus append-only hash chain for reproduction tasks."""

    def __init__(self, home):
        self.home = Path(home).resolve()
        self.path = self.home / EVENT_DB
        self._storage = SQLiteStorageAdapter(self.path, timeout=10)

    def _connect(self):
        return self._storage.connect()

    def initialize(self, state, *, imported=False):
        self.home.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            with connection.transaction(write=True):
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS reproduction_snapshot(
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        version INTEGER NOT NULL, state TEXT NOT NULL, updated TEXT NOT NULL)
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS reproduction_events(
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL, payload TEXT NOT NULL,
                        previous_hash TEXT NOT NULL, hash TEXT NOT NULL, created TEXT NOT NULL)
                """)
                existing = connection.execute(
                    "SELECT 1 FROM reproduction_snapshot WHERE singleton=1").fetchone()
                if existing is not None:
                    return self.read()
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                event_type = "legacy_imported" if imported else "registered"
                payload = {"state": state, "migration": imported}
                body, event_hash = chained_body(
                    {"event_type": event_type, "payload": payload, "created": now},
                    GENESIS_HASH)
                connection.execute(
                    "INSERT INTO reproduction_events(event_type,payload,previous_hash,hash,created) "
                    "VALUES(?,?,?,?,?)",
                    (event_type, canonical(payload), GENESIS_HASH, event_hash, now))
                connection.execute(
                    "INSERT INTO reproduction_snapshot(singleton,version,state,updated) "
                    "VALUES(1,1,?,?)", (canonical(state), now))
        finally:
            connection.close()
        return state

    def read(self):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT state FROM reproduction_snapshot WHERE singleton=1").fetchone()
            if row is None:
                raise ProtocolError("复现事件存储缺少权威快照")
            state = json.loads(row[0])
            integrity = self.verify(connection)
            if not integrity["ok"]:
                raise ProtocolError(f"复现事件存储完整性失败: {integrity['reason']}")
            return state
        finally:
            connection.close()

    def transition(self, expected_phases, state, event_type, payload=None):
        expected_phases = set(expected_phases)
        connection = self._connect()
        try:
            with connection.transaction(write=True):
                row = connection.execute(
                    "SELECT version,state FROM reproduction_snapshot WHERE singleton=1").fetchone()
                if row is None:
                    raise ProtocolError("复现事件存储缺少权威快照")
                current = json.loads(row[1])
                if current.get("phase") not in expected_phases:
                    raise ProtocolError(
                        f"复现状态已并发变化: {current.get('phase')} 不在 {sorted(expected_phases)}")
                prior = connection.execute(
                    "SELECT hash FROM reproduction_events ORDER BY seq DESC LIMIT 1").fetchone()
                previous = prior[0] if prior else GENESIS_HASH
                now = datetime.now().astimezone().isoformat(timespec="seconds")
                event_payload = {"state": state, **(payload or {})}
                body, event_hash = chained_body(
                    {"event_type": event_type, "payload": event_payload, "created": now},
                    previous)
                connection.execute(
                    "INSERT INTO reproduction_events(event_type,payload,previous_hash,hash,created) "
                    "VALUES(?,?,?,?,?)",
                    (event_type, canonical(event_payload), previous, event_hash, now))
                connection.execute(
                    "UPDATE reproduction_snapshot SET version=?,state=?,updated=? WHERE singleton=1",
                    (row[0] + 1, canonical(state), now))
        finally:
            connection.close()
        # SQLite is authoritative.  The JSON file is a best-effort compatibility
        # projection, so a projection failure must not turn a committed event
        # into an apparent operation failure.
        try:
            write_json(self.home / "state.json", state)
        except OSError:
            pass
        return state

    def verify(self, connection=None):
        own = connection is None
        connection = connection or self._connect()
        try:
            rows = list(connection.execute(
                "SELECT seq,event_type,payload,previous_hash,hash,created "
                "FROM reproduction_events ORDER BY seq"))
            integrity = verify_hash_chain(
                rows,
                lambda row: {"event_type": row[1], "payload": json.loads(row[2]),
                             "created": row[5]},
                lambda row: row[3], lambda row: row[4],
                label="复现事件链", seq_for=lambda row: row[0])
            if not integrity["ok"]:
                return {"ok": False, "reason": integrity["reason"]}
            last_state = None
            last_phase = None
            count = 0
            for row in rows:
                payload = json.loads(row[2])
                if not isinstance(payload.get("state"), dict):
                    return {"ok": False, "reason": f"事件 seq={row[0]} 缺少状态投影"}
                last_state = payload["state"]
                phase = last_state.get("phase")
                event_type = row[1]
                legal = {
                    "execution_started": ({"registered"}, "running"),
                    "execution_completed": ({"running"}, "executed"),
                    "execution_failed": ({"running"}, "execution_failed"),
                    "verification_started": ({"executed", "verified"}, "verifying"),
                    "verification_completed": ({"verifying"}, "verified"),
                    "verification_failed": ({"verifying"}, "verification_failed"),
                }
                if count == 0:
                    if event_type not in {"registered", "legacy_imported"}:
                        return {"ok": False, "reason": "事件链首项不是注册或迁移事件"}
                    if event_type == "registered" and phase != "registered":
                        return {"ok": False, "reason": "注册事件状态不合法"}
                elif event_type == "interrupted":
                    expected = {"running": "execution_interrupted",
                                "verifying": "verification_interrupted"}.get(last_phase)
                    if phase != expected:
                        return {"ok": False, "reason": f"事件 seq={row[0]} 中断迁移不合法"}
                elif event_type in legal:
                    sources, target = legal[event_type]
                    if last_phase not in sources or phase != target:
                        return {"ok": False, "reason": f"事件 seq={row[0]} 状态迁移不合法"}
                else:
                    return {"ok": False, "reason": f"事件 seq={row[0]} 类型未知"}
                last_phase = phase
                count += 1
            snapshot = connection.execute(
                "SELECT state FROM reproduction_snapshot WHERE singleton=1").fetchone()
            if snapshot is None or json.loads(snapshot[0]) != last_state:
                return {"ok": False, "reason": "快照与事件链末端不一致"}
            return {"ok": True, "events": count, "head": integrity["head"]}
        finally:
            if own:
                connection.close()

    def events(self):
        connection = self._connect()
        try:
            return [{"seq": row[0], "event_type": row[1],
                     "payload": json.loads(row[2]), "previous_hash": row[3],
                     "hash": row[4], "created": row[5]}
                    for row in connection.execute(
                        "SELECT seq,event_type,payload,previous_hash,hash,created "
                        "FROM reproduction_events ORDER BY seq")]
        finally:
            connection.close()


def _https_url(value, name):
    if not isinstance(value, str) or urlparse(value).scheme != "https" or not urlparse(value).netloc:
        raise ProtocolError(f"{name} 必须是完整 HTTPS URL")


def validate_paper_record(paper):
    required = {"schema_version", "title", "authors", "year", "venue", "doi",
                "landing_page", "claim"}
    if not isinstance(paper, dict) or set(paper) != required or paper["schema_version"] != "1.0":
        raise ProtocolError("paper.json 不符合 1.0 契约")
    if (not isinstance(paper["title"], str) or not paper["title"].strip()
            or not isinstance(paper["venue"], str) or not paper["venue"].strip()
            or type(paper["year"]) is not int or not 1400 <= paper["year"] <= 2200
            or not isinstance(paper["authors"], list) or not paper["authors"]
            or any(not isinstance(author, str) or not author.strip() for author in paper["authors"])):
        raise ProtocolError("paper.json 的题名、作者、年份或来源不合法")
    if not isinstance(paper["doi"], str) or not paper["doi"].startswith("10."):
        raise ProtocolError("paper.json 缺少有效 DOI")
    _https_url(paper["landing_page"], "landing_page")
    claim = paper["claim"]
    if not isinstance(claim, dict) or set(claim) != {"id", "text", "metric", "reported_value", "evidence"}:
        raise ProtocolError("paper.json claim 字段不符合契约")
    if (not isinstance(claim["id"], str) or not claim["id"].strip()
            or not isinstance(claim["text"], str) or not claim["text"].strip()
            or not number(claim["reported_value"])):
        raise ProtocolError("论文 claim 的 id、文本或报告值不合法")
    metric = claim["metric"]
    if (not isinstance(metric, dict) or set(metric) != {"name", "direction", "unit"}
            or not isinstance(metric["name"], str) or not metric["name"].strip()
            or metric["direction"] not in {"min", "max"}
            or not isinstance(metric["unit"], str) or not metric["unit"].strip()):
        raise ProtocolError("论文 claim 缺少明确的指标契约")
    evidence = claim["evidence"]
    if (not isinstance(evidence, dict) or set(evidence) != {"location", "source_url", "support"}
            or not isinstance(evidence["location"], str) or not evidence["location"].strip()
            or evidence["support"] not in {"direct", "indirect", "unverified"}):
        raise ProtocolError("论文 claim 缺少可定位的证据记录")
    _https_url(evidence["source_url"], "claim.evidence.source_url")
    canonical(paper)


def validate_reproduction_protocol(protocol, paper):
    required = {"schema_version", "frozen_before_execution", "dataset", "target_claim"}
    if (not isinstance(protocol, dict) or not required <= set(protocol)
            or protocol["schema_version"] != "1.0" or protocol["frozen_before_execution"] is not True):
        raise ProtocolError("protocol.json 必须是执行前冻结的 1.0 协议")
    target = protocol["target_claim"]
    if not isinstance(target, dict) or set(target) != {"id", "metric", "reported_value", "absolute_tolerance"}:
        raise ProtocolError("protocol target_claim 字段不符合契约")
    claim = paper["claim"]
    if (target["id"] != claim["id"] or target["metric"] != claim["metric"]
            or target["reported_value"] != claim["reported_value"]):
        raise ProtocolError("protocol target_claim 与 paper.json 主张不一致")
    if not number(target["absolute_tolerance"]) or target["absolute_tolerance"] < 0:
        raise ProtocolError("absolute_tolerance 必须是非负有限数值")
    dataset = protocol["dataset"]
    if (not isinstance(dataset, dict) or not {"name", "official_url", "expected_sha256"} <= set(dataset)
            or not isinstance(dataset["name"], str) or not dataset["name"].strip()
            or not isinstance(dataset["expected_sha256"], str) or len(dataset["expected_sha256"]) != 64):
        raise ProtocolError("protocol dataset 缺少名称、官方地址或 SHA-256")
    _https_url(dataset["official_url"], "dataset.official_url")
    canonical(protocol)


def inspect_reproduction(root):
    root = Path(root).resolve()
    manifest = read_json(root / MANIFEST)
    validate_manifest(manifest)
    paper = read_json(_relative(root, manifest["paper_record"]))
    protocol = read_json(_relative(root, manifest["protocol"]))
    validate_paper_record(paper)
    validate_reproduction_protocol(protocol, paper)
    for name in (manifest["runner"], manifest["verifier"]):
        _relative(root, name)
    warnings = []
    if paper["claim"]["evidence"]["support"] != "direct":
        warnings.append("论文主张尚未标记为正文或摘要直接支持")
    exactness = protocol.get("decision_rule", {}).get("method_exactness", "")
    if "approximate" in exactness.lower():
        warnings.append("协议声明为方法近似复现，报告不得表述为算法完全复现")
    return {"status": "ready", "task_id": manifest["id"], "claim_id": paper["claim"]["id"],
            "metric": paper["claim"]["metric"], "reported_value": paper["claim"]["reported_value"],
            "dataset": protocol["dataset"]["name"], "evidence_support": paper["claim"]["evidence"]["support"],
            "warnings": warnings}


def validate_manifest(manifest):
    required = {"schema_version", "id", "title", "paper_record", "protocol",
                "runner", "verifier", "required_outputs", "timeout_seconds"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ProtocolError("reproduction.json 字段不完整或包含未知字段")
    if manifest["schema_version"] != "1.0":
        raise ProtocolError("不支持的复现任务契约版本")
    for name in ("id", "title"):
        if not isinstance(manifest[name], str) or not manifest[name].strip():
            raise ProtocolError(f"{name} 必须是非空字符串")
    inputs = [manifest[name] for name in ("paper_record", "protocol", "runner", "verifier")]
    outputs = manifest["required_outputs"]
    if (any(not isinstance(path, str) for path in inputs)
            or not isinstance(outputs, list) or not outputs
            or any(not isinstance(path, str) for path in outputs)):
        raise ProtocolError("输入和 required_outputs 必须使用相对路径")
    if len(set(inputs)) != len(inputs) or len(set(outputs)) != len(outputs):
        raise ProtocolError("复现任务包含重复路径")
    if set(inputs) & set(outputs):
        raise ProtocolError("输入文件不能同时声明为输出")
    if not manifest["runner"].endswith(".py") or not manifest["verifier"].endswith(".py"):
        raise ProtocolError("runner 和 verifier 必须是 Python 文件")
    timeout = manifest["timeout_seconds"]
    if type(timeout) not in (int, float) or not 0 < timeout <= 86400:
        raise ProtocolError("timeout_seconds 必须在 (0,86400] 范围内")
    canonical(manifest)


def _relative(root, relative, must_exist=True):
    normalized = portable_path(relative)
    if normalized is None:
        raise ProtocolError("复现任务路径必须是相对路径")
    path = (root / normalized).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ProtocolError(f"复现任务路径越界: {relative}")
    if must_exist and not path.is_file():
        raise ProtocolError(f"复现任务文件缺失: {relative}")
    return path


def _environment():
    return {"python": sys.version, "platform": platform.platform(),
            "executable": sys.executable}


def _claim_assessment(root, state, result):
    paper = read_json(_relative(root, state["manifest"]["paper_record"]))
    protocol = read_json(_relative(root, state["manifest"]["protocol"]))
    validate_paper_record(paper)
    validate_reproduction_protocol(protocol, paper)
    target = protocol["target_claim"]
    metric = target["metric"]
    field = metric["name"]
    value = result.get(field)
    # Older frozen verifiers with multiple methods used ``primary_<metric>``
    # for the preregistered primary estimate. Do not rewrite their hashed
    # verifier or protocol; accept that unambiguous legacy spelling only when
    # the canonical field is absent. New verifiers should emit the metric name.
    if value is None and field not in result:
        legacy_field = f"primary_{field}"
        if legacy_field in result:
            field = legacy_field
            value = result[field]
    if not number(value):
        raise ProtocolError(
            f"核验器结果必须包含有限数值指标: {metric['name']}")
    difference = abs(float(value) - float(target["reported_value"]))
    tolerance = float(target["absolute_tolerance"])
    assessment = {
        "claim_id": target["id"], "metric": metric, "observed_value": float(value),
        "reported_value": float(target["reported_value"]),
        "absolute_difference": difference, "absolute_tolerance": tolerance,
        "outcome": "supports_reproduction" if difference <= tolerance
                   else "does_not_reproduce",
    }
    if field != metric["name"]:
        assessment["result_field"] = field
    return assessment


def reproduction_research_projection(root, state=None):
    """Project a frozen reproduction into the shared scientific graph vocabulary."""
    root = Path(root).resolve()
    state = state or ReproductionTask(root).audit()
    manifest = state["manifest"]
    paper = read_json(_relative(root, manifest["paper_record"]))
    protocol = read_json(_relative(root, manifest["protocol"]))
    task_id = manifest["id"]
    study_id = "S-repro-" + digest({"task_id": task_id,
                                     "inputs": state["input_hashes"]})[:16]
    protocol_id = "PRT-" + digest(protocol)[:20]
    source_id = "SRC-" + digest({"doi": paper["doi"],
                                  "claim": paper["claim"]})[:20]
    code_id = "RVC-" + digest({name: state["input_hashes"][name] for name in (
        manifest["runner"], manifest["verifier"])})[:20]
    run_id = "RUN-repro-" + digest({"study_id": study_id,
                                     "code_id": code_id})[:16]
    nodes = [
        {"kind": "study", "id": study_id, "status": state["phase"]},
        {"kind": "external_source", "id": source_id, "doi": paper["doi"],
         "claim_id": paper["claim"]["id"]},
        {"kind": "reproduction_protocol", "id": protocol_id,
         "status": "frozen", "sha256": state["input_hashes"][manifest["protocol"]]},
        {"kind": "code_revision", "id": code_id, "status": "immutable_inputs",
         "files": 2, "source_control": {"kind": "reproduction_snapshot"}},
    ]
    edges = [
        {"source": {"kind": "external_source", "id": source_id},
         "target": {"kind": "study", "id": study_id}, "relation": "motivates"},
        {"source": {"kind": "study", "id": study_id},
         "target": {"kind": "reproduction_protocol", "id": protocol_id},
         "relation": "preregistered_as"},
        {"source": {"kind": "reproduction_protocol", "id": protocol_id},
         "target": {"kind": "code_revision", "id": code_id},
         "relation": "implemented_by"},
    ]
    runs, observations, decisions, issues = [], [], [], []
    if state.get("run"):
        run = state["run"]
        run_status = "succeeded" if state["phase"] in {"executed", "verified"} else state["phase"]
        nodes.append({"kind": "run", "id": run_id, "status": run_status})
        edges.append({"source": {"kind": "code_revision", "id": code_id},
                      "target": {"kind": "run", "id": run_id},
                      "relation": "executed_as"})
        runs.append({"entity_id": run_id, "status": run_status,
                     "revision_id": code_id, "output_hashes": run.get("output_hashes", {})})
        for name, sha in sorted((run.get("output_hashes") or {}).items()):
            artifact_id = "ART-" + digest({"run_id": run_id, "path": name,
                                            "sha256": sha})[:20]
            nodes.append({"kind": "artifact", "id": artifact_id, "path": name,
                          "sha256": sha})
            edges.append({"source": {"kind": "run", "id": run_id},
                          "target": {"kind": "artifact", "id": artifact_id},
                          "relation": "produced"})
    verification = state.get("verification")
    if verification:
        try:
            assessment = _claim_assessment(root, state, verification["result"])
            observation_id = "OBS-repro-" + digest({"run_id": run_id,
                                                      "assessment": assessment})[:16]
            decision_id = "DEC-repro-" + digest({"observation_id": observation_id,
                                                  "outcome": assessment["outcome"]})[:16]
            nodes.extend([
                {"kind": "observation", "id": observation_id,
                 "status": "recorded", "value": assessment["observed_value"],
                 "unit": assessment["metric"]["unit"]},
                {"kind": "decision", "id": decision_id, "status": "recorded",
                 "action": assessment["outcome"]},
            ])
            edges.extend([
                {"source": {"kind": "run", "id": run_id},
                 "target": {"kind": "observation", "id": observation_id},
                 "relation": "observed_as"},
                {"source": {"kind": "observation", "id": observation_id},
                 "target": {"kind": "decision", "id": decision_id},
                 "relation": "supports"},
                {"source": {"kind": "external_source", "id": source_id},
                 "target": {"kind": "decision", "id": decision_id},
                 "relation": "compared_against"},
            ])
            observations.append({"entity_id": observation_id, "run_id": run_id,
                                 **assessment})
            decisions.append({"entity_id": decision_id, "observation_refs": [observation_id],
                              "action": assessment["outcome"]})
        except (KeyError, ProtocolError) as error:
            issues.append({"code": "reproduction_verification_invalid",
                           "entity_id": task_id, "detail": str(error)})
    return {"schema_version": "1.0", "kind": "reproduction",
            "study": {"entity_id": study_id, "question": paper["claim"]["text"],
                      "status": state["phase"]},
            "runs": runs, "observations": observations, "decisions": decisions,
            "experiment_graph": {"schema_version": "1.0", "study_id": study_id,
                                 "nodes": nodes, "edges": edges,
                                 "integrity": {"ok": not issues, "issues": issues}}}


def initialize_reproduction(root):
    root = Path(root).resolve()
    manifest = read_json(root / MANIFEST)
    validate_manifest(manifest)
    readiness = inspect_reproduction(root)
    inputs = [MANIFEST, manifest["paper_record"], manifest["protocol"],
              manifest["runner"], manifest["verifier"]]
    hashes = {name: file_hash(_relative(root, name)) for name in inputs}
    for name in manifest["required_outputs"]:
        _relative(root, name, must_exist=False)
    home = root / STATE_DIR
    home.mkdir(exist_ok=False)
    state = {"schema_version": "1.0", "phase": "registered", "manifest": manifest,
             "input_hashes": hashes, "environment": _environment(), "run": None,
             "verification": None, "readiness": readiness}
    write_json(home / "state.json", state)
    ReproductionEventStore(home).initialize(state)
    return state


class ReproductionTask:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / STATE_DIR
        state_path = self.home / "state.json"
        database_path = self.home / EVENT_DB
        if not state_path.is_file() and not database_path.is_file():
            raise ProtocolError("复现任务未注册，请先运行 reproduce init")
        self.events_store = ReproductionEventStore(self.home)
        if not self.events_store.path.is_file():
            # Explicit legacy import: preserve the old snapshot and label the
            # synthetic event as migration rather than inventing past history.
            self.events_store.initialize(read_json(state_path), imported=True)

    def state(self):
        return self.events_store.read()

    def _save(self, state, expected_phases, event_type, payload=None):
        return self.events_store.transition(expected_phases, state, event_type, payload)

    def verify_inputs(self, state=None):
        state = state or self.state()
        for name, expected in state["input_hashes"].items():
            if file_hash(_relative(self.root, name)) != expected:
                raise ProtocolError(f"已冻结的复现输入发生修改: {name}")
        if _environment() != state["environment"]:
            raise ProtocolError("运行环境与任务注册时不同")
        return state

    def _command(self, script, log_prefix, timeout):
        environment = {key: value for key, value in os.environ.items()
                       if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP",
                                          "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"}}
        environment.update({"PYTHONHASHSEED": "0", "OMP_NUM_THREADS": "1"})
        stdout_path = self.home / f"{log_prefix}.stdout.log"
        stderr_path = self.home / f"{log_prefix}.stderr.log"
        started = time.monotonic()
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            completed = subprocess.run(
                [sys.executable, str(_relative(self.root, script))], cwd=self.root,
                env=environment, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                timeout=timeout, check=False)
        return completed.returncode, time.monotonic() - started, stdout_path, stderr_path

    def _output_hashes(self, manifest):
        return {name: file_hash(_relative(self.root, name))
                for name in manifest["required_outputs"]}

    def run(self, trusted_local=False):
        if not trusted_local:
            raise ProtocolError("复现脚本会执行本地代码；请显式使用 --trusted-local")
        state = self.verify_inputs()
        if state["phase"] != "registered":
            raise ProtocolError("复现任务只能执行一次；如需重跑请建立新任务")
        manifest = state["manifest"]
        state["phase"] = "running"
        self._save(state, {"registered"}, "execution_started")
        try:
            code, elapsed, stdout, stderr = self._command(
                manifest["runner"], "run", manifest["timeout_seconds"])
        except (OSError, subprocess.SubprocessError) as error:
            state["phase"] = "execution_failed"
            state["run"] = {"error_type": type(error).__name__}
            self._save(state, {"running"}, "execution_failed",
                       {"error_type": type(error).__name__})
            raise ProtocolError(f"复现实验执行失败: {type(error).__name__}: {error}") from error
        if code != 0:
            state["phase"] = "execution_failed"
            state["run"] = {"returncode": code, "elapsed_seconds": elapsed,
                            "stdout": stdout.name, "stderr": stderr.name}
            self._save(state, {"running"}, "execution_failed", {"returncode": code})
            raise ProtocolError(f"复现实验执行失败；查看 {stderr}")
        try:
            self.verify_inputs(state)
            output_hashes = self._output_hashes(manifest)
        except (OSError, KeyError, ProtocolError) as error:
            state["phase"] = "execution_failed"
            state["run"] = {"returncode": code, "elapsed_seconds": elapsed,
                            "stdout": stdout.name, "stderr": stderr.name,
                            "error_type": type(error).__name__}
            self._save(state, {"running"}, "execution_failed",
                       {"error_type": type(error).__name__, "reason": str(error)})
            raise ProtocolError(f"复现实验产物或冻结输入校验失败: {error}") from error
        state["run"] = {"returncode": code, "elapsed_seconds": elapsed,
                        "stdout": stdout.name, "stderr": stderr.name,
                        "output_hashes": output_hashes}
        state["phase"] = "executed"
        self._save(state, {"running"}, "execution_completed",
                   {"output_hashes": state["run"]["output_hashes"]})
        return self.verify(trusted_local=True)

    def verify(self, trusted_local=False):
        if not trusted_local:
            raise ProtocolError("核验器是任务登记的本地代码；请显式使用 --trusted-local")
        state = self.verify_inputs()
        if state["phase"] not in {"executed", "verified"} or not state["run"]:
            raise ProtocolError("复现实验尚未成功执行")
        current = self._output_hashes(state["manifest"])
        if current != state["run"]["output_hashes"]:
            raise ProtocolError("复现产物在执行后发生修改")
        prior_phase = state["phase"]
        state["phase"] = "verifying"
        self._save(state, {prior_phase}, "verification_started")
        try:
            code, elapsed, stdout, stderr = self._command(
                state["manifest"]["verifier"], "verify", state["manifest"]["timeout_seconds"])
        except (OSError, subprocess.SubprocessError) as error:
            state["phase"] = "verification_failed"
            self._save(state, {"verifying"}, "verification_failed",
                       {"error_type": type(error).__name__})
            raise ProtocolError(f"复现证据核验失败: {type(error).__name__}: {error}") from error
        if code != 0:
            state["phase"] = "verification_failed"
            self._save(state, {"verifying"}, "verification_failed", {"returncode": code})
            raise ProtocolError(f"复现证据核验失败；查看 {stderr}")
        try:
            result = json.loads(stdout.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            state["phase"] = "verification_failed"
            self._save(state, {"verifying"}, "verification_failed",
                       {"error_type": type(error).__name__})
            raise ProtocolError("核验器标准输出必须是单个 JSON 对象") from error
        if not isinstance(result, dict) or result.get("status") != "verified":
            state["phase"] = "verification_failed"
            self._save(state, {"verifying"}, "verification_failed",
                       {"reason": "verifier_status"})
            raise ProtocolError("核验器未返回 status=verified")
        try:
            assessment = _claim_assessment(self.root, state, result)
        except (KeyError, TypeError, ValueError, ProtocolError) as error:
            state["phase"] = "verification_failed"
            self._save(state, {"verifying"}, "verification_failed",
                       {"error_type": type(error).__name__, "reason": str(error)})
            raise
        state["phase"] = "verified"
        state["verification"] = {"returncode": code, "elapsed_seconds": elapsed,
                                 "stdout": stdout.name, "stderr": stderr.name,
                                 "result": result, "assessment": assessment}
        self._save(state, {"verifying"}, "verification_completed",
                   {"assessment": assessment})
        return {"task_id": state["manifest"]["id"], "phase": state["phase"],
                "outputs": state["run"]["output_hashes"], "verification": result,
                "assessment": assessment}

    def audit(self):
        state = self.verify_inputs()
        if state["run"]:
            current = self._output_hashes(state["manifest"])
            if current != state["run"]["output_hashes"]:
                raise ProtocolError("复现产物在执行后发生修改")
        if state.get("verification"):
            expected = _claim_assessment(self.root, state, state["verification"]["result"])
            stored = state["verification"].get("assessment")
            if stored is not None and stored != expected:
                raise ProtocolError("复现主张裁定与冻结协议或核验结果不一致")
        return state

    def recover_interrupted(self):
        state = self.state()
        if state["phase"] not in {"running", "verifying"}:
            return state
        prior = state["phase"]
        state["phase"] = ("execution_interrupted" if prior == "running"
                          else "verification_interrupted")
        return self._save(state, {prior}, "interrupted", {"interrupted_phase": prior})

    def event_history(self):
        return {"events": self.events_store.events(),
                "integrity": self.events_store.verify()}

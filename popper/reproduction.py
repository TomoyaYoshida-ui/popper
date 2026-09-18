"""Generic, auditable wrapper for paper reproduction tasks."""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .core import (ProtocolError, canonical, file_hash, number, portable_path, read_json,
                   write_json)


MANIFEST = "reproduction.json"
STATE_DIR = ".popper-reproduction"


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
    return state


class ReproductionTask:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.home = self.root / STATE_DIR
        if not (self.home / "state.json").is_file():
            raise ProtocolError("复现任务未注册，请先运行 reproduce init")

    def state(self):
        return read_json(self.home / "state.json")

    def _save(self, state):
        write_json(self.home / "state.json", state)

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
        try:
            code, elapsed, stdout, stderr = self._command(
                manifest["runner"], "run", manifest["timeout_seconds"])
        except (OSError, subprocess.SubprocessError) as error:
            state["phase"] = "execution_failed"
            state["run"] = {"error_type": type(error).__name__}
            self._save(state)
            raise ProtocolError(f"复现实验执行失败: {type(error).__name__}: {error}") from error
        if code != 0:
            state["phase"] = "execution_failed"
            state["run"] = {"returncode": code, "elapsed_seconds": elapsed,
                            "stdout": stdout.name, "stderr": stderr.name}
            self._save(state)
            raise ProtocolError(f"复现实验执行失败；查看 {stderr}")
        self.verify_inputs(state)
        state["run"] = {"returncode": code, "elapsed_seconds": elapsed,
                        "stdout": stdout.name, "stderr": stderr.name,
                        "output_hashes": self._output_hashes(manifest)}
        state["phase"] = "executed"
        self._save(state)
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
        try:
            code, elapsed, stdout, stderr = self._command(
                state["manifest"]["verifier"], "verify", state["manifest"]["timeout_seconds"])
        except (OSError, subprocess.SubprocessError) as error:
            state["phase"] = "verification_failed"
            self._save(state)
            raise ProtocolError(f"复现证据核验失败: {type(error).__name__}: {error}") from error
        if code != 0:
            state["phase"] = "verification_failed"
            self._save(state)
            raise ProtocolError(f"复现证据核验失败；查看 {stderr}")
        try:
            result = json.loads(stdout.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            state["phase"] = "verification_failed"
            self._save(state)
            raise ProtocolError("核验器标准输出必须是单个 JSON 对象") from error
        if not isinstance(result, dict) or result.get("status") != "verified":
            state["phase"] = "verification_failed"
            self._save(state)
            raise ProtocolError("核验器未返回 status=verified")
        state["phase"] = "verified"
        state["verification"] = {"returncode": code, "elapsed_seconds": elapsed,
                                 "stdout": stdout.name, "stderr": stderr.name,
                                 "result": result}
        self._save(state)
        return {"task_id": state["manifest"]["id"], "phase": state["phase"],
                "outputs": state["run"]["output_hashes"], "verification": result}

    def audit(self):
        state = self.verify_inputs()
        if state["run"]:
            current = self._output_hashes(state["manifest"])
            if current != state["run"]["output_hashes"]:
                raise ProtocolError("复现产物在执行后发生修改")
        return state

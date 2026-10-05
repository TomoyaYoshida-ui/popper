"""Review and preregister a local project without overwriting existing inputs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from ..core import (Experiment, ProtocolError, digest, evaluator_for_metric,
                    evaluator_pack, initialize, normalized_spec_path, read_json,
                    validate_spec, write_json)


def _target(root, name):
    normalized = normalized_spec_path("project_file", name)
    root = Path(root).resolve()
    target = (root / normalized).resolve()
    if not target.is_relative_to(root):
        raise ProtocolError("项目文件路径越出项目目录")
    return target


def _prepare(project_path, protocol, files):
    if not isinstance(project_path, str) or not project_path:
        raise ProtocolError("项目路径必须是绝对路径")
    project = Path(project_path)
    if not project.root or (os.name == "nt" and not project.drive):
        raise ProtocolError("项目路径必须是绝对路径")
    if project.is_symlink() or project.name in {"", ".", ".."}:
        raise ProtocolError("项目路径不合法或是符号链接")
    project = project.resolve()
    if not project.parent.is_dir() or (project.exists() and not project.is_dir()):
        raise ProtocolError("项目父目录不存在或目标不是目录")
    if project.name in {".popper", "dist", "build"}:
        raise ProtocolError("项目目录名称不适合科研项目")
    if files is None:
        files = {}
    if (not isinstance(files, dict) or len(files) > 64
            or any(not isinstance(name, str) or not isinstance(content, str)
                   for name, content in files.items())):
        raise ProtocolError("项目文件须是至多 64 项的 UTF-8 文本映射")
    if sum(len(content.encode("utf-8")) for content in files.values()) > 16 * 1024 * 1024:
        raise ProtocolError("项目文件总量超过 16 MiB")
    if "experiment.json" in files:
        raise ProtocolError("协议应通过 protocol 字段提交")
    if protocol is None:
        if not (project / "experiment.json").is_file():
            raise ProtocolError("项目缺少 experiment.json，请填写协议草稿")
        protocol = read_json(project / "experiment.json")
    if not isinstance(protocol, dict):
        raise ProtocolError("协议草稿必须是 JSON 对象")
    spec = json.loads(json.dumps(protocol, ensure_ascii=False))
    validate_spec(spec)
    registered = [*spec["code_files"], *spec.get("provenance_files", []),
                  spec["train"], spec["dev"], spec["test"]]
    if len(set(registered)) != len(registered):
        raise ProtocolError("协议重复登记了项目文件")
    if set(files) - set(registered):
        raise ProtocolError("提供的项目文件必须由协议登记")
    if (project / "experiment.json").is_file() and read_json(project / "experiment.json") != spec:
        raise ProtocolError("已有 experiment.json 与协议草稿不一致，不能覆盖")
    if project.exists() and (project / ".popper").exists() and not (project / ".popper" / "state.db").is_file():
        raise ProtocolError("项目存在未完成的初始化目录；请审查现场，不能覆盖")
    sources = {}
    for name in registered:
        if name in files:
            content = files[name].encode("utf-8")
            if len(content) > 4 * 1024 * 1024:
                raise ProtocolError(f"单个项目文件超过 4 MiB: {name}")
        else:
            path = _target(project, name)
            if not path.is_file():
                raise ProtocolError(f"缺少已登记输入: {name}")
            if path.stat().st_size > 16 * 1024 * 1024:
                raise ProtocolError(f"单个项目输入超过预览上限: {name}")
            content = path.read_bytes()
        existing = _target(project, name)
        if existing.exists() and existing.read_bytes() != content:
            raise ProtocolError(f"已有项目文件与提交内容不同，不能覆盖: {name}")
        sources[name] = content
    return project, spec, files, sources


def preview_project_preparation(project_path, protocol=None, files=None):
    project, spec, files, sources = _prepare(project_path, protocol, files)
    with tempfile.TemporaryDirectory(prefix="popper-project-preview-") as stage_name:
        stage = Path(stage_name)
        write_json(stage / "experiment.json", spec)
        for name, content in sources.items():
            target = _target(stage, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        initialize(stage)
    initialized = (project / ".popper" / "state.db").is_file()
    if initialized:
        exp = Experiment(project, readonly=True)
        try:
            state = exp.verify_inputs()
            if state["spec"] != spec:
                raise ProtocolError("已有预注册协议与草稿不一致")
        finally:
            exp.close()
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()}
    request = {"project_path": str(project), "spec": spec, "input_hashes": hashes}
    evaluator = evaluator_pack(evaluator_for_metric(spec["metric"]))
    return {"project_path": str(project), "name": spec["name"],
            "objective": spec["objective"], "metric": spec["metric"],
            "domain_pack": evaluator_for_metric(spec["metric"]),
            "entrypoint": spec["entrypoint"], "code_files": spec["code_files"],
            "data_files": {split: spec[split] for split in ("train", "dev", "test")},
            "repeat_unit": evaluator.repeat_unit(),
            "repeat_values": spec.get("seeds", []), "candidate_count": len(spec["candidates"]),
            "experiment_budget": spec["budget"], "input_hashes": hashes,
            "already_initialized": initialized, "approval_sha256": digest(request)}


def commit_project_preparation(project_path, protocol, files, approval_sha256):
    if not isinstance(approval_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", approval_sha256):
        raise ProtocolError("项目预注册审批摘要不合法")
    preview = preview_project_preparation(project_path, protocol, files)
    if preview["approval_sha256"] != approval_sha256:
        raise ProtocolError("项目协议或输入已变化，请重新预览")
    project, spec, files, sources = _prepare(project_path, protocol, files)
    if not project.exists():
        project.mkdir()
    for name, content in sources.items():
        target = _target(project, name)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    if not (project / "experiment.json").exists():
        write_json(project / "experiment.json", spec)
    if not preview["already_initialized"]:
        initialize(project)
    return {**preview, "already_initialized": preview["already_initialized"],
            "status": "registered"}

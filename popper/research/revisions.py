"""不可变 CodeRevision 存储：模型提案与实际执行代码之间的身份边界。"""
from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..core import (Experiment, ProtocolError, digest, file_hash, portable_path,
                    read_json, write_json)
from .execution import changed_statement_lines


@dataclass(frozen=True)
class CodeEdit:
    path: str
    replacement: str
    original_sha256: str | None = None

    def __post_init__(self):
        normalized = portable_path(self.path)
        if normalized is None or PurePosixPath(normalized).suffix.lower() != ".py":
            raise ProtocolError("CodeEdit path 必须是 revision 内的相对 Python 路径")
        # 存归一后的形式：两侧对同一份提案必须算出同一个 revision_id，且后续
        # `code_dir / edit.path` 拼的也必须是已经判定过的那个形式。
        object.__setattr__(self, "path", normalized)
        if not isinstance(self.replacement, str) or not self.replacement.strip():
            raise ProtocolError("CodeEdit replacement 不能为空")
        if len(self.replacement.encode("utf-8")) > 300_000:
            raise ProtocolError("单个 CodeEdit 不能超过 300KB")
        try:
            compile(self.replacement, self.path, "exec")
        except SyntaxError as error:
            raise ProtocolError(f"CodeEdit Python 语法错误: {self.path}:{error.lineno}") from None


class RevisionStore:
    """将父代码完整复制为内容寻址快照；已有 revision 永不原地修改。"""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def create(self, project, hypothesis_id, design_id, edits, actor,
               parent_revision_id=None):
        project = Path(project).resolve()
        if not hypothesis_id or not design_id or not actor:
            raise ProtocolError("revision 必须绑定 hypothesis/design/actor")
        edits = tuple(edits)
        if not edits:
            raise ProtocolError("revision 至少包含一个实际代码修改")
        if len({edit.path for edit in edits}) != len(edits):
            raise ProtocolError("revision 不能重复修改同一路径")
        exp = Experiment(project)
        try:
            state = exp.verify_inputs()
            if state["phase"] != "searching":
                raise ProtocolError("只能从 searching 实验创建 CodeRevision")
            registered = {Path(name).as_posix() for name in state["spec"]["code_files"]}
            source_files = {}
            if parent_revision_id:
                parent = self.verify(parent_revision_id)
                if (parent["identity"]["hypothesis_id"] != hypothesis_id
                        or parent["identity"]["design_id"] != design_id):
                    raise ProtocolError("父 revision 必须属于同一 hypothesis/design")
                parent_code = self.root / parent_revision_id / "code"
                for relative in parent["files"]:
                    source = parent_code / relative
                    source_files[relative] = {"source": str(source),
                                              "sha256": file_hash(source),
                                              "content": source.read_text(encoding="utf-8")}
            else:
                for relative in registered:
                    source = (project / relative).resolve()
                    source_files[relative] = {"source": str(source),
                                              "sha256": file_hash(source),
                                              "content": source.read_text(encoding="utf-8")}
            additions = set()
            for edit in edits:
                if edit.path in source_files:
                    if edit.original_sha256 != source_files[edit.path]["sha256"]:
                        raise ProtocolError("CodeEdit 原文件 SHA-256 不匹配")
                elif edit.original_sha256 is not None:
                    raise ProtocolError("新增文件的 original_sha256 必须为 null")
                else:
                    additions.add(edit.path)
            edit_payload = [{"path": edit.path,
                             "original_sha256": edit.original_sha256,
                             "replacement_sha256": digest(edit.replacement)} for edit in edits]
            # Compare the full repaired revision to the frozen project, not merely
            # its failed parent: removing an injected fault is not a new algorithm.
            resulting = {name: item["content"] for name, item in source_files.items()}
            resulting.update({edit.path: edit.replacement for edit in edits})
            execution_changes = {
                name: changed_statement_lines(
                    (project / name).read_text(encoding="utf-8") if name in registered else "", content)
                for name, content in resulting.items()}
            identity = {"hypothesis_id": hypothesis_id, "design_id": design_id,
                        "parent_revision_id": parent_revision_id,
                        "source_input_hashes": state["input_hashes"],
                        "source_revision_sha256": (file_hash(
                            self.root / parent_revision_id / "revision.json")
                            if parent_revision_id else None),
                        "edits": edit_payload, "actor": actor,
                        "execution_changes": execution_changes}
            revision_id = "REV-" + digest(identity)[:24]
            target = (self.root / revision_id).resolve()
            if not target.is_relative_to(self.root):
                raise ProtocolError("revision 目标越界")
            if target.exists():
                manifest = self.verify(revision_id)
                if manifest["identity"] != identity:
                    raise ProtocolError("revision_id 内容冲突")
                return manifest
            staging = (self.root / (revision_id + ".staging")).resolve()
            if staging.exists():
                raise ProtocolError("发现未完成 revision staging；需先审计后处理")
            try:
                code_dir = staging / "code"
                for relative, item in source_files.items():
                    destination = code_dir / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(item["content"], encoding="utf-8")
                for edit in edits:
                    destination = code_dir / edit.path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(edit.replacement, encoding="utf-8")
                files = {str(path.relative_to(code_dir)).replace("\\", "/"): file_hash(path)
                         for path in sorted(code_dir.rglob("*.py"))}
                manifest = {"schema_version": "1.0", "revision_id": revision_id,
                            "identity": identity, "entrypoint": state["spec"]["entrypoint"],
                            "registered_code_files": sorted(registered),
                            "added_code_files": sorted(additions), "files": files,
                            "status": "immutable"}
                staging.mkdir(parents=True, exist_ok=True)
                write_json(staging / "revision.json", manifest)
                staging.rename(target)
                return manifest
            except Exception:
                if staging.exists() and staging.is_relative_to(self.root):
                    shutil.rmtree(staging)
                raise
        finally:
            exp.close()

    def verify(self, revision_id):
        target = (self.root / revision_id).resolve()
        if not target.is_relative_to(self.root):
            raise ProtocolError("revision_id 越界")
        manifest_path = target / "revision.json"
        if not manifest_path.is_file():
            raise ProtocolError(f"CodeRevision 不存在: {revision_id}")
        manifest = read_json(manifest_path)
        if manifest.get("revision_id") != revision_id or manifest.get("status") != "immutable":
            raise ProtocolError("CodeRevision manifest 身份错误")
        if "REV-" + digest(manifest.get("identity"))[:24] != revision_id:
            raise ProtocolError("CodeRevision identity 摘要已变化")
        code_dir = target / "code"
        actual = {str(path.relative_to(code_dir)).replace("\\", "/"): file_hash(path)
                  for path in sorted(code_dir.rglob("*.py"))}
        if actual != manifest.get("files"):
            raise ProtocolError("CodeRevision 文件集合或 SHA-256 已变化")
        return manifest

    def path(self, revision_id):
        self.verify(revision_id)
        return self.root / revision_id

    def materialize(self, revision_id, manifest, code_bytes):
        """把远程上传的不可变 revision 落盘（幂等、原子），供分离式 worker 执行。

        `manifest` 为 revision.json 完整对象，`code_bytes` 为 {relative_path: bytes}；
        二者均按内容寻址校验后才写入，避免与本地 create() 的信任边界不一致。
        """
        if manifest.get("revision_id") != revision_id or manifest.get("status") != "immutable":
            raise ProtocolError("materialize revision manifest 身份错误")
        if "REV-" + digest(manifest.get("identity"))[:24] != revision_id:
            raise ProtocolError("materialize revision identity 摘要已变化")
        target = (self.root / revision_id).resolve()
        if not target.is_relative_to(self.root):
            raise ProtocolError("revision_id 越界")
        if target.exists():
            existing = self.verify(revision_id)
            if existing != manifest:
                raise ProtocolError("revision_id 内容冲突")
            return existing
        expected_files = manifest.get("files") or {}
        if set(code_bytes) != set(expected_files):
            raise ProtocolError("materialize 代码文件集合与 manifest 不一致")
        for name, content in code_bytes.items():
            # manifest 里的 key 是 create() 已经归一过的形式，这里不再接受第
            # 二次归一（否则同一文件在两侧会被拼成不同形状）。
            if portable_path(name) != name or PurePosixPath(name).suffix.lower() != ".py":
                raise ProtocolError("materialize 代码路径不合法")
            if hashlib.sha256(content).hexdigest() != expected_files[name]:
                raise ProtocolError("materialize 代码文件 SHA-256 不匹配")
        staging = (self.root / (revision_id + ".staging")).resolve()
        if staging.exists():
            raise ProtocolError("发现未完成 revision staging；需先审计后处理")
        try:
            code_dir = staging / "code"
            for name, content in code_bytes.items():
                destination = code_dir / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            staging.mkdir(parents=True, exist_ok=True)
            write_json(staging / "revision.json", manifest)
            staging.rename(target)
            return manifest
        except Exception:
            if staging.exists() and staging.is_relative_to(self.root):
                shutil.rmtree(staging)
            raise

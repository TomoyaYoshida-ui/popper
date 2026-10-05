"""不可变 CodeRevision 存储：模型提案与实际执行代码之间的身份边界。"""
from __future__ import annotations

import hashlib
import difflib
import shutil
import subprocess
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..core import (Experiment, ProtocolError, digest, file_hash, portable_path,
                    read_json, write_json)
from .execution import changed_statement_lines


def source_control_provenance(project, source_files):
    """Return a portable Git anchor for the exact revision source set.

    The immutable file hashes remain authoritative.  Git adds human-reviewable
    ancestry without making Git a runtime dependency: projects outside a
    repository are explicitly represented as unversioned.
    """
    project = Path(project).resolve()
    files = sorted(portable_path(name) for name in source_files)

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(project), *args], capture_output=True,
            check=True, timeout=5).stdout.decode("utf-8", errors="strict").strip()

    try:
        root = Path(git("rev-parse", "--show-toplevel")).resolve()
        if not project.is_relative_to(root):
            return {"kind": "unversioned"}
        commit = git("rev-parse", "--verify", "HEAD")
        if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
            return {"kind": "unversioned"}
        status = git("-c", "core.quotepath=false", "status", "--porcelain=v1",
                     "--untracked-files=all", "--", *files)
        project_path = project.relative_to(root).as_posix() or "."
        return {"kind": "git", "head_commit": commit,
                "project_path": project_path,
                "source_files_clean": not bool(status),
                "source_status_sha256": hashlib.sha256(
                    status.encode("utf-8")).hexdigest()}
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return {"kind": "unversioned"}


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
               parent_revision_id=None, expected_source_control=None):
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
            # 走共享判据而不是 `Path(name).as_posix()`：后者按宿主折叠分隔符，同一份 spec 在
            # Windows 与 Linux 上会算出不同的 registered 集合（进而不同的 revision_id）。
            # 归一后的值也正是 `project / relative` 能读到的那个形式。
            registered = {portable_path(name) for name in state["spec"]["code_files"]}
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
            edit_by_path = {edit.path: edit for edit in edits}
            diff_parts = []
            for name in sorted(edit_by_path):
                before = source_files.get(name, {}).get("content", "")
                after = edit_by_path[name].replacement
                diff_parts.extend(difflib.unified_diff(
                    before.splitlines(keepends=True), after.splitlines(keepends=True),
                    fromfile=f"a/{name}", tofile=f"b/{name}"))
            frozen_diff = "".join(diff_parts)
            # Compare the full repaired revision to the frozen project, not merely
            # its failed parent: removing an injected fault is not a new algorithm.
            resulting = {name: item["content"] for name, item in source_files.items()}
            resulting.update({edit.path: edit.replacement for edit in edits})
            execution_changes = {
                name: changed_statement_lines(
                    (project / name).read_text(encoding="utf-8") if name in registered else "", content)
                for name, content in resulting.items()}
            source_control = (parent["identity"].get(
                "source_control", {"kind": "unversioned"})
                if parent_revision_id else source_control_provenance(project, registered))
            if (expected_source_control is not None
                    and source_control != expected_source_control):
                raise ProtocolError("源码 Git 锚点已变化，请重新预览修订提案")
            identity = {"hypothesis_id": hypothesis_id, "design_id": design_id,
                        "parent_revision_id": parent_revision_id,
                        "source_input_hashes": state["input_hashes"],
                        "source_control": source_control,
                        "diff_sha256": digest(frozen_diff),
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
                            "diff": frozen_diff,
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
        identity = manifest.get("identity") or {}
        if "diff_sha256" in identity:
            if (not isinstance(manifest.get("diff"), str)
                    or digest(manifest["diff"]) != identity["diff_sha256"]):
                raise ProtocolError("CodeRevision diff 摘要已变化")
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


class RevisionDiffArchive:
    """Auditable sidecars for legacy revisions whose identity predates frozen diffs.

    A sidecar never edits ``revision.json`` and therefore never pretends the
    reconstructed diff was part of the original content identity.  Reconstruction
    is allowed only from an immutable parent snapshot, not from the mutable project.
    """

    def __init__(self, revisions):
        self.revisions = revisions if isinstance(revisions, RevisionStore) else RevisionStore(revisions)
        self.root = self.revisions.root / ".diff-attestations"

    def _path(self, revision_id):
        path = (self.root / f"{revision_id}.json").resolve()
        if path.parent != self.root:
            raise ProtocolError("Diff attestation 路径越界")
        return path

    def _reconstruct(self, revision_id):
        current = self.revisions.verify(revision_id)
        parent_id = (current.get("identity") or {}).get("parent_revision_id")
        if not parent_id:
            raise ProtocolError(
                "旧 root revision 缺少不可变父快照，不能从当前工作目录推测 Diff")
        parent = self.revisions.verify(parent_id)
        missing = sorted(set(parent["files"]) - set(current["files"]))
        if missing:
            raise ProtocolError("旧 revision 删除了父版本文件，无法按现行 Diff 契约迁移")
        parent_code = self.revisions.path(parent_id) / "code"
        current_code = self.revisions.path(revision_id) / "code"
        parts = []
        for name in sorted(current["files"]):
            before = ((parent_code / name).read_text(encoding="utf-8")
                      if name in parent["files"] else "")
            after = (current_code / name).read_text(encoding="utf-8")
            if before == after:
                continue
            parts.extend(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{name}", tofile=f"b/{name}"))
        diff_text = "".join(parts)
        if not diff_text:
            raise ProtocolError("旧 child revision 相对父快照没有可证明的差异")
        return current, parent, diff_text

    def migrate(self, revision_id):
        current = self.revisions.verify(revision_id)
        identity = current.get("identity") or {}
        if "diff_sha256" in identity:
            return {"revision_id": revision_id, "source": "frozen_identity",
                    "diff": current["diff"], "diff_sha256": identity["diff_sha256"],
                    "attestation_id": None}
        path = self._path(revision_id)
        if path.is_file():
            return self.verify(revision_id)
        current, parent, diff_text = self._reconstruct(revision_id)
        parent_id = current["identity"]["parent_revision_id"]
        body = {
            "schema_version": "1.0",
            "kind": "legacy_revision_diff_attestation",
            "revision_id": revision_id,
            "revision_manifest_sha256": file_hash(
                self.revisions.path(revision_id) / "revision.json"),
            "parent_revision_id": parent_id,
            "parent_manifest_sha256": file_hash(
                self.revisions.path(parent_id) / "revision.json"),
            "diff": diff_text,
            "diff_sha256": digest(diff_text),
            "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "method": "immutable_parent_snapshot_v1",
        }
        record = {**body, "attestation_id": "DFA-" + digest(body)[:24]}
        self.root.mkdir(parents=True, exist_ok=True)
        write_json(path, record)
        return self.verify(revision_id)

    def verify(self, revision_id):
        path = self._path(revision_id)
        if not path.is_file():
            raise ProtocolError("Diff attestation 不存在")
        record = read_json(path)
        attestation_id = record.get("attestation_id")
        body = {key: value for key, value in record.items() if key != "attestation_id"}
        if attestation_id != "DFA-" + digest(body)[:24]:
            raise ProtocolError("Diff attestation 身份摘要已变化")
        current, parent, expected = self._reconstruct(revision_id)
        parent_id = current["identity"]["parent_revision_id"]
        if (record.get("revision_id") != revision_id
                or record.get("parent_revision_id") != parent_id
                or record.get("revision_manifest_sha256") != file_hash(
                    self.revisions.path(revision_id) / "revision.json")
                or record.get("parent_manifest_sha256") != file_hash(
                    self.revisions.path(parent_id) / "revision.json")
                or record.get("diff") != expected
                or record.get("diff_sha256") != digest(expected)):
            raise ProtocolError("Diff attestation 与不可变 revision 快照不一致")
        return {**record, "source": "migrated_attestation"}

    def resolve(self, revision_id):
        manifest = self.revisions.verify(revision_id)
        identity = manifest.get("identity") or {}
        if "diff_sha256" in identity:
            return {"source": "frozen_identity", "diff": manifest["diff"],
                    "diff_sha256": identity["diff_sha256"], "attestation_id": None}
        if self._path(revision_id).is_file():
            record = self.verify(revision_id)
            return {"source": record["source"], "diff": record["diff"],
                    "diff_sha256": record["diff_sha256"],
                    "attestation_id": record["attestation_id"]}
        return {"source": "unavailable", "diff": None, "diff_sha256": None,
                "attestation_id": None}

    def migrate_all(self):
        results = []
        for manifest_path in sorted(self.revisions.root.glob("REV-*/revision.json")):
            revision_id = manifest_path.parent.name
            try:
                migrated = self.migrate(revision_id)
                results.append({key: migrated.get(key) for key in (
                    "revision_id", "source", "diff_sha256", "attestation_id")})
            except ProtocolError as error:
                results.append({"revision_id": revision_id, "source": "unavailable",
                                "error": str(error)})
        return {"results": results,
                "migrated": sum(row["source"] == "migrated_attestation" for row in results),
                "native": sum(row["source"] == "frozen_identity" for row in results),
                "unavailable": sum(row["source"] == "unavailable" for row in results)}

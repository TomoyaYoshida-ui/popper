"""Bounded text-source editor for an explicitly registered trusted workspace.

This service never creates paths, follows symlinks, or edits frozen experiment
inputs. It is a local workstation capability, not a remote file API.
"""
from __future__ import annotations

import hashlib
import difflib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .core import Experiment, ProtocolError, file_hash, read_json, write_json


class ProjectFileConflict(ProtocolError):
    """The selected workspace or file digest changed before a save."""


class ProjectFileUncertain(ProtocolError):
    """The file may be written while its tool receipt is not finalized."""


class ProjectFiles:
    MAX_BYTES = 1_048_576
    MAX_FILES = 500
    MAX_SCANNED = 5000
    MAX_DEPTH = 8
    SUFFIXES = frozenset({
        ".py", ".pyi", ".md", ".tex", ".txt", ".json", ".toml",
        ".yaml", ".yml", ".js", ".jsx", ".ts", ".tsx", ".css",
        ".html", ".sql", ".sh", ".ps1", ".r", ".jl",
    })
    IGNORED_DIRS = frozenset({
        ".git", ".popper", ".venv", "venv", "node_modules", "__pycache__",
        "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    })

    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ProtocolError("项目文件工作区不存在")

    def _frozen_paths(self):
        if not (self.root / ".popper" / "state.db").is_file():
            return set()
        experiment = Experiment(self.root, readonly=True)
        try:
            return set(experiment.state().get("input_hashes", {}))
        finally:
            experiment.close()

    def _path(self, relative):
        if (not isinstance(relative, str) or not relative or len(relative) > 512
                or "\\" in relative or "\x00" in relative or relative.startswith("/")):
            raise ProtocolError("项目文件路径不合法")
        parts = relative.split("/")
        if any(not part or part in {".", ".."} or part.startswith(".")
               or ":" in part for part in parts):
            raise ProtocolError("项目文件路径不合法")
        if any(part in self.IGNORED_DIRS for part in parts[:-1]):
            raise ProtocolError("项目文件目录受保护")
        target = self.root
        for part in parts:
            target = target / part
            if target.is_symlink():
                raise ProtocolError("项目文件不允许符号链接")
        resolved = target.resolve()
        if not resolved.is_relative_to(self.root):
            raise ProtocolError("项目文件路径越界")
        if resolved.suffix.lower() not in self.SUFFIXES:
            raise ProtocolError("项目文件类型不允许编辑")
        if not resolved.is_file():
            raise ProtocolError("项目文件不存在；编辑器不会创建新文件")
        if resolved.stat().st_size > self.MAX_BYTES:
            raise ProtocolError("项目文件超过 1 MiB 上限")
        return resolved

    def list_files(self):
        frozen = self._frozen_paths()
        found = []
        scanned = 0
        stack = [(self.root, 0)]
        truncated = False
        while stack:
            directory, depth = stack.pop()
            try:
                entries = os.scandir(directory)
            except OSError:
                continue
            with entries:
                for entry in entries:
                    scanned += 1
                    if scanned > self.MAX_SCANNED or len(found) >= self.MAX_FILES:
                        truncated = True
                        break
                    if entry.name.startswith(".") or entry.is_symlink():
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if (depth < self.MAX_DEPTH
                                    and entry.name not in self.IGNORED_DIRS
                                    and Path(entry.path).resolve().is_relative_to(self.root)):
                                stack.append((Path(entry.path), depth + 1))
                        elif (entry.is_file(follow_symlinks=False)
                              and Path(entry.name).suffix.lower() in self.SUFFIXES
                              and entry.stat(follow_symlinks=False).st_size <= self.MAX_BYTES):
                            relative = Path(entry.path).relative_to(self.root).as_posix()
                            found.append({"path": relative,
                                          "bytes": entry.stat(follow_symlinks=False).st_size,
                                          "editable": relative not in frozen,
                                          "reason": ("已冻结的实验输入；请创建 Revision"
                                                     if relative in frozen else None)})
                    except OSError:
                        continue
            if truncated:
                break
        found.sort(key=lambda item: item["path"].casefold())
        return {"files": found, "truncated": truncated,
                "limits": {"max_bytes": self.MAX_BYTES, "max_files": self.MAX_FILES}}

    def read(self, relative):
        target = self._path(relative)
        content = target.read_bytes()
        if len(content) > self.MAX_BYTES:
            raise ProtocolError("项目文件超过 1 MiB 上限")
        try:
            text = content.decode("utf-8-sig")
        except UnicodeError as error:
            raise ProtocolError("项目文件不是 UTF-8 文本") from error
        return {"path": relative, "content": text,
                "sha256": hashlib.sha256(content).hexdigest(),
                "bytes": len(content),
                "editable": relative not in self._frozen_paths()}

    def write(self, relative, content, expected_sha256):
        target, original, encoded, original_mode = self._prepare_write(
            relative, content, expected_sha256)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=".popper-edit-", dir=target.parent,
                    delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, original_mode)
            if file_hash(target) != expected_sha256:
                raise ProjectFileConflict("项目文件已在别处修改，请重新载入后再保存")
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return {"path": relative, "sha256": hashlib.sha256(encoded).hexdigest(),
                "bytes": len(encoded)}

    def preview_write(self, relative, content, expected_sha256):
        _target, original, encoded, _mode = self._prepare_write(
            relative, content, expected_sha256)
        before = original.decode("utf-8-sig").splitlines(keepends=True)
        after = encoded.decode("utf-8-sig").splitlines(keepends=True)
        lines = difflib.unified_diff(
            before, after, fromfile=f"a/{relative}", tofile=f"b/{relative}",
            n=3)
        diff = "".join(
            line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
            for line in lines)
        if len(diff.encode("utf-8")) > 200_000:
            raise ProtocolError("差异预览超过 200 KiB；请缩小修改范围")
        return {"path": relative, "before_sha256": expected_sha256,
                "after_sha256": hashlib.sha256(encoded).hexdigest(),
                "before_bytes": len(original), "after_bytes": len(encoded),
                "diff": diff, "changed": encoded != original}

    def preview_replace(self, relative, old_text, new_text, expected_sha256):
        if (not isinstance(old_text, str) or not old_text
                or not isinstance(new_text, str)
                or len(old_text) > 16_384 or len(new_text) > 16_384):
            raise ProtocolError("代码补丁必须是有界的非空旧文本与新文本")
        current = self.read(relative)
        if current["sha256"] != expected_sha256:
            raise ProjectFileConflict("项目文件已在别处修改，请重新读取后再提议补丁")
        if current["content"].count(old_text) != 1:
            raise ProjectFileConflict("旧文本必须在当前文件中恰好出现一次")
        updated = current["content"].replace(old_text, new_text, 1)
        return self.preview_write(relative, updated, expected_sha256), updated

    def _receipt_path(self, call_id):
        if (not isinstance(call_id, str)
                or re.fullmatch(r"tcl_[A-Za-z0-9_-]{8,128}", call_id) is None):
            raise ProtocolError("代码编辑 tool call ID 不合法")
        return self.root / ".popper" / "file-edit-receipts" / (call_id + ".json")

    def edit_receipt(self, call_id, arguments):
        receipt_path = self._receipt_path(call_id)
        if not receipt_path.is_file():
            return None
        receipt = read_json(receipt_path)
        argument_hash = hashlib.sha256(json.dumps(
            arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        if receipt.get("arguments_sha256") != argument_hash:
            raise ProjectFileConflict("代码编辑回执与工具参数不匹配")
        result = receipt.get("result")
        if (receipt.get("status") == "committed" and isinstance(result, dict)):
            return result
        if receipt.get("status") != "prepared" or not isinstance(result, dict):
            raise ProjectFileUncertain("代码编辑回执状态不合法，需要人工核查")
        current_hash = file_hash(self._path(arguments["path"]))
        if current_hash == result.get("sha256"):
            write_json(receipt_path, {**receipt, "status": "committed"})
            return result
        if current_hash == arguments["expected_sha256"]:
            return None
        raise ProjectFileUncertain("代码编辑可能与外部修改交错，需要人工核查")

    def apply_replace(self, call_id, arguments):
        prior = self.edit_receipt(call_id, arguments)
        if prior is not None:
            return prior
        preview, updated = self.preview_replace(
            arguments["path"], arguments["old_text"],
            arguments["new_text"], arguments["expected_sha256"])
        if not preview["changed"]:
            raise ProtocolError("代码补丁没有产生修改")
        result = {"path": arguments["path"], "sha256": preview["after_sha256"],
                  "bytes": preview["after_bytes"],
                  "before_sha256": preview["before_sha256"],
                  "diff": preview["diff"]}
        receipt_path = self._receipt_path(call_id)
        argument_hash = hashlib.sha256(json.dumps(
            arguments, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        write_json(receipt_path, {"schema_version": "1.0", "status": "prepared",
                                  "arguments_sha256": argument_hash, "result": result})
        saved = self.write(arguments["path"], updated, arguments["expected_sha256"])
        if saved["sha256"] != result["sha256"]:
            raise ProjectFileUncertain("代码编辑结果摘要不一致，需要人工核查")
        try:
            write_json(receipt_path, {"schema_version": "1.0", "status": "committed",
                                      "arguments_sha256": argument_hash, "result": result})
        except Exception as error:
            raise ProjectFileUncertain("代码已写盘但回执未完成，需启动恢复") from error
        return result

    def _prepare_write(self, relative, content, expected_sha256):
        target = self._path(relative)
        if relative in self._frozen_paths():
            raise ProtocolError("已冻结的实验输入不能原地修改；请创建 Revision")
        if (not isinstance(expected_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
            raise ProtocolError("保存项目文件需要已读取文件的 SHA-256")
        if not isinstance(content, str):
            raise ProtocolError("项目文件内容必须是文本")
        try:
            encoded = content.encode("utf-8")
        except UnicodeError as error:
            raise ProtocolError("项目文件包含无法编码的字符") from error
        if len(encoded) > self.MAX_BYTES:
            raise ProtocolError("项目文件超过 1 MiB 上限")
        original = target.read_bytes()
        if hashlib.sha256(original).hexdigest() != expected_sha256:
            raise ProjectFileConflict("项目文件已在别处修改，请重新载入后再保存")
        original_mode = stat.S_IMODE(target.stat().st_mode)
        if not original_mode & stat.S_IWUSR:
            raise ProtocolError("只读项目文件不能写回")
        if original.startswith(b"\xef\xbb\xbf"):
            encoded = b"\xef\xbb\xbf" + encoded
        if len(encoded) > self.MAX_BYTES:
            raise ProtocolError("项目文件超过 1 MiB 上限")
        return target, original, encoded, original_mode

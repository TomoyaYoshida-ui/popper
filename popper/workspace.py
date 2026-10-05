"""工作区/Quest 模型。多文件夹 + 文件夹内 Quest 任务的增删改，持久化到 <project>/.popper/workspace.json。

同时登记稿件物化产物（deliverables）并提供越界防护的文件解析。遵循“无假数据”：首次访问仅用真实
项目名与真实 state.phase 播种单个文件夹与单个 Quest；其余内容均来自用户真实操作并原子持久化。
"""
from __future__ import annotations

import threading
import uuid
import re
import os
import tempfile
from datetime import datetime
from pathlib import Path

from .core import ProtocolError, digest, file_hash, read_json, write_json
from .reproduction import STATE_DIR

SCHEMA_VERSION = "1.0"
WORKSPACE_REL = ".popper/workspace.json"
REGISTRY_REL = ".popper/workspaces.json"
DELIVERABLES_REL = "deliverables"

PHASE_LABEL = {
    "searching": "推进实验搜索",
    "frozen": "完成候选冻结",
    "confirming": "消费最终测试",
    "completed": "汇总实验结论",
    "confirmation_failed": "处理最终确认失败",
}

VALID_TEMPLATES = {"md", "tex", "docx", "pdf"}
VALID_DISCLOSURES = {None, "nature", "acm", "ieee"}

# 模板→中文标签：后端单一定义，前端不再硬编码或回退。
TEMPLATE_LABELS = {
    "md": "Markdown",
    "tex": "LaTeX",
    "docx": "Word",
    "pdf": "PDF",
    "json": "JSON",
}

# AI 披露口径→中文标签：后端单一定义，前端不再硬编码或回退。
DISCLOSURE_LABELS = {
    "nature": "Nature 口径",
    "acm": "ACM 口径",
    "ieee": "IEEE 口径",
}

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")
_COMMAND_HISTORY_LIMIT = 256


class WorkspaceVersionConflict(ProtocolError):
    """A command was based on a stale workspace state version."""


class WorkspaceIdempotencyConflict(ProtocolError):
    """An idempotency key was reused for a different command."""


class Workspace:
    def __init__(self, project_dir, lock=None):
        self.root = Path(project_dir).resolve()
        self.lock = lock if lock is not None else threading.Lock()
        self.path = self.root / WORKSPACE_REL
        self.deliverables_dir = self.root / DELIVERABLES_REL

    # -- 基础 ---------------------------------------------------------------
    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat(timespec="seconds")

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex}"

    def read(self) -> dict | None:
        """读取 workspace；文件不存在返回 None；损坏/结构异常抛 ProtocolError。"""
        if not self.path.is_file():
            return None
        try:
            data = read_json(self.path)
        except Exception as error:
            raise ProtocolError(f"workspace.json 无法解析：{error}")
        if not isinstance(data, dict):
            raise ProtocolError("workspace.json 结构不合法")
        meta = data.setdefault("meta", {})
        meta.setdefault("state_version", 1)
        meta.setdefault("_command_results", {})
        return data

    def save(self, data: dict, expected_version: int | None = None) -> int:
        """Atomically persist a new workspace version.

        Legacy callers omit ``expected_version`` and retain their previous
        last-writer-wins behavior. API v1 commands always provide it and fail
        closed on stale state.
        """
        if not isinstance(data, dict) or not isinstance(data.get("meta"), dict):
            raise ProtocolError("workspace 数据缺少 meta")
        current = self.read()
        current_version = int(current["meta"]["state_version"]) if current else 0
        if expected_version is not None:
            if type(expected_version) is not int or expected_version < 1:
                raise ProtocolError("expected_version 必须是正整数")
            if expected_version != current_version:
                raise WorkspaceVersionConflict(
                    f"workspace 版本冲突：期望 {expected_version}，当前 {current_version}")
        next_version = current_version + 1
        data["meta"]["state_version"] = next_version
        write_json(self.path, data)
        return next_version

    # -- 真实项目信息（用于无假数据播种） --------------------------------------
    def project_spec(self) -> str | None:
        """取真实项目展示名：实验取 experiment.json[name]；复现回退'复现任务'。"""
        exp = self.root / "experiment.json"
        if exp.is_file():
            try:
                spec = read_json(exp)
                name = str(spec.get("name") or "").strip()
                return name or None
            except Exception:
                return None
        state = self.root / STATE_DIR / "state.json"
        if state.is_file():
            return "复现任务"
        return None

    def current_phase(self) -> str | None:
        """实验模式返回真实 state.phase；复现/异常返回 None。"""
        if (self.root / ".popper" / "state.db").is_file() and (self.root / "experiment.json").is_file():
            try:
                from .core import Experiment
                exp = Experiment(self.root)
                try:
                    return exp.state().get("phase")
                finally:
                    exp.close()
            except Exception:
                return None
        return None

    def is_empty(self) -> bool:
        return not self.path.is_file()

    def seed_if_empty(self, spec_name: str | None, phase: str | None) -> bool:
        """仅当文件不存在时用真实项目信息播种一个文件夹 + 一个 Quest。返回是否播了种子。
        调用方应持有 ws_lock。"""
        if self.path.is_file():
            return False
        now = self._now()
        folder_name = spec_name or "研究任务"
        quest_title = PHASE_LABEL.get(phase or "") or "研究任务"
        data = {
            "schema_version": SCHEMA_VERSION,
            "meta": {
                "next_quest_counter": 1,
                "state_version": 1,
                "_command_results": {},
                "seed": {"folder_name": folder_name, "phase": phase, "seeded_at": now},
            },
            "folders": [{
                "fid": self._new_id("f"),
                "name": folder_name,
                "created": now,
                "tasks": [{
                    "quid": self._new_id("q"),
                    "title": quest_title,
                    "created": now,
                    "done": False,
                }],
            }],
            "deliverables": [],
        }
        write_json(self.path, data)
        return True

    def apply_operation(self, data: dict, payload: dict) -> dict:
        """Apply one validated workspace mutation without persisting it."""
        if not isinstance(payload, dict) or not isinstance(payload.get("op"), str):
            raise ProtocolError("请求必须含 op 字符串")
        op = payload["op"]
        handlers = {
            "folder_create": (["name"], lambda: self.folder_create(data, payload["name"])),
            "folder_rename": (["fid", "name"], lambda: self.folder_rename(
                data, payload["fid"], payload["name"])),
            "folder_remove": (["fid"], lambda: self.folder_remove(data, payload["fid"])),
            "task_create": (["fid"], lambda: self.task_create(
                data, payload["fid"], payload.get("title"))),
            "task_rename": (["fid", "quid", "title"], lambda: self.task_rename(
                data, payload["fid"], payload["quid"], payload["title"])),
            "task_toggle": (["fid", "quid"], lambda: self.task_toggle(
                data, payload["fid"], payload["quid"], bool(payload.get("done", False)))),
            "task_remove": (["fid", "quid"], lambda: self.task_remove(
                data, payload["fid"], payload["quid"])),
            "task_move": (["from_fid", "quid", "to_fid"], lambda: self.task_move(
                data, payload["from_fid"], payload["quid"], payload["to_fid"])),
        }
        handler = handlers.get(op)
        if handler is None:
            raise ProtocolError("未知工作区操作")
        needed, apply = handler
        for key in needed:
            if key not in payload or payload[key] is None:
                raise ProtocolError(f"缺少 {key}")
        apply()
        return data

    def apply_command(self, payload: dict, *, idempotency_key: str,
                      expected_version: int) -> dict:
        """Apply an API v1 command exactly once within workspace.json.

        Command receipt and mutated state are written in the same atomic file
        replacement, so a crash cannot persist one without the other.
        """
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
                idempotency_key):
            raise ProtocolError("idempotency_key 格式不合法")
        data = self.read()
        if data is None:
            raise ProtocolError("workspace 尚未初始化")
        command_hash = digest(payload)
        receipts = data["meta"].setdefault("_command_results", {})
        previous = receipts.get(idempotency_key)
        if previous is not None:
            if previous.get("command_sha256") != command_hash:
                raise WorkspaceIdempotencyConflict("相同 idempotency_key 对应不同命令")
            return dict(previous["result"])
        current_version = data["meta"]["state_version"]
        if expected_version != current_version:
            raise WorkspaceVersionConflict(
                f"workspace 版本冲突：期望 {expected_version}，当前 {current_version}")
        self.apply_operation(data, payload)
        result = {"operation": payload["op"], "state_version": current_version + 1}
        receipts[idempotency_key] = {
            "command_sha256": command_hash,
            "result": result,
            "applied_at": self._now(),
        }
        while len(receipts) > _COMMAND_HISTORY_LIMIT:
            receipts.pop(next(iter(receipts)))
        self.save(data, expected_version=expected_version)
        return dict(result)

    def command_receipt(self, payload: dict, *, idempotency_key: str) -> dict | None:
        """Read an exact committed command receipt without replaying its effect."""
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
                idempotency_key):
            raise ProtocolError("idempotency_key 格式不合法")
        data = self.read()
        if data is None:
            return None
        receipt = data["meta"].get("_command_results", {}).get(idempotency_key)
        if receipt is None:
            return None
        if (not isinstance(receipt, dict)
                or receipt.get("command_sha256") != digest(payload)
                or not isinstance(receipt.get("result"), dict)
                or receipt["result"].get("operation") != payload.get("op")
                or type(receipt["result"].get("state_version")) is not int
                or receipt["result"]["state_version"] < 1
                or receipt["result"]["state_version"] > data["meta"]["state_version"]):
            raise WorkspaceIdempotencyConflict("工具命令收据与原请求不一致")
        return dict(receipt["result"])

    # -- 定位辅助 ------------------------------------------------------------
    @staticmethod
    def _folder(data: dict, fid: str) -> dict:
        for folder in data["folders"]:
            if folder["fid"] == fid:
                return folder
        raise ProtocolError("文件夹不存在")

    @staticmethod
    def _task(folder: dict, quid: str) -> dict:
        for task in folder["tasks"]:
            if task["quid"] == quid:
                return task
        raise ProtocolError("Quest 不存在")

    # -- 文件夹 --------------------------------------------------------------
    def folder_create(self, data: dict, name: str) -> dict:
        folder = {"fid": self._new_id("f"), "name": str(name).strip()[:80],
                  "created": self._now(), "tasks": []}
        data["folders"].append(folder)
        return data

    def folder_rename(self, data: dict, fid: str, name: str) -> dict:
        self._folder(data, fid)["name"] = str(name).strip()[:80]
        return data

    def folder_remove(self, data: dict, fid: str) -> dict:
        data["folders"] = [f for f in data["folders"] if f["fid"] != fid]
        return data

    # -- Quest 任务 ----------------------------------------------------------
    def task_create(self, data: dict, fid: str, title: str | None = None) -> dict:
        folder = self._folder(data, fid)
        if title is None or not str(title).strip():
            title = f"新 Quest {data['meta']['next_quest_counter']}"
        else:
            title = str(title).strip()[:80]
        data["meta"]["next_quest_counter"] += 1
        task = {"quid": self._new_id("q"), "title": title,
                "created": self._now(), "done": False}
        folder["tasks"].append(task)
        return data

    def task_rename(self, data: dict, fid: str, quid: str, title: str) -> dict:
        self._task(self._folder(data, fid), quid)["title"] = str(title).strip()[:80]
        return data

    def task_toggle(self, data: dict, fid: str, quid: str, done: bool) -> dict:
        self._task(self._folder(data, fid), quid)["done"] = bool(done)
        return data

    def task_remove(self, data: dict, fid: str, quid: str) -> dict:
        folder = self._folder(data, fid)
        folder["tasks"] = [t for t in folder["tasks"] if t["quid"] != quid]
        return data

    def task_move(self, data: dict, from_fid: str, quid: str, to_fid: str) -> dict:
        src = self._folder(data, from_fid)
        task = self._task(src, quid)
        src["tasks"] = [t for t in src["tasks"] if t["quid"] != quid]
        self._folder(data, to_fid)["tasks"].append(task)
        return data

    # -- 物化产物 ------------------------------------------------------------
    def ensure_deliverables_dir(self) -> Path:
        self.deliverables_dir.mkdir(parents=True, exist_ok=True)
        return self.deliverables_dir

    def register_deliverable(self, data: dict, filename: str, template: str,
                             disclosure: str | None, bytes_: int) -> dict:
        item = {"id": self._new_id("d"), "filename": str(filename),
                "template": str(template), "disclosure": disclosure,
                "created": self._now(), "bytes": int(bytes_)}
        data.setdefault("deliverables", []).append(item)
        return item

    def list_deliverables(self, data: dict) -> list[dict]:
        items = list(data.get("deliverables", []))
        items.sort(key=lambda it: it.get("created", ""), reverse=True)
        return items

    def deliverable_path(self, item_id: str) -> Path:
        """按登记 id 或文件名解析合法产物路径；未登记/越界/非文件均抛 ProtocolError。"""
        data = self.read() or {}
        item = next((it for it in data.get("deliverables", [])
                     if it.get("id") == item_id or it.get("filename") == item_id), None)
        if item is None:
            raise ProtocolError("产物不存在")
        target = (self.deliverables_dir / item["filename"]).resolve()
        base = self.deliverables_dir.resolve()
        if not target.is_relative_to(base):
            raise ProtocolError("产物路径越界")
        if not target.is_file():
            raise ProtocolError("产物文件缺失")
        return target

    WRITEABLE_SUFFIXES = {".md", ".tex", ".json"}

    def write_deliverable(self, item_id: str, content: str, *, expected_sha256: str) -> Path:
        """把编辑后的文本写回已登记的物化文件（id 或文件名定位）。

        仅允许覆写 deliverables 目录内已存在的 md/tex/json 真实文件；扩展名受限、
        路径在目录白名单内解析，防穿越与新建。
        """
        data = self.read() or {}
        item = next((it for it in data.get("deliverables", [])
                     if it.get("id") == item_id or it.get("filename") == item_id), None)
        if item is None:
            raise ProtocolError("产物不存在")
        filename = item["filename"]
        suffix = Path(filename).suffix.lower()
        if suffix not in self.WRITEABLE_SUFFIXES:
            raise ProtocolError(f"仅支持写回文本稿件 .md/.tex/.json，收到 {suffix}")
        target = (self.deliverables_dir / filename).resolve()
        base = self.deliverables_dir.resolve()
        if not target.is_relative_to(base):
            raise ProtocolError("产物路径越界")
        if not target.is_file():
            raise ProtocolError("仅允许覆写已生成的物化文件")
        if (not isinstance(expected_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
            raise ProtocolError("保存稿件需要已读取文件的 SHA-256")
        if file_hash(target) != expected_sha256:
            raise WorkspaceVersionConflict("稿件已在别处修改，请重新加载后再保存")
        try:
            encoded = content.encode("utf-8")
        except UnicodeError as error:
            raise ProtocolError("稿件包含无法编码的字符") from error
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=".popper-edit-", dir=target.parent,
                    delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return target


class WorkspaceHub:
    """多工作区根注册表：主根 + 显式登记的额外根 + 当前根指针。

    主根是 serve 的项目目录，永远存在且不可移除；额外根只能经 CLI
    --workspace-root 显式登记（不做磁盘扫描，与 --research-run 同一模式），
    登记持久化到 <project>/.popper/workspaces.json，重启不丢；当前根指针同样
    持久化。每个根的 Quest 与产物状态仍只存在于该根自己的 workspace.json /
    deliverables/ 里——注册表只保存「有哪些根、当前在哪个根」，不复制业务状态，
    根间数据天然隔离。所有方法都要求调用方持有 ws_lock（与 Workspace 共用一把锁）。
    """

    def __init__(self, project_dir, lock=None):
        self.primary = Workspace(project_dir, lock)
        self.lock = self.primary.lock
        self.registry_path = self.primary.root / REGISTRY_REL
        self._cache: dict[str, Workspace] = {str(self.primary.root): self.primary}
        self._ensure_registry()

    # -- 注册表持久化 ----------------------------------------------------------
    @staticmethod
    def _derive_name(root: Path) -> str:
        """根显示名：优先真实项目名（experiment.json / 复现任务），否则目录名。"""
        return Workspace(root).project_spec() or root.name

    def _load(self) -> dict:
        if not self.registry_path.is_file():
            return {"schema_version": SCHEMA_VERSION, "current": None, "roots": []}
        try:
            data = read_json(self.registry_path)
        except Exception as error:
            raise ProtocolError(f"workspaces.json 无法解析：{error}")
        if not isinstance(data, dict) or not isinstance(data.get("roots"), list):
            raise ProtocolError("workspaces.json 结构不合法")
        data.setdefault("current", None)
        return data

    def _save(self, reg: dict) -> None:
        write_json(self.registry_path, reg)

    def _ensure_registry(self) -> None:
        """保证注册表存在且自洽：主根在列、当前指针有效；仅自修复时写文件。"""
        reg = self._load()
        changed = False
        if not any(entry.get("primary") for entry in reg["roots"]):
            reg["roots"].insert(0, {
                "wid": "primary", "root": str(self.primary.root),
                "name": self._derive_name(self.primary.root),
                "primary": True, "registered": Workspace._now(),
            })
            changed = True
        if reg["current"] is None or not any(entry["wid"] == reg["current"]
                                             for entry in reg["roots"]):
            reg["current"] = reg["roots"][0]["wid"]
            changed = True
        if changed:
            self._save(reg)

    # -- 登记 ------------------------------------------------------------------
    def register_cli_roots(self, paths) -> None:
        """CLI 显式登记额外工作区根：不存在/非目录拒绝；重复与主根幂等跳过。"""
        reg = self._load()
        known = {Path(entry["root"]).resolve() for entry in reg["roots"]}
        changed = False
        for raw in paths or ():
            root = Path(raw).resolve()
            if not root.is_dir():
                raise ProtocolError(f"工作区根不存在或不是目录：{root}")
            if root in known:
                continue
            reg["roots"].append({
                "wid": f"w_{uuid.uuid4().hex[:12]}", "root": str(root),
                "name": self._derive_name(root), "primary": False,
                "registered": Workspace._now(),
            })
            known.add(root)
            changed = True
        if changed:
            self._save(reg)

    # -- 查询 / 切换 / 移除 ------------------------------------------------------
    def current_wid(self) -> str:
        return self._load()["current"]

    def current(self) -> Workspace:
        reg = self._load()
        for entry in reg["roots"]:
            if entry["wid"] == reg["current"]:
                return self._workspace_for(entry["root"])
        return self.primary

    def roots_payload(self) -> list[dict]:
        reg = self._load()
        items = []
        for entry in reg["roots"]:
            item = dict(entry)
            if entry.get("primary"):
                # 主根显示名跟随真实项目名（experiment.json 改名即生效）。
                item["name"] = self._derive_name(self.primary.root)
            item["current"] = entry["wid"] == reg["current"]
            items.append(item)
        return items

    def _workspace_for(self, root_str: str) -> Workspace:
        ws = self._cache.get(root_str)
        if ws is None:
            ws = Workspace(root_str, self.lock)
            self._cache[root_str] = ws
        return ws

    def switch(self, wid: str) -> None:
        reg = self._load()
        if not any(entry["wid"] == wid for entry in reg["roots"]):
            raise ProtocolError("工作区不存在")
        reg["current"] = wid
        self._save(reg)

    def remove(self, wid: str) -> None:
        """从注册表摘除非主根（磁盘数据保留）；摘除当前根则回退主根。"""
        reg = self._load()
        entry = next((e for e in reg["roots"] if e["wid"] == wid), None)
        if entry is None:
            raise ProtocolError("工作区不存在")
        if entry.get("primary"):
            raise ProtocolError("主工作区不可移除")
        reg["roots"] = [e for e in reg["roots"] if e["wid"] != wid]
        if reg["current"] == wid:
            reg["current"] = reg["roots"][0]["wid"]
        self._save(reg)

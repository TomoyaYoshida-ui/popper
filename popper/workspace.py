"""工作区/Quest 模型。多文件夹 + 文件夹内 Quest 任务的增删改，持久化到 <project>/.popper/workspace.json。

同时登记稿件物化产物（deliverables）并提供越界防护的文件解析。遵循“无假数据”：首次访问仅用真实
项目名与真实 state.phase 播种单个文件夹与单个 Quest；其余内容均来自用户真实操作并原子持久化。
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime
from pathlib import Path

from .core import ProtocolError, read_json, write_json
from .reproduction import STATE_DIR

SCHEMA_VERSION = "1.0"
WORKSPACE_REL = ".popper/workspace.json"
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
        return data

    def save(self, data: dict) -> None:
        write_json(self.path, data)

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

    def write_deliverable(self, item_id: str, content: str) -> Path:
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
        target.write_text(str(content), encoding="utf-8")
        return target
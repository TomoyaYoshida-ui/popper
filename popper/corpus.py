"""领域语料库 + 社区共享协议（opt-in）。

四类记录：gap / contradiction / negative_result / foresight。
每条锚定文献 id + 描述 + 类型 + 时间戳；本地增量积累，可选贡献到社区共享池。
本地池与社区池物理隔离；社区语料二次验证后才允许进入 claim 链路。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .core import ProtocolError, digest, read_json, write_json

VALID_TYPES = ("gap", "contradiction", "negative_result", "foresight")
_VALID_NOVELTY = ("major", "incremental", "trivial")
_VALID_SUPPORT = ("full", "partial", "none", "unavailable")
# 可进入 claim 链路的来源：本地自然记录 或 经二次验证的社区记录。
CLAIMABLE_VERIFICATION = ("local", "verified")
_LITERATURE_ID_PATTERN = re.compile(r"^(arXiv:\S+|\d+\.\d+|\S+/\S+|doi:\S+)$", re.IGNORECASE)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _validate_literature_id(literature_id):
    if not isinstance(literature_id, str) or not _LITERATURE_ID_PATTERN.match(literature_id.strip()):
        raise ProtocolError("literature_id 无法解析：需要 arXiv 号 / DOI / 可解析标识")


class Corpus:
    """本地领域语料库。

    directory/
      records.json        # 本地记录（权威本体）
      community/          # 社区共享池（只读，进入前需二次验证）
        *.json            # 每个来源一条
      outbox/             # 待贡献到社区（share 写入）
        {record_id}.json
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.records_path = self.directory / "records.json"
        self.community_dir = self.directory / "community"
        self.outbox_dir = self.directory / "outbox"

    def _require_initialized(self):
        if not self.records_path.is_file():
            raise ProtocolError("语料库未初始化；先运行 corpus init")

    def _load(self):
        self._require_initialized()
        return read_json(self.records_path)

    def initialize(self):
        if self.records_path.is_file():
            return {"status": "already_initialized", "directory": str(self.directory)}
        write_json(self.records_path, {"schema_version": "1.0", "records": []})
        self.community_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        return {"status": "initialized", "directory": str(self.directory)}

    def add(self, type_, literature_id, description, novelty_tag=None, record_id=None,
            support_level=None):
        if type_ not in VALID_TYPES:
            raise ProtocolError(f"type 必须是 {VALID_TYPES}")
        if not isinstance(description, str) or not description.strip():
            raise ProtocolError("description 不能为空")
        if novelty_tag is not None and novelty_tag not in _VALID_NOVELTY:
            raise ProtocolError(f"novelty_tag 必须是 {_VALID_NOVELTY} 之一")
        if support_level is not None and support_level not in _VALID_SUPPORT:
            raise ProtocolError(f"support_level 必须是 {_VALID_SUPPORT} 之一")
        _validate_literature_id(literature_id)
        record_id = record_id or digest({"t": type_, "l": literature_id.strip(),
                                         "d": description.strip()})[:12]
        record = {
            "record_id": record_id,
            "type": type_,
            "literature_id": literature_id.strip(),
            "description": description.strip(),
            "novelty_tag": novelty_tag,
            "support_level": support_level,
            "timestamp": _now(),
            "source": "local",
            "verification": "local",
        }
        data = self._load()
        if any(r["record_id"] == record_id for r in data["records"]):
            return {"status": "already_exists", "record_id": record_id,
                    "library_size": len(data["records"])}
        data["records"].append(record)
        write_json(self.records_path, data)
        return {"status": "added", "record_id": record_id, "library_size": len(data["records"])}

    def stats(self):
        data = self._load()
        by_type = {}
        for r in data["records"]:
            by_type[r["type"]] = by_type.get(r["type"], 0) + 1
        newest = max((r["timestamp"] for r in data["records"]), default=None)
        return {
            "schema_version": data["schema_version"],
            "count": len(data["records"]),
            "by_type": by_type,
            "community_count": len(list(self.community_dir.glob("*.json"))),
            "latest_update": newest,
        }

    def share(self, record_id):
        """把本地记录贡献到社区共享池（opt-in）。

        提交前做 schema 合规 + 文献 id 可解析校验；不合格拒绝。
        提交后该条进入 outbox 待批次，并标记 review_pending。
        """
        data = self._load()
        record = next((r for r in data["records"] if r["record_id"] == record_id), None)
        if record is None:
            raise ProtocolError(f"本地库中不存在 record_id: {record_id}")
        if record.get("source") == "community":
            raise ProtocolError("社区来源记录不可再次共享")
        _validate_literature_id(record["literature_id"])  # 提交前复检
        record = dict(record, verification="review_pending")
        out = self.outbox_dir / f"{record_id}.json"
        write_json(out, record)
        return {"status": "queued", "record_id": record_id, "outbox": str(out)}

    def list_community(self):
        """读取社区共享池（只读；进入 claim 链路前需二次验证）。"""
        self._require_initialized()
        return [read_json(p) for p in sorted(self.community_dir.glob("*.json"))]

    def verify_community(self, source_path=None):
        """二次验证社区语料后导入本地。

        未经二次验证的社区记录不会被 verified_records() 返回，因此不能进入 claim 链路。
        社区池与本地位物理隔离；导入以 record_id 去重，不重复入库。
        """
        self._require_initialized()
        sources = [Path(source_path)] if source_path else sorted(self.community_dir.glob("*.json"))
        imported = []
        for path in sources:
            record = read_json(path)
            _validate_literature_id(record["literature_id"])
            if record["type"] not in VALID_TYPES:
                raise ProtocolError(f"社区记录 type 非法: {record['type']}")
            verified = dict(record, source="community", verification="verified")
            data = self._load()
            if any(r["record_id"] == verified["record_id"] for r in data["records"]):
                continue
            data["records"].append(verified)
            write_json(self.records_path, data)
            imported.append(verified["record_id"])
        return {"status": "imported", "count": len(imported), "record_ids": imported}

    def verified_records(self):
        """仅返回本地自然或经二次验证的记录，供 claim 链路使用。"""
        self._require_initialized()
        return [r for r in self._load()["records"]
                if r.get("verification") in CLAIMABLE_VERIFICATION]
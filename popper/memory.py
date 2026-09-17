"""上下文管理 · 三层记忆 + 指针外置 + 驱逐。

- 工作记忆（≤4K/步骤，步骤结束即清空）。
- 短期记忆（state.md + memory_summary，跨步骤持久化，会话级）。
- 长期记忆（domain_evidence + idea_cards + artifact 索引，项目级）。
- 指针外置：>4KB 内容落盘为 artifact，上下文仅 {artifact_id, sha256_prefix, summary}（
  16 位十六进制摘要前缀，不是完整 sha256，键名如实标注）。
- 驱逐策略：LRU + importance 加权；SLO-4 预算（≤64K/阶段，≤100K/会话）。
"""
from __future__ import annotations

from pathlib import Path

from .core import ProtocolError, canonical, digest, read_json, write_json

WORKING_LIMIT = 4 * 1024          # ≤4K/步骤
STAGE_LIMIT = 64 * 1024           # SLO-4 ≤64K/阶段
SESSION_LIMIT = 100 * 1024        # ≤100K/会话
ARTIFACT_PTR_THRESHOLD = 4 * 1024  # >4KB 指针外置

_ABBREV = 10  # 摘要保留 token（由侧门，不伪造语义内容）


class Memory:
    """三层记忆。directory/ 保存长期 artifact 与索引。

    directory/
      working.md          # 工作记忆（步骤级，运行完清空）
      memory_summary.json # 短期摘要
      artifacts/          # 指针外置的大块内容
        {artifact_id}.txt
      index.json          # artifact 索引（id, sha256_prefix, summary, size, importance, last_access）
    """

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.working_path = self.directory / "working.md"
        self.summary_path = self.directory / "memory_summary.json"
        self.artifacts_dir = self.directory / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.directory / "index.json"
        self.session_used = 0  # 会话累计 token（SLO-4）

    # ---- 工作记忆 ----
    def set_working(self, text):
        self.working_path.write_text(text or "", encoding="utf-8")

    def clear_working(self):
        """步骤结束即清空工作记忆。"""
        self.working_path.write_text("", encoding="utf-8")

    def working_size(self) -> int:
        return self.working_path.stat().st_size if self.working_path.exists() else 0

    # ---- 短期记忆 ----
    def set_summary(self, summary: dict):
        write_json(self.summary_path, summary)

    def summary(self):
        if not self.summary_path.is_file():
            return {}
        return read_json(self.summary_path)

    # ---- 长期记忆 + 指针外置 ----
    def _index(self):
        if not self.index_path.is_file():
            return {"schema_version": "1.0", "artifacts": {}}
        return read_json(self.index_path)

    def _save_index(self, data):
        write_json(self.index_path, data)

    def externalize(self, content, importance=1.0):
        """大块内容指针外置。>threshold 落盘，否则原样返回。

        返回：指针 dict 或原内容引用。summary 只做长度截断的提示，不谎称语义摘要。
        """
        if isinstance(content, str):
            size = len(content.encode("utf-8"))
        else:
            content = canonical(content)
            size = len(content.encode("utf-8"))
        if size <= ARTIFACT_PTR_THRESHOLD:
            return {"inline": True, "content": content, "size": size}
        artifact_id = digest({"content": content})[:16]
        path = self.artifacts_dir / f"{artifact_id}.txt"
        if not path.exists():
            path.write_text(content, encoding="utf-8")
        data = self._index()
        data["artifacts"][artifact_id] = {
            "artifact_id": artifact_id, "sha256_prefix": artifact_id,
            "size": size, "importance": importance, "last_access": 0,
            "summary": self._abbrev(content),
        }
        self._save_index(data)
        return {"inline": False, "artifact_id": artifact_id,
                "sha256_prefix": artifact_id, "summary": self._abbrev(content), "size": size}

    def _abbrev(self, content):
        return content[: _ABBREV * 4]  # 简化截断提示，不构造虚假语义摘要

    def touch(self, artifact_id):
        data = self._index()
        art = data["artifacts"].get(artifact_id)
        if art:
            art["last_access"] = art.get("last_access", 0) + 1
            self._save_index(data)

    def account_tokens(self, n: int):
        """累加会话 token 用量，超 SLO-4 预算即报错（拒绝且不记账）。"""
        if self.session_used + n > SESSION_LIMIT:
            raise ProtocolError(f"超过 SLO-4 会话预算 {SESSION_LIMIT} token")
        self.session_used += n
        return self.session_used

    # ---- 驱逐策略：LRU + importance ----
    def evict(self, target_size=0, stage_budget=STAGE_LIMIT):
        """驱逐至 stage_budget 内（LRU + importance 加权）。

        score = importance * 100 + last_access；低分优先被驱逐。每驱逐一个就按它的
        size 递减 target_size，达标即停（否则会把全部 artifact 清空）。被驱逐的条目
        同时从索引移除，索引与磁盘保持一致。返回被驱逐列表。
        """
        if target_size <= stage_budget:
            return []
        data = self._index()
        arts = sorted(data["artifacts"].items(), key=lambda kv: (
            kv[1].get("importance", 0.0) * 100 + kv[1].get("last_access", 0)))
        evicted = []
        for artifact_id, meta in arts:
            if target_size <= stage_budget:
                break
            path = self.artifacts_dir / f"{artifact_id}.txt"
            if path.exists():
                path.unlink()
            target_size -= meta.get("size", 0)
            data["artifacts"].pop(artifact_id, None)
            evicted.append(artifact_id)
        if evicted:
            self._save_index(data)
        return evicted

    # ---- 投影/审计 ----
    def snapshot(self):
        return {
            "working_size": self.working_size(),
            "working_bytes": self.working_size(),
            "summary": self.summary(),
            "artifacts": list(self._index()["artifacts"].values()),
            "session_used": self.session_used,
        }
"""稿件事务流编排（写作/投稿/传播）。

在物化稿件（materialize 渲染的 md/tex/docx/pdf）之上记录发布事务流状态机：
    draft -> reviewed -> submitted -> published
- 状态存于项目 `<project>/.popper/manuscript-flow.json`（以稿件路径为键，各自独立）。
- 每次迁移经 Experiment._event(kind="manuscript_flow", ...) 写入事件链（append-only + hash），
  可被 Experiment.replay() 一并校验。
- 前向迁移走合法迁移表（单步前进）；回退需携带原因，且只能回退到更早状态。
- submit 前运行 gap 报告（复用 evidence.check_references / check_consistency），
  迁移不阻断，仅如实记录 gaps 与 gaps_present 供发布门禁决策。
- share 登记稿件去向（destination/timestamp/status），如实记录，不虚构发表。
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .core import ProtocolError, read_json, write_json
from .evidence import EvidenceStore, check_consistency, check_references


def _now():
    return datetime.now(timezone.utc).isoformat()


class ManuscriptFlow:
    """稿件事务状态机（挂在 Experiment 上，复用其 db 写事件链、home 存 json）。"""

    # 全序：数字越小越靠前；用于判定回退是否合法。
    ORDER = {"draft": 0, "reviewed": 1, "submitted": 2, "published": 3}
    ALLOWED = set(ORDER)
    # 合法前向单步迁移表（其余前向目标均为非法迁移）。
    FORWARD = {
        "draft": {"reviewed"},
        "reviewed": {"submitted"},
        "submitted": {"published"},
        "published": set(),
    }

    def __init__(self, experiment):
        self.experiment = experiment
        self.path = experiment.home / "manuscript-flow.json"
        self.data = self._load()

    # ---- 持久化 ------------------------------------------------------------
    def _load(self):
        if not self.path.is_file():
            return {"schema_version": "1.0", "manuscripts": {}}
        return read_json(self.path)

    def _save(self):
        write_json(self.path, self.data)

    @staticmethod
    def _key(manuscript):
        # 以稿件路径为键；未显式给定时使用默认占位键。
        return str(manuscript) if manuscript is not None else "default"

    def _blank(self, manuscript):
        return {"manuscript": manuscript, "state": "draft",
                "transitions": [], "shares": []}

    def _entry(self, manuscript):
        key = self._key(manuscript)
        manuscripts = self.data.setdefault("manuscripts", {})
        return manuscripts.setdefault(key, self._blank(key))

    def _record(self, entry, from_state, to_state, reason=None, extra=None):
        """迁移留痕：更新 json 状态 + 写入事件链。"""
        item = {"from": from_state, "to": to_state, "at": _now(), "reason": reason}
        if extra:
            item.update(extra)
        entry["transitions"].append(item)
        entry["state"] = to_state
        self._save()
        payload = {"manuscript": entry["manuscript"], "from": from_state,
                   "to": to_state, "reason": reason}
        if extra:
            payload.update(extra)
        with self.experiment.db:
            self.experiment._event("manuscript_flow", payload)

    # ---- 查询 --------------------------------------------------------------
    def current(self, manuscript):
        """读取某稿件当前状态。"""
        return self._entry(manuscript)["state"]

    def status(self):
        """汇总全部稿件状态。"""
        return {"schema_version": self.data["schema_version"],
                "manuscripts": self.data.get("manuscripts", {})}

    # ---- 状态机迁移 ---------------------------------------------------------
    def transition(self, manuscript, to, reason=None):
        """前向单步迁移（执行合法迁移表校验；非法迁移抛 ProtocolError）。"""
        if to not in self.ALLOWED:
            raise ProtocolError(f"未知目标状态: {to}")
        entry = self._entry(manuscript)
        from_state = entry["state"]
        if to not in self.FORWARD.get(from_state, set()):
            raise ProtocolError(f"非法迁移: {from_state} -> {to}")
        self._record(entry, from_state, to, reason)
        return {"status": to, "from": from_state, "manuscript": entry["manuscript"]}

    def revert(self, manuscript, to, reason):
        """回退到更早状态；必须携带原因，且仅允许退到全序更靠前状态。"""
        if to not in self.ALLOWED:
            raise ProtocolError(f"未知目标状态: {to}")
        if not isinstance(reason, str) or not reason.strip():
            raise ProtocolError("回退必须填写原因")
        entry = self._entry(manuscript)
        from_state = entry["state"]
        if self.ORDER[to] >= self.ORDER[from_state]:
            raise ProtocolError(f"非法回退: {from_state} -> {to}")
        self._record(entry, from_state, to, reason.strip())
        return {"status": to, "from": from_state, "manuscript": entry["manuscript"]}

    # ---- 投稿前 gap 报告 ----------------------------------------------------
    def gap_report(self, manuscript, evidence_dir=None):
        """submit 前对稿件引用的一致性检查。

        无 evidence store 时 gaps 含 missing_store；有 store 时复用
        check_references（unresolved 引用）与 check_consistency（未绑定证据的 claim）。
        仅报告，不阻断迁移。
        """
        gaps = []
        if evidence_dir is None:
            gaps.append("missing_store")
        elif manuscript is None:
            gaps.append("missing_manuscript")
        else:
            store = EvidenceStore(evidence_dir)
            for ref_id in check_references(manuscript, store)["unresolved"]:
                gaps.append(f"unresolved_reference:{ref_id}")
            for issue in check_consistency(manuscript, store)["issues"]:
                gaps.append(f"missing_evidence:{issue['claim_id']}")
        return {"gaps": gaps, "gaps_present": len(gaps) > 0}

    def submit(self, manuscript, evidence_dir=None, reason=None):
        """reviewed -> submitted；迁移前生成 gap 报告并随结果返回，不阻断。"""
        report = self.gap_report(manuscript, evidence_dir)
        result = self.transition(manuscript, "submitted", reason)
        result["gap_report"] = report
        return result

    def publish(self, manuscript, reason=None):
        """submitted -> published。"""
        return self.transition(manuscript, "published", reason)

    # ---- 传播/共享登记 ------------------------------------------------------
    def share(self, manuscript, destination=None, community_dir=None):
        """登记稿件去向（社区共享池/预印本/发表）。

        复用 corpus.share 的"登记去向、如实记录"契约语义：写入 manuscript-flow.json 的
        destination / timestamp / status，不虚构发表。传入 --community-dir 视为去向。
        """
        entry = self._entry(manuscript)
        if community_dir is not None:
            destination = str(Path(community_dir))
        if destination is None or not str(destination).strip():
            raise ProtocolError("共享需指定 destination 或 --community-dir")
        destination = str(destination)
        record = {"destination": destination, "status": "shared", "at": _now()}
        entry["shares"].append(record)
        self._save()
        with self.experiment.db:
            self.experiment._event("manuscript_flow", {
                "manuscript": entry["manuscript"], "action": "share",
                "destination": destination, "status": "shared", "at": record["at"]})
        return {"status": "registered", "manuscript": entry["manuscript"],
                "destination": destination, "shares": entry["shares"]}
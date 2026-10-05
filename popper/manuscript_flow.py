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

import json
from datetime import datetime, timezone
from pathlib import Path

from .core import ProtocolError, file_hash, read_json, write_json
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
    @staticmethod
    def _bound_directory(manuscript):
        path = Path(manuscript) if manuscript is not None else Path("")
        if path.name == "manuscript.md" and (path.parent / "binding-manifest.json").is_file():
            return path.parent
        return None

    def _transition(self, manuscript, to, reason=None, bound_verification=None):
        """Apply a forward transition; bound drafts require an internal proof."""
        if to not in self.ALLOWED:
            raise ProtocolError(f"未知目标状态: {to}")
        entry = self._entry(manuscript)
        bound_now = self._bound_directory(manuscript) is not None
        if bound_now:
            entry["binding_required"] = True
        if (to == "reviewed" and (bound_now or entry.get("binding_required"))
                and bound_verification is None):
            raise ProtocolError("证据绑定草稿须使用 review-bound 登记人工审校")
        if (to in {"submitted", "published"}
                and (bound_now or entry.get("binding_required"))
                and bound_verification is None):
            raise ProtocolError("证据绑定草稿须使用 submit-bound/publish-bound 重新校验")
        from_state = entry["state"]
        if to not in self.FORWARD.get(from_state, set()):
            raise ProtocolError(f"非法迁移: {from_state} -> {to}")
        self._record(entry, from_state, to, reason,
                     {"bound_verification": bound_verification}
                     if bound_verification is not None else None)
        return {"status": to, "from": from_state, "manuscript": entry["manuscript"]}

    def transition(self, manuscript, to, reason=None):
        """Forward transition for ordinary manuscripts; bound drafts use strict APIs."""
        return self._transition(manuscript, to, reason)

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
        if self._bound_directory(manuscript) is not None:
            raise ProtocolError("证据绑定草稿须使用 submit-bound 重新校验")
        report = self.gap_report(manuscript, evidence_dir)
        result = self.transition(manuscript, "submitted", reason)
        result["gap_report"] = report
        return result

    def publish(self, manuscript, reason=None):
        """submitted -> published。"""
        return self.transition(manuscript, "published", reason)

    def _verify_bound(self, manuscript, research_run_dir, evidence_dir,
                      draft_spec_path):
        from .research.bound_manuscript import verify_bound_draft

        bound_dir = self._bound_directory(manuscript)
        if bound_dir is None or Path(manuscript).resolve() != (bound_dir / "manuscript.md").resolve():
            raise ProtocolError("提交目标不是证据绑定草稿的 manuscript.md")
        verified = verify_bound_draft(
            self.experiment.root, research_run_dir, evidence_dir,
            draft_spec_path, bound_dir)
        return {
            "verification_level": verified["verification_level"],
            "claim_count": verified["claim_count"],
            "research_run_dir": str(Path(research_run_dir).resolve()),
            "evidence_dir": str(Path(evidence_dir).resolve()),
            "draft_spec_sha256": file_hash(draft_spec_path),
            "binding_manifest_sha256": file_hash(bound_dir / "binding-manifest.json"),
            "manuscript_sha256": file_hash(manuscript),
            "publication_attested": False,
        }

    def _verify_projection(self, manuscript):
        """Detect a file/DB split after a crash before a strict transition."""
        self.experiment.replay()
        key = self._key(manuscript)
        entry = self._entry(manuscript)
        recorded = self._recorded_transitions(key)
        projected = self._projected_transitions(key, entry)
        if (recorded != projected
                or entry["state"] != (recorded[-1]["to"] if recorded else "draft")):
            raise ProtocolError("稿件状态投影与审计事件链不一致")

    def _recorded_transitions(self, key):
        rows = self.experiment.db.execute(
            "SELECT payload FROM events WHERE kind='manuscript_flow' ORDER BY seq")
        return [payload for row in rows
                if (payload := json.loads(row["payload"])).get("manuscript") == key
                and "to" in payload]

    @staticmethod
    def _projected_transitions(key, entry):
        result = []
        for item in entry["transitions"]:
            value = {"manuscript": key, "from": item["from"],
                     "to": item["to"], "reason": item["reason"]}
            if "bound_verification" in item:
                value["bound_verification"] = item["bound_verification"]
            result.append(value)
        return result

    def recover_interrupted_projection(self, manuscript, reason):
        """Explicitly undo one JSON-only tail after a verified interrupted write.

        A recovery event is committed first. Retrying after a crash between that
        event and the JSON correction reuses the event instead of duplicating it.
        Arbitrary divergence or a DB-ahead projection remains fail-closed.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ProtocolError("恢复稿件状态必须填写原因")
        if not self.path.is_file():
            raise ProtocolError("稿件状态投影文件不存在")
        self.experiment.replay()
        self.data = self._load()
        key = self._key(manuscript)
        entry = self.data.get("manuscripts", {}).get(key)
        if not isinstance(entry, dict):
            raise ProtocolError("没有可恢复的稿件状态")
        recorded = self._recorded_transitions(key)
        projected = self._projected_transitions(key, entry)
        if (len(projected) != len(recorded) + 1
                or projected[:-1] != recorded
                or entry["state"] != projected[-1]["to"]):
            raise ProtocolError("仅允许恢复单个未写入事件链的尾部迁移")
        previous = recorded[-1]["to"] if recorded else "draft"
        discarded = projected[-1]
        if (discarded["from"] != previous
                or discarded["to"] not in self.ALLOWED
                or discarded["to"] == previous):
            raise ProtocolError("尾部迁移与事件链状态不一致")
        prior_hash = file_hash(self.path)
        existing = None
        for row in self.experiment.db.execute(
                "SELECT payload FROM events "
                "WHERE kind='manuscript_projection_recovery' ORDER BY seq DESC"):
            candidate = json.loads(row["payload"])
            if (candidate.get("manuscript") == key
                    and candidate.get("projection_sha256") == prior_hash
                    and candidate.get("discarded_transition") == discarded):
                existing = candidate
                break
        if existing is None:
            payload = {"manuscript": key, "projection_sha256": prior_hash,
                       "discarded_transition": discarded, "reason": reason.strip()}
            with self.experiment.db:
                self.experiment._event("manuscript_projection_recovery", payload)
        entry["transitions"].pop()
        entry["state"] = previous
        self._save()
        self._verify_projection(manuscript)
        return {"status": "recovered", "manuscript": key,
                "restored_state": previous, "discarded_state": discarded["to"],
                "replayed": existing is not None}

    def submit_bound(self, manuscript, research_run_dir, evidence_dir,
                     draft_spec_path, reason=None):
        """Record a reviewed bound draft only after live authority verification."""
        self._verify_projection(manuscript)
        entry = self._entry(manuscript)
        if entry["state"] != "reviewed":
            raise ProtocolError("证据绑定草稿必须先通过 review-bound")
        reviewed = next((item.get("bound_verification") for item in
                         reversed(entry["transitions"]) if item["to"] == "reviewed"), None)
        if reviewed is None:
            raise ProtocolError("缺少人工审校时的证据绑定校验记录；请先回退到 draft 再 review-bound")
        proof = self._verify_bound(manuscript, research_run_dir, evidence_dir,
                                   draft_spec_path)
        if proof != reviewed:
            raise ProtocolError("证据绑定草稿与人工审校时的冻结校验记录不同")
        result = self._transition(manuscript, "submitted", reason, proof)
        result["bound_verification"] = proof
        return result

    def review_bound(self, manuscript, research_run_dir, evidence_dir,
                     draft_spec_path, reason=None):
        """Record an explicit human review only after checking the bound draft."""
        if not isinstance(reason, str) or not reason.strip():
            raise ProtocolError("人工审校记录必须填写说明")
        self._verify_projection(manuscript)
        proof = self._verify_bound(manuscript, research_run_dir, evidence_dir,
                                   draft_spec_path)
        result = self._transition(manuscript, "reviewed", reason.strip(), proof)
        result["bound_verification"] = proof
        return result

    def publish_bound(self, manuscript, research_run_dir, evidence_dir,
                      draft_spec_path, reason=None):
        """Revalidate the same bound draft before recording local publication state."""
        self._verify_projection(manuscript)
        entry = self._entry(manuscript)
        if entry["state"] != "submitted":
            raise ProtocolError("证据绑定草稿必须先通过 submit-bound")
        prior = next((item.get("bound_verification") for item in
                      reversed(entry["transitions"]) if item["to"] == "submitted"), None)
        if prior is None:
            raise ProtocolError("缺少先前提交的证据绑定校验记录")
        current = self._verify_bound(manuscript, research_run_dir, evidence_dir,
                                     draft_spec_path)
        if current != prior:
            raise ProtocolError("证据绑定草稿与提交时的冻结校验记录不同")
        result = self._transition(manuscript, "published", reason, current)
        result["bound_verification"] = current
        result["publication_attested"] = False
        return result

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

"""研究存储（R0）· SQLite 追加记录 + 状态机 + 权威写入方。

- 权威写入方是结构而非参数：本存储自己写 study/hypothesis/design/decision/
  reflection；observation 与 run 必须持有本模块签发的窄接口凭据
  （ObservationWriter / RunWriter），调用方无法用字符串自报写入方。
- 非法状态转移使整个事务回滚，不留半成品。
- 事件链 sha256 校验：写路径只做 O(1) 链头校验（外部改动会被立即拒绝），
  全量重放由 verify() 显式提供，供审计与启动门禁调用。
- 预算账本与事件、快照同库同连接：预留在同一事务内提交，不存在跨库补偿。
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..core import ProtocolError, digest, file_hash, read_json
from .budget import BudgetLedger
from .contracts import (Decision, DesignStatus, ExperimentDesign, Hypothesis,
                        HypothesisStatus, Observation, RunStatus, Study,
                        StudyStatus, TransitionError, validate_design_transition,
                        validate_hypothesis_revision, validate_hypothesis_transition,
                        validate_run_transition, validate_study_transition)
from .schema import migrate_research

# 实体 → 权威写入方。写入方由代码路径决定，不再由调用方作为参数传入。
# 值必须是真实存在的写入方：study 类实体由本模块自写；observation 的权威是独立
# 评估服务 evaluation_service.py；run 的权威是持有 RunWriter 能力的编排内核
# controller.py（worker 只执行，不碰存储）。
AUTHORITY = {
    "study": "research_store",
    "hypothesis": "research_store",
    "design": "research_store",
    "decision": "research_store",
    "reflection": "research_store",
    "observation": "evaluation_service",
    "run": "research_controller",
}

# 已退役的权威名 → 当前名。历史研究库里 run 事件记录为 experiment_service，
# 但该模块从未存在（真实写入方是 controller.py）。归档是不可改写的审计证据，
# 改链重算等于篡改证据，因此 verify() 继续接受退役名；新写入一律用当前名。
RETIRED_WRITERS = {("run", "experiment_service"): "research_controller"}

# 写入能力的签发令牌：只有本模块能构造能力对象。
_CAPABILITY = object()

_TERMINAL_STUDIES = {StudyStatus.CONCLUDED, StudyStatus.BUDGET_EXHAUSTED,
                     StudyStatus.BLOCKED_EXTERNAL, StudyStatus.INTEGRITY_FAILED}

# 每种实体的状态转移表：公开写方法与反思通道共用，避免某条通道绕过状态机。
_TRANSITION_VALIDATORS = {
    "study": validate_study_transition,
    "hypothesis": validate_hypothesis_transition,
    "design": validate_design_transition,
    "run": validate_run_transition,
}


class ObservationWriter:
    """observation 权威写入能力（evaluation_service）。

    只能由 ResearchStore 签发。调用方拿不到「写入方」这个可伪造的字符串，
    也就不可能把自己声明成评估服务。
    """

    def __init__(self, store, token):
        if token is not _CAPABILITY:
            raise ProtocolError("ObservationWriter 只能由 ResearchStore 签发")
        self._store = store

    def add(self, observation: Observation):
        return self._store._add_observation(observation)


class RunWriter:
    """run 权威写入能力（权威写入方 research_controller）。只能由 ResearchStore 签发。"""

    def __init__(self, store, token):
        if token is not _CAPABILITY:
            raise ProtocolError("RunWriter 只能由 ResearchStore 签发")
        self._store = store

    def register(self, run_id, design_id):
        return self._store._register_run(run_id, design_id)

    def set_status(self, run_id, target: RunStatus):
        return self._store._set_run_status(run_id, target)


class ResearchStore:
    """研究决策存储：快照表 + 追加事件链，单写事务。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._terminal_confirmation_allowed = False
        self._in_transaction = False
        migrate_research(self._conn)
        # 窄接口写入能力：observation 归评估服务，run 归实验服务。
        self.observations = ObservationWriter(self, _CAPABILITY)
        self.runs = RunWriter(self, _CAPABILITY)

    def close(self):
        self._conn.close()

    @property
    def _unsafe_conn(self):
        """测试专用：绕过公开 API 直接篡改，以验证校验能捕获。生产代码禁止使用。"""
        return self._conn

    # ---- 事件链 ----
    def _append(self, kind, entity_id, event_type, payload, writer):
        if AUTHORITY.get(kind) != writer:
            raise ProtocolError(
                f"权威写入方冲突: {kind} 只能由 {AUTHORITY.get(kind)} 写入（收到 {writer}）")
        row = self._conn.execute(
            "SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row[0] if row else "0" * 64
        body = {"kind": kind, "entity_id": entity_id, "event_type": event_type,
                "payload": payload, "writer": writer, "previous": previous}
        event_hash = digest(body)
        self._conn.execute(
            "INSERT INTO events(kind, entity_id, event_type, payload, writer,"
            " previous_hash, hash) VALUES(?,?,?,?,?,?,?)",
            (kind, entity_id, event_type,
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             writer, previous, event_hash))
        seq = self._conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        self._conn.execute(
            "INSERT OR REPLACE INTO store_meta(key, value) VALUES('chain_head', ?)",
            (json.dumps([seq, event_hash]),))
        return event_hash

    def _upsert(self, kind, entity_id, status, payload, writer, seq):
        self._conn.execute(
            "INSERT OR REPLACE INTO snapshots(kind, entity_id, status, payload,"
            " writer, updated_seq) VALUES(?,?,?,?,?,?)",
            (kind, entity_id, getattr(status, "value", str(status)),
             json.dumps(payload, ensure_ascii=False, sort_keys=True),
             writer, seq))

    def _require_study_active(self, study_id):
        study = self.get("study", study_id)
        if study is None:
            raise ProtocolError(f"study 不存在: {study_id}")
        if study["status"] in _TERMINAL_STUDIES and not self._terminal_confirmation_allowed:
            raise TransitionError(f"study 已终结（{study['status']}），不再接受新动作")

    def terminal_confirmation_writes(self):
        """允许对已终结研究写入后验独立确认证据（签名 holdout + 预注册切片）。

        确认是与策略行为解耦的后验审计；研究在 concluded/budget_exhausted 终止时
        仍未消费保留集，此时写入的确认 Observation/Decision/Run 是审计所需证据，
        不改变研究结论语义。其余写路径仍拒绝终结研究。
        """
        from contextlib import contextmanager

        @contextmanager
        def _allow():
            previous = self._terminal_confirmation_allowed
            self._terminal_confirmation_allowed = True
            try:
                yield
            finally:
                self._terminal_confirmation_allowed = previous

        return _allow()

    def _entity_exists(self, kind, entity_id):
        return self._conn.execute(
            "SELECT 1 FROM snapshots WHERE kind = ? AND entity_id = ?",
            (kind, entity_id)).fetchone() is not None

    def _study_version(self, study_id):
        row = self._conn.execute(
            "SELECT version FROM study_versions WHERE study_id = ?", (study_id,)).fetchone()
        if row is None:
            raise ProtocolError(f"study 缺少状态版本: {study_id}")
        return row[0]

    def _touch_study(self, study_id):
        changed = self._conn.execute(
            "UPDATE study_versions SET version = version + 1 WHERE study_id = ?", (study_id,))
        if changed.rowcount != 1:
            raise ProtocolError(f"study 缺少状态版本: {study_id}")
        return self._study_version(study_id)

    @staticmethod
    def _same_payload(current, proposed):
        return {k: v for k, v in current.items() if k != "status"} == proposed

    def _check_chain_head(self):
        """O(1) 链头校验：事件表被外部删除、追加或替换时立即失败。

        这不替代 verify() 的全量重放——就地改写历史事件载荷只能由显式 verify()
        发现。写路径上不做 O(n) 重放，避免整体写入退化为 O(n²)。
        """
        row = self._conn.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        expected = [row[0], row[1]] if row else [0, "0" * 64]
        stored = self._conn.execute(
            "SELECT value FROM store_meta WHERE key = 'chain_head'").fetchone()
        if stored is None:
            # 全新库：没有事件也没有链头，属合法初始态。一旦有事件却缺链头，
            # 说明记录被删改，必须失败。
            if row is None:
                return
            raise ProtocolError(
                "research store 链头校验失败：事件表被外部改动，请运行 verify() 审计")
        if json.loads(stored[0]) != expected:
            raise ProtocolError(
                "research store 链头校验失败：事件表被外部改动，请运行 verify() 审计")

    def _transaction(self, fn):
        self._check_chain_head()
        owner = not self._in_transaction
        if owner:
            self._begin()
        try:
            result = fn()
        except Exception:
            if owner:
                self._end(failed=True)
            raise
        if owner:
            self._end(failed=False)
        return result

    @contextmanager
    def transaction(self):
        """单元事务：把多个公开写方法与预算预留合并成一个原子单元。

        事务内部再调用公开写方法是安全的（不重复提交），因此控制器不再需要
        「先记账、失败后手工回滚」的跨库补偿。
        """
        self._check_chain_head()
        owner = not self._in_transaction
        if owner:
            self._begin()
        try:
            yield self
        except Exception:
            if owner:
                self._end(failed=True)
            raise
        if owner:
            self._end(failed=False)

    def _begin(self):
        # BEGIN IMMEDIATE：先取写锁再校验，避免验证与写入之间被别的写入方插入。
        self._conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True

    def _end(self, failed):
        self._in_transaction = False
        if failed:
            self._conn.rollback()
        else:
            self._conn.commit()

    # ---- Study ----
    def create_study(self, study: Study):
        writer = AUTHORITY["study"]

        def _do():
            if self._entity_exists("study", study.study_id):
                raise ProtocolError(f"study 已存在: {study.study_id}")
            # 预算族登记与 study 快照在同一次事务内提交：不存在「建了 study 但
            # 预算没登记」或反过来的中间态。
            BudgetLedger(self._conn).open_family(study.family_id, study.budget_cap)
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            payload = {"family_id": study.family_id, "question": study.question,
                       "scope": study.scope, "data_exposure": list(study.data_exposure),
                       "budget_cap": study.budget_cap}
            self._append("study", study.study_id, "created", payload, writer)
            self._upsert("study", study.study_id, study.status, payload, writer, seq)
            self._conn.execute(
                "INSERT INTO study_versions(study_id, version) VALUES(?, 1)",
                (study.study_id,))
            return self.get("study", study.study_id)
        return self._transaction(_do)

    def set_study_status(self, study_id, target: StudyStatus):
        writer = AUTHORITY["study"]

        def _do():
            current = self.get("study", study_id)
            if current is None:
                raise ProtocolError(f"study 不存在: {study_id}")
            validate_study_transition(current["status"], target)
            if target != StudyStatus.INTEGRITY_FAILED:
                active = [row[0] for row in self._conn.execute(
                    "SELECT entity_id FROM snapshots WHERE kind = 'run' "
                    "AND status IN (?, ?)",
                    (RunStatus.QUEUED.value, RunStatus.RUNNING.value))
                    if self.get("run", row[0]).get("study_id") == study_id]
                if active:
                    raise TransitionError(
                        f"study 仍有未终结 Run，不能进入 {target.value}: {active}")
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            self._append("study", study_id, "status", {"to": target.value}, writer)
            payload = {k: v for k, v in current.items()
                       if k not in {"status", "state_version"}}
            self._upsert("study", study_id, target, payload, writer, seq)
            self._touch_study(study_id)
            return self.get("study", study_id)
        return self._transaction(_do)

    # ---- Hypothesis（修订只能新增版本） ----
    def add_hypothesis(self, hypothesis: Hypothesis):
        def _do():
            self._require_study_active(hypothesis.study_id)
            return self._put_hypothesis(hypothesis, AUTHORITY["hypothesis"])
        return self._transaction(_do)

    def _put_hypothesis(self, hypothesis, writer):
        if self._entity_exists("hypothesis", hypothesis.hypothesis_id):
            raise ProtocolError(f"hypothesis 已存在: {hypothesis.hypothesis_id}")
        if hypothesis.version != 1:
            raise TransitionError("新假设版本必须为 1")
        if hypothesis.parent_version is not None:
            raise TransitionError("新假设不能有父版本")
        if hypothesis.status != HypothesisStatus.UNTESTED:
            raise TransitionError("新假设必须从 untested 状态开始")
        seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
        payload = _hypothesis_payload(hypothesis)
        self._append("hypothesis", hypothesis.hypothesis_id, "created",
                     {**payload, "version": 1}, writer)
        self._upsert("hypothesis", hypothesis.hypothesis_id, hypothesis.status,
                     {**payload, "version": 1}, writer, seq)
        self._touch_study(hypothesis.study_id)
        return self.get("hypothesis", hypothesis.hypothesis_id)

    def revise_hypothesis(self, revision: Hypothesis):
        writer = AUTHORITY["hypothesis"]

        def _do():
            self._require_study_active(revision.study_id)
            current = self.get("hypothesis", revision.hypothesis_id)
            if current is None:
                raise ProtocolError(f"hypothesis 不存在: {revision.hypothesis_id}")
            if revision.study_id != current["study_id"]:
                raise TransitionError("假设修订不能迁移到另一个 study")
            if revision.version != current["version"] + 1:
                raise TransitionError(
                    f"假设修订必须追加版本: 当前 {current['version']}，修订必须为 "
                    f"{current['version'] + 1}（收到 {revision.version}）")
            if revision.parent_version != current["version"]:
                raise TransitionError("parent_version 必须指向当前版本")
            validate_hypothesis_revision(current["status"])
            if revision.status != HypothesisStatus.UNTESTED:
                raise TransitionError("假设修订版本必须重新从 untested 状态开始")
            payload = _hypothesis_payload(revision)
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            self._append("hypothesis", revision.hypothesis_id, "revised",
                         {**payload, "version": revision.version}, writer)
            self._upsert("hypothesis", revision.hypothesis_id, revision.status,
                         {**payload, "version": revision.version}, writer, seq)
            self._touch_study(revision.study_id)
            return self.get("hypothesis", revision.hypothesis_id)
        return self._transaction(_do)

    def set_hypothesis_status(self, hypothesis_id, version, target: HypothesisStatus):
        writer = AUTHORITY["hypothesis"]

        def _do():
            current = self.get("hypothesis", hypothesis_id)
            if current is None:
                raise ProtocolError(f"hypothesis 不存在: {hypothesis_id}")
            self._require_study_active(current["study_id"])
            if current["version"] != version:
                raise TransitionError(
                    f"状态更新必须针对当前版本: 当前 {current['version']}，收到 {version}")
            validate_hypothesis_transition(current["status"], target)
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            self._append("hypothesis", hypothesis_id, "status",
                         {"version": version, "to": target.value}, writer)
            payload = {k: v for k, v in current.items() if k != "status"}
            self._upsert("hypothesis", hypothesis_id, target, payload, writer, seq)
            self._touch_study(current["study_id"])
            return self.get("hypothesis", hypothesis_id)
        return self._transaction(_do)

    # ---- ExperimentDesign（冻结后不可变） ----
    def create_design(self, design: ExperimentDesign):
        writer = AUTHORITY["design"]

        def _do():
            if design.status != DesignStatus.DRAFT:
                raise TransitionError("新设计必须以 DRAFT 状态创建")
            hypothesis = self.get("hypothesis", design.hypothesis_id)
            if hypothesis is None:
                raise ProtocolError(f"hypothesis 不存在: {design.hypothesis_id}")
            self._require_study_active(hypothesis["study_id"])
            if not self._entity_exists("design", design.design_id):
                seq = self._conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
                payload = {**_design_payload(design), "study_id": hypothesis["study_id"],
                           "hypothesis_version": hypothesis["version"]}
                self._append("design", design.design_id, "created", payload, writer)
                self._upsert("design", design.design_id, design.status,
                             payload, writer, seq)
                self._touch_study(hypothesis["study_id"])
                return self.get("design", design.design_id)
            raise ProtocolError(f"design 已存在: {design.design_id}")
        return self._transaction(_do)

    def freeze_design(self, design_id):
        writer = AUTHORITY["design"]

        def _do():
            current = self.get("design", design_id)
            if current is None:
                raise ProtocolError(f"design 不存在: {design_id}")
            self._require_study_active(current["study_id"])
            validate_design_transition(current["status"], DesignStatus.FROZEN)
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            self._append("design", design_id, "frozen", {"to": "frozen"}, writer)
            self._upsert("design", design_id, DesignStatus.FROZEN,
                         {k: v for k, v in current.items() if k != "status"}, writer, seq)
            self._touch_study(current["study_id"])
            return self.get("design", design_id)
        return self._transaction(_do)

    # ---- Observation / Decision（不可变，幂等） ----
    def _add_observation(self, observation: Observation):
        """仅供 ObservationWriter 调用：observation 的权威写入方是评估服务。"""
        writer = AUTHORITY["observation"]

        def _do():
            if self._entity_exists("observation", observation.observation_id):
                current = self.get("observation", observation.observation_id)
                proposed = {"observation_id": observation.observation_id,
                            "run_id": observation.run_id, "scorer_id": observation.scorer_id,
                            "value": observation.value, "unit": observation.unit,
                            "uncertainty": observation.uncertainty, "scope": observation.scope,
                            "artifact_id": observation.artifact_id,
                            "artifact_sha256": observation.artifact_sha256,
                            "selector": observation.selector,
                            "evaluator_service_hash": observation.evaluator_service_hash,
                            "trust": observation.trust}
                if all(current.get(k) == v for k, v in proposed.items()):
                    return {"status": "duplicate", "observation_id": observation.observation_id}
                raise ProtocolError("相同 observation_id 对应不同内容")
            run = self.get("run", observation.run_id)
            if run is None:
                raise ProtocolError(f"run 不存在: {observation.run_id}")
            if run["status"] != RunStatus.SUCCEEDED.value:
                raise TransitionError("只有成功完成的 Run 可以产生 Observation")
            design = self.get("design", run["design_id"])
            self._require_study_active(design["study_id"])
            if observation.scorer_id != design["scorer_id"]:
                raise ProtocolError(
                    f"Observation scorer 与冻结设计不一致: {observation.scorer_id} != "
                    f"{design['scorer_id']}")
            if observation.artifact_id:
                artifact = Path(observation.artifact_id)
                if not artifact.is_file() or file_hash(artifact) != observation.artifact_sha256:
                    raise ProtocolError("Observation 评估制品不存在或摘要不一致")
                try:
                    selected = read_json(artifact)
                    for part in observation.selector.split("."):
                        selected = selected[int(part)] if isinstance(selected, list) else selected[part]
                except (OSError, ValueError, KeyError, IndexError, TypeError):
                    raise ProtocolError("Observation selector 无法从评估制品重算") from None
                if str(selected) != str(observation.value):
                    raise ProtocolError("Observation value 与评估制品不一致")
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            payload = {"observation_id": observation.observation_id,
                       "run_id": observation.run_id, "scorer_id": observation.scorer_id,
                       "value": observation.value, "unit": observation.unit,
                       "uncertainty": observation.uncertainty, "scope": observation.scope,
                       "artifact_id": observation.artifact_id,
                       "artifact_sha256": observation.artifact_sha256,
                       "selector": observation.selector,
                       "evaluator_service_hash": observation.evaluator_service_hash,
                       "trust": observation.trust,
                       "study_id": design["study_id"], "design_id": run["design_id"],
                       "hypothesis_id": design["hypothesis_id"],
                       "hypothesis_version": design["hypothesis_version"]}
            self._append("observation", observation.observation_id, "created", payload, writer)
            self._upsert("observation", observation.observation_id, "computed",
                         payload, writer, seq)
            self._touch_study(design["study_id"])
            return {"status": "added", "observation_id": observation.observation_id}
        return self._transaction(_do)

    def add_decision(self, decision: Decision):
        writer = AUTHORITY["decision"]

        def _do():
            if self._entity_exists("decision", decision.decision_id):
                current = self.get("decision", decision.decision_id)
                proposed = {"study_id": decision.study_id, "state_version": decision.state_version,
                            "observation_refs": list(decision.observation_refs),
                            "action": decision.action, "alternatives": list(decision.alternatives),
                            "rationale": decision.rationale,
                            "budget_request": decision.budget_request,
                            "actor": decision.actor, "model": decision.model}
                if self._same_payload(current, proposed):
                    return {"status": "duplicate", "decision_id": decision.decision_id}
                raise ProtocolError("相同 decision_id 对应不同内容")
            self._require_study_active(decision.study_id)
            current_version = self._study_version(decision.study_id)
            if decision.state_version != current_version:
                raise TransitionError(
                    f"决策基于陈旧或不存在的 study 状态: 当前 {current_version}，"
                    f"收到 {decision.state_version}")
            for ref in decision.observation_refs:
                if not self._entity_exists("observation", ref):
                    raise ProtocolError(f"决策引用了不存在的观察: {ref}")
                observation = self.get("observation", ref)
                if observation.get("study_id") != decision.study_id:
                    raise ProtocolError(f"决策引用了其他 study 的观察: {ref}")
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            payload = {"study_id": decision.study_id, "state_version": decision.state_version,
                       "observation_refs": list(decision.observation_refs),
                       "action": decision.action, "alternatives": list(decision.alternatives),
                       "rationale": decision.rationale, "budget_request": decision.budget_request,
                       "actor": decision.actor, "model": decision.model}
            self._append("decision", decision.decision_id, "created", payload, writer)
            self._upsert("decision", decision.decision_id, "recorded", payload, writer, seq)
            self._touch_study(decision.study_id)
            return {"status": "recorded", "decision_id": decision.decision_id}
        return self._transaction(_do)

    def apply_reflection(self, reflection_id, study_id, result,
                         expected_state_version, actor, model=None):
        """Atomically record evidence-driven reflection and its pending control.

        Revised hypotheses and designs are appended before the reflection and
        decision. No public write method is nested here: one transaction owns
        every event, so a failed append cannot leave a partially revised plan.
        The existing study and budget ledger retain their identities.
        """
        fields = {"action", "rationale", "alternative_explanation",
                  "next_hypothesis_id", "revision", "evidence_refs"}
        if (not isinstance(reflection_id, str) or not reflection_id
                or not isinstance(study_id, str) or not study_id
                or not isinstance(actor, str) or not actor.strip()
                or (model is not None and not isinstance(model, str))
                or not isinstance(result, dict) or set(result) != fields):
            raise ProtocolError("Reflection 身份或字段不合法")
        if result["action"] not in {"add_control", "request_confirmation", "stop",
                                    "request_scope_boundary_confirmation"}:
            raise ProtocolError("Reflection action 不合法")
        for name in ("rationale", "alternative_explanation"):
            if not isinstance(result[name], str) or not result[name].strip():
                raise ProtocolError(f"Reflection {name} 不能为空")
        refs = result["evidence_refs"]
        if (not isinstance(refs, list) or not 2 <= len(refs) <= 16
                or any(not isinstance(ref, str) or not ref for ref in refs)
                or len(set(refs)) != len(refs)):
            raise ProtocolError("Reflection 必须依序引用基线与候选两个不同 Observation")
        target_id, revision = result["next_hypothesis_id"], result["revision"]
        if result["action"] == "add_control":
            if not isinstance(target_id, str) or not target_id:
                raise ProtocolError("Reflection add_control 必须指定下一假设")
        elif target_id is not None or revision is not None:
            raise ProtocolError("只有 add_control 可以指定或修订下一假设")
        scientific_fields = {"mechanism", "applicability", "predictions",
                             "falsification", "alternatives"}
        if revision is not None:
            if (not isinstance(revision, dict) or not revision
                    or not set(revision).issubset(scientific_fields)):
                raise ProtocolError("Reflection revision 只能修改科学文本字段")
            for name, value in revision.items():
                if name in {"mechanism", "applicability"}:
                    valid = isinstance(value, str) and bool(value.strip())
                else:
                    valid = (isinstance(value, list) and bool(value)
                             and all(isinstance(item, str) and item.strip() for item in value))
                if not valid:
                    raise ProtocolError(f"Reflection revision {name} 格式不合法")
        if result["action"] == "add_control":
            predictions = revision.get("predictions", []) if revision is not None else []
            if len({item.strip().casefold() for item in predictions}) < 2:
                raise ProtocolError("Reflection add_control 必须修订至少两个不同的区分性预测")
        # Detach caller-owned lists/dictionaries from the immutable stored value.
        proposed = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
        refs = proposed["evidence_refs"]
        target_id, revision = proposed["next_hypothesis_id"], proposed["revision"]
        decision_id = "DEC-" + reflection_id

        def _write(kind, entity_id, event_type, status, payload, event_payload=None,
                   from_status=None):
            # 反思通道与公开写方法走同一张转移表：不允许绕过状态机直接落终态。
            if from_status is not None:
                _TRANSITION_VALIDATORS[kind](from_status, status)
            writer = AUTHORITY[kind]
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            self._append(kind, entity_id, event_type,
                         payload if event_payload is None else event_payload, writer)
            self._upsert(kind, entity_id, status, payload, writer, seq)
            self._touch_study(study_id)

        def _do():
            # 写锁已由 _transaction 的 BEGIN IMMEDIATE 取得：另一个写入方不能在
            # 校验与这些事件之间插入一次 Run 安排。
            existing = self.get("reflection", reflection_id)
            if existing is not None:
                identity = {**proposed, "study_id": study_id, "actor": actor,
                            "model": model, "decision_id": decision_id}
                if all(existing.get(key) == value for key, value in identity.items()):
                    return existing
                raise ProtocolError("相同 reflection_id 对应不同内容")
            self._require_study_active(study_id)
            source_version = self._study_version(study_id)
            if type(expected_state_version) is not int or expected_state_version != source_version:
                raise TransitionError("Reflection 基于陈旧或不存在的 study 状态")
            observations, evidence_designs = [], []
            for ref in refs:
                observation = self.get("observation", ref)
                if (observation is None or observation.get("study_id") != study_id
                        or not isinstance(observation.get("scope"), str)
                        or not (observation["scope"] == "dev"
                                or observation["scope"].startswith("dev:slice:"))):
                    raise ProtocolError("Reflection 必须引用本 study 的真实开发集 Observation")
                run = self.get("run", observation["run_id"])
                design = self.get("design", observation["design_id"])
                if (run is None or run["status"] != RunStatus.SUCCEEDED.value
                        or run["design_id"] != observation["design_id"]
                        or design is None or design["study_id"] != study_id
                        or design["hypothesis_id"] != observation["hypothesis_id"]
                        or design["hypothesis_version"] != observation["hypothesis_version"]):
                    raise ProtocolError("Reflection Observation 与成功 Run/冻结设计不一致")
                observations.append(observation)
                evidence_designs.append(design)
            baseline, candidate = observations[:2]
            baseline_design, candidate_design = evidence_designs[:2]
            if (not baseline["hypothesis_id"].startswith("H-control-")
                    or candidate["hypothesis_id"].startswith("H-control-")
                    or baseline["scope"] != "dev" or candidate["scope"] != "dev"
                    or baseline["hypothesis_id"] == candidate["hypothesis_id"]
                    or baseline_design["interventions"] != [baseline_design["baseline"]]
                    or baseline_design["control"] != baseline_design["baseline"]
                    or any(candidate_design[name] != baseline_design[name] for name in
                           ("baseline", "control", "scorer_id", "metric", "metric_direction"))):
                raise ProtocolError("Reflection evidence_refs 必须依序为同一实验的基线与候选")
            effect = candidate["value"] - baseline["value"]
            if candidate_design["metric_direction"] == "min":
                effect = -effect
            if not math.isfinite(effect):
                raise ProtocolError("Reflection 方向统一效应必须是有限数值")
            threshold = candidate_design["min_meaningful_effect"]
            slice_values = {}
            for extra in observations[2:]:
                if (extra["hypothesis_id"] not in {baseline["hypothesis_id"],
                                                    candidate["hypothesis_id"]}
                        or not extra["scope"].startswith("dev:slice:")):
                    raise ProtocolError(
                        "Reflection slice evidence must compare the same hypotheses")
                values = slice_values.setdefault(extra["scope"], {})
                if extra["hypothesis_id"] in values:
                    raise ProtocolError("Reflection slice evidence is duplicated")
                values[extra["hypothesis_id"]] = extra["value"]
            slice_effects = []
            for values in slice_values.values():
                if set(values) != {baseline["hypothesis_id"], candidate["hypothesis_id"]}:
                    raise ProtocolError(
                        "Reflection slice evidence must contain baseline and candidate")
                raw = values[candidate["hypothesis_id"]] - values[baseline["hypothesis_id"]]
                slice_effects.append(-raw if candidate_design["metric_direction"] == "min" else raw)
            boundary = (abs(effect) < threshold and len(slice_effects) >= 2
                        and max(slice_effects) >= threshold and min(slice_effects) < threshold)
            has_remaining = any(
                row["study_id"] == study_id and row["status"] == HypothesisStatus.UNTESTED.value
                and row["hypothesis_id"] != candidate["hypothesis_id"]
                for row in self.list("hypothesis"))
            ledger = self.budget(study_id)
            available = ledger.balance(self.get("study", study_id)["family_id"])["available"]
            allowed = ({"request_confirmation"} if effect >= threshold else
                       {"request_scope_boundary_confirmation"} if boundary else
                       {"add_control"} if has_remaining and available >= 1 else {"stop"})
            if proposed["action"] not in allowed:
                raise ProtocolError("Reflection action 与权威开发证据及冻结阈值不一致")
            if proposed["action"] == "add_control":
                study = self.get("study", study_id)
                if ledger.balance(study["family_id"])["available"] < 1:
                    raise ProtocolError("Reflection 剩余预算不足以安排对照")
            for prior in self.list("reflection"):
                if prior["study_id"] == study_id and prior["evidence_refs"][1] == refs[1]:
                    raise ProtocolError("同一候选 Observation 已存在 Reflection")
            next_version = next_design_id = None
            if target_id is not None:
                current = self.get("hypothesis", target_id)
                if (current is None or current["study_id"] != study_id
                        or current["status"] != HypothesisStatus.UNTESTED.value
                        or target_id in {baseline["hypothesis_id"], candidate["hypothesis_id"]}):
                    raise TransitionError("Reflection 只能选择本 study 尚未实验的其他候选")
                designs = [design for design in self.list("design")
                           if design["hypothesis_id"] == target_id and design["study_id"] == study_id]
                design_ids = {design["design_id"] for design in designs}
                if any(run["design_id"] in design_ids for run in self.list("run")):
                    raise TransitionError("Reflection 不能改写已有 Run 的候选")
                if any(prior["study_id"] == study_id and prior["next_hypothesis_id"] == target_id
                       for prior in self.list("reflection")):
                    raise TransitionError("Reflection 候选已有待执行对照安排")
                current_designs = [design for design in designs
                                   if design["hypothesis_version"] == current["version"]
                                   and design["status"] == DesignStatus.FROZEN.value]
                if len(current_designs) != 1:
                    raise ProtocolError("Reflection 候选必须绑定唯一的当前版本冻结设计")
                current_design = current_designs[0]
                next_version, next_design_id = current["version"], current_design["design_id"]
                if revision is not None:
                    if all(current[name] == value for name, value in revision.items()):
                        raise ProtocolError("Reflection revision 没有实际科学文本修改")
                    next_version += 1
                    next_design_id = "D-" + target_id.removeprefix("H-") + "-v" + str(next_version)
                    if self._entity_exists("design", next_design_id):
                        raise ProtocolError("Reflection 新版本设计已存在")
                    hypothesis_payload = {key: value for key, value in current.items() if key != "status"}
                    hypothesis_payload.update(revision)
                    hypothesis_payload.update(version=next_version, parent_version=current["version"])
                    validate_hypothesis_revision(current["status"])
                    _write("hypothesis", target_id, "revised", HypothesisStatus.UNTESTED,
                           hypothesis_payload)
                    design_payload = {key: value for key, value in current_design.items() if key != "status"}
                    design_payload.update(design_id=next_design_id, hypothesis_version=next_version)
                    _write("design", next_design_id, "created", DesignStatus.DRAFT, design_payload)
                    _write("design", next_design_id, "frozen", DesignStatus.FROZEN, design_payload,
                           {"to": DesignStatus.FROZEN.value}, from_status=DesignStatus.DRAFT)
            if self._entity_exists("decision", decision_id):
                raise ProtocolError("Reflection 对应 Decision 已存在且没有反思记录")
            payload = {**proposed, "reflection_id": reflection_id, "study_id": study_id,
                       "source_state_version": source_version, "actor": actor, "model": model,
                       "decision_id": decision_id, "next_hypothesis_version": next_version,
                       "next_design_id": next_design_id}
            _write("reflection", reflection_id, "created", "recorded", payload)
            decision_payload = {"study_id": study_id, "state_version": self._study_version(study_id),
                                "observation_refs": proposed["evidence_refs"], "action": proposed["action"],
                                "alternatives": [proposed["alternative_explanation"]],
                                "rationale": proposed["rationale"], "budget_request": 0.0,
                                "actor": actor, "model": model}
            _write("decision", decision_id, "created", "recorded", decision_payload)
            return self.get("reflection", reflection_id)
        return self._transaction(_do)

    # ---- Run 状态（实验服务权威；当前状态由存储读取，调用方不能自报） ----
    def _set_run_status(self, run_id, target: RunStatus):
        """仅供 RunWriter 调用：Run 的权威写入方是实验服务。"""
        writer = AUTHORITY["run"]

        def _do():
            current = self.get("run", run_id)
            if current is None:
                raise ProtocolError(f"run 不存在: {run_id}")
            validate_run_transition(RunStatus(current["status"]), target)
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            self._append("run", run_id, "status",
                         {"from": current["status"], "to": target.value}, writer)
            payload = {k: v for k, v in current.items() if k != "status"}
            self._upsert("run", run_id, target, payload, writer, seq)
            self._touch_study(current["study_id"])
            return self.get("run", run_id)
        return self._transaction(_do)

    def _register_run(self, run_id, design_id):
        """仅供 RunWriter 调用：Run 的权威写入方是实验服务。"""
        writer = AUTHORITY["run"]

        def _do():
            design = self.get("design", design_id)
            if design is None:
                raise ProtocolError(f"design 不存在: {design_id}")
            if design["status"] != DesignStatus.FROZEN.value:
                raise TransitionError("只有冻结设计可以产生 Run")
            self._require_study_active(design["study_id"])
            if self._entity_exists("run", run_id):
                raise ProtocolError(f"run 已存在: {run_id}")
            seq = self._conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM events").fetchone()[0]
            self._append("run", run_id, "registered",
                         {"run_id": run_id, "design_id": design_id, "study_id": design["study_id"],
                          "status": RunStatus.QUEUED.value}, writer)
            self._upsert("run", run_id, RunStatus.QUEUED,
                         {"run_id": run_id, "design_id": design_id,
                          "study_id": design["study_id"]}, writer, seq)
            self._touch_study(design["study_id"])
            return self.get("run", run_id)
        return self._transaction(_do)

    # ---- 查询 ----
    def get(self, kind, entity_id):
        row = self._conn.execute(
            "SELECT status, payload FROM snapshots WHERE kind = ? AND entity_id = ?",
            (kind, entity_id)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[1])
        payload["status"] = row[0]
        if kind == "study":
            payload["state_version"] = self._study_version(entity_id)
        return payload

    def history(self, kind, entity_id):
        rows = self._conn.execute(
            "SELECT seq, event_type, payload, writer, hash FROM events "
            "WHERE kind = ? AND entity_id = ? ORDER BY seq", (kind, entity_id)).fetchall()
        return [{"seq": r[0], "event_type": r[1], "payload": json.loads(r[2]),
                 "writer": r[3], "hash": r[4]} for r in rows]

    def list(self, kind):
        """按实体 ID 返回一种实体的只读快照，用于控制器构建可恢复上下文。"""
        if kind not in AUTHORITY:
            raise ProtocolError(f"未知实体类型: {kind}")
        rows = self._conn.execute(
            "SELECT entity_id FROM snapshots WHERE kind = ? ORDER BY updated_seq, entity_id",
            (kind,)).fetchall()
        return [self.get(kind, row[0]) for row in rows]

    def verify(self):
        """校验事件链，并确认快照和 study 版本可由事件精确重放。"""
        previous = "0" * 64
        count = 0
        expected_snapshots = {}
        expected_versions = {}

        def fail(reason):
            return {"ok": False, "events": count, "reason": reason}

        def study_for(key):
            snapshot = expected_snapshots.get(key)
            return snapshot["payload"].get("study_id") if snapshot else None

        for row in self._conn.execute("SELECT * FROM events ORDER BY seq"):
            kind, entity_id, event_type, payload, writer, prev, event_hash = row[1:]
            authority = AUTHORITY.get(kind)
            if writer != authority and RETIRED_WRITERS.get((kind, writer)) != authority:
                return fail(
                    f"事件写入方与权威不一致 @seq={row[0]}: {kind} 只能由 "
                    f"{authority} 写入（记录为 {writer}）")
            if prev != previous:
                return fail(f"事件链断裂 @seq={row[0]}")
            decoded = json.loads(payload)
            expected = digest({"kind": kind, "entity_id": entity_id,
                               "event_type": event_type,
                               "payload": decoded,
                               "writer": writer, "previous": previous})
            if expected != event_hash:
                return fail(f"事件哈希不匹配 @seq={row[0]}")

            key = (kind, entity_id)
            current = expected_snapshots.get(key)
            if event_type in {"created", "registered", "revised"}:
                statuses = {
                    ("study", "created"): StudyStatus.ACTIVE.value,
                    ("hypothesis", "created"): HypothesisStatus.UNTESTED.value,
                    ("hypothesis", "revised"): HypothesisStatus.UNTESTED.value,
                    ("design", "created"): DesignStatus.DRAFT.value,
                    ("run", "registered"): RunStatus.QUEUED.value,
                    ("observation", "created"): "computed",
                    ("decision", "created"): "recorded",
                    ("reflection", "created"): "recorded",
                }
                status = statuses.get((kind, event_type))
                if status is None:
                    return fail(f"未知创建事件 {kind}.{event_type} @seq={row[0]}")
                snapshot_payload = dict(decoded)
                snapshot_payload.pop("status", None)
                expected_snapshots[key] = {
                    "status": status, "payload": snapshot_payload,
                    "writer": writer, "updated_seq": row[0],
                }
            elif event_type in {"status", "frozen"}:
                if current is None:
                    return fail(f"状态事件缺少前序实体 {kind}:{entity_id} @seq={row[0]}")
                target = decoded.get("to")
                if not target:
                    return fail(f"状态事件缺少目标状态 @seq={row[0]}")
                expected_snapshots[key] = {
                    "status": target, "payload": current["payload"],
                    "writer": writer, "updated_seq": row[0],
                }
            else:
                return fail(f"未知事件类型 {event_type} @seq={row[0]}")

            if kind == "study" and event_type == "created":
                if entity_id in expected_versions:
                    return fail(f"重复创建 study: {entity_id} @seq={row[0]}")
                expected_versions[entity_id] = 1
            else:
                study_id = entity_id if kind == "study" else study_for(key)
                if not study_id or study_id not in expected_versions:
                    return fail(f"事件无法绑定已创建 study @seq={row[0]}")
                expected_versions[study_id] += 1

            previous = event_hash
            count += 1

        actual_snapshots = {}
        for kind, entity_id, status, payload, writer, updated_seq in self._conn.execute(
                "SELECT kind, entity_id, status, payload, writer, updated_seq FROM snapshots"):
            actual_snapshots[(kind, entity_id)] = {
                "status": status, "payload": json.loads(payload),
                "writer": writer, "updated_seq": updated_seq,
            }
        if actual_snapshots != expected_snapshots:
            differing = sorted(set(actual_snapshots) ^ set(expected_snapshots))
            if not differing:
                differing = sorted(
                    key for key in actual_snapshots
                    if actual_snapshots[key] != expected_snapshots[key])
            return fail(f"快照与事件重放不一致: {differing[:3]}")

        actual_versions = dict(self._conn.execute(
            "SELECT study_id, version FROM study_versions"))
        if actual_versions != expected_versions:
            return fail("study 状态版本与事件重放不一致")
        for key, snapshot in expected_snapshots.items():
            if key[0] != "observation" or not snapshot["payload"].get("artifact_id"):
                continue
            payload = snapshot["payload"]
            path = Path(payload["artifact_id"])
            if not path.is_file() or file_hash(path) != payload.get("artifact_sha256"):
                return fail(f"Observation 评估制品摘要变化: {key[1]}")
            try:
                selected = read_json(path)
                for part in payload["selector"].split("."):
                    selected = selected[int(part)] if isinstance(selected, list) else selected[part]
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                return fail(f"Observation selector 无法重放: {key[1]}")
            if str(selected) != str(payload["value"]):
                return fail(f"Observation 评估值无法重放: {key[1]}")
        return {"ok": True, "events": count, "reason": "事件链、快照和状态版本一致"}

    def budget(self, study_id):
        """返回账本：与研究库同连接，因此可与状态写入共享同一事务。"""
        if self.get("study", study_id) is None:
            raise ProtocolError(f"study 不存在: {study_id}")
        return BudgetLedger(self._conn)

    def reconcile_budget(self, study_id):
        """启动对账：回收开发集 Run 的悬空预留。

        开发集 Run 一旦进入终态失败或取消，它的预留不可能再被消费；按当前
        全表快照求差集回收，而不是依赖崩溃前那次补偿一定跑完。
        """
        study = self.get("study", study_id)
        if study is None:
            raise ProtocolError(f"study 不存在: {study_id}")
        terminal_failed = {RunStatus.CANCELLED.value, RunStatus.INFRA_FAILED.value,
                           RunStatus.IMPLEMENTATION_FAILED.value}
        run_status = {row[0]: row[1] for row in self._conn.execute(
            "SELECT entity_id, status FROM snapshots WHERE kind = 'run'")}
        reclaimed = []
        ledger = self.budget(study_id)
        with self.transaction():
            for item in ledger.outstanding(study["family_id"]):
                if item["kind"] != "dev":
                    continue
                if run_status.get(item["item"]) in terminal_failed:
                    ledger.reclaim(item["reservation_id"])
                    reclaimed.append(item["item"])
        return reclaimed


def _hypothesis_payload(h):
    """Hypothesis 快照载荷（含版本信息由调用方补 version 字段）。"""
    return {"hypothesis_id": h.hypothesis_id, "study_id": h.study_id,
            "parent_version": h.parent_version, "mechanism": h.mechanism,
            "applicability": h.applicability, "predictions": list(h.predictions),
            "falsification": list(h.falsification), "alternatives": list(h.alternatives)}


def _design_payload(d):
    """ExperimentDesign 快照载荷。"""
    return {"design_id": d.design_id, "hypothesis_id": d.hypothesis_id,
            "interventions": list(d.interventions), "control": d.control,
            "baseline": d.baseline, "metric": d.metric,
            "scorer_id": d.scorer_id,
            "metric_direction": d.metric_direction, "analysis_unit": d.analysis_unit,
            "splits": list(d.splits), "seeds": list(d.seeds),
            "min_meaningful_effect": d.min_meaningful_effect}

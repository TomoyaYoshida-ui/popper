"""R0 契约/存储/预算测试：非法状态转移、权威写入方、预算预留与不可重置。"""
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from popper.core import ProtocolError, digest
from popper.research import (BudgetLedger, Decision, DesignStatus,
                             ExperimentDesign, Hypothesis, HypothesisStatus,
                             Observation, ResearchStore, RunStatus, Study,
                             StudyStatus, TransitionError)
from popper.research.schema import migrate_budget
from popper.research.store import AUTHORITY, ObservationWriter, RunWriter


EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "quadratic"


def _study(study_id="S1", family_id="F1", cap=100.0, **kw):
    return Study(study_id=study_id, family_id=family_id, question="q", scope="s",
                 budget_cap=cap, **kw)


def _hypothesis(hid="H1", version=1, parent=None, **kw):
    return Hypothesis(hypothesis_id=hid, study_id="S1", version=version,
                      parent_version=parent, mechanism="m", applicability="a",
                      predictions=("p1",), falsification=("f1",), alternatives=("a1",),
                      **kw)


def _design(did="D1", status=DesignStatus.DRAFT, **kw):
    return ExperimentDesign(design_id=did, hypothesis_id="H1",
                            interventions=("x",), control="c", baseline="b",
                            metric="mse", scorer_id="mse-v1", metric_direction="min",
                            analysis_unit="seed",
                            splits=("dev",), seeds=(11, 29), min_meaningful_effect=0.05,
                            status=status, **kw)


class ResearchStoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.store = ResearchStore(self.dir / "research.sqlite")

    def tearDown(self):
        self.store.close()
        self._tmp.cleanup()


class StudyContractTests(ResearchStoreTestCase):
    def test_study_requires_budget_cap(self):
        with self.assertRaisesRegex(ProtocolError, "预算硬上限"):
            Study(study_id="S", family_id="F", question="q", scope="s")

    def test_study_create_and_terminal_transition(self):
        self.store.create_study(_study())
        self.assertEqual("active", self.store.get("study", "S1")["status"])
        self.store.set_study_status("S1", StudyStatus.CONCLUDED)
        self.assertEqual("concluded", self.store.get("study", "S1")["status"])
        # 终态不能再转移
        with self.assertRaises(TransitionError):
            self.store.set_study_status("S1", StudyStatus.BUDGET_EXHAUSTED)

    def test_study_same_status_is_illegal(self):
        self.store.create_study(_study())
        with self.assertRaises(TransitionError):
            self.store.set_study_status("S1", StudyStatus.ACTIVE)

    def test_study_terminal_blocks_new_actions(self):
        self.store.create_study(_study())
        self.store.set_study_status("S1", StudyStatus.INTEGRITY_FAILED)
        with self.assertRaises(TransitionError):
            self.store.add_hypothesis(_hypothesis())
        with self.assertRaises(TransitionError):
            self.store.add_decision(Decision(decision_id="D1", study_id="S1",
                                             state_version=1, action="stop"))

    def test_duplicate_study_rejected(self):
        self.store.create_study(_study())
        with self.assertRaisesRegex(ProtocolError, "已存在"):
            self.store.create_study(_study())


class HypothesisContractTests(ResearchStoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.create_study(_study())

    def test_append_only_revision(self):
        self.store.add_hypothesis(_hypothesis())
        with self.assertRaises(TransitionError):
            self.store.revise_hypothesis(_hypothesis(version=3, parent=2))
        self.store.revise_hypothesis(_hypothesis(version=2, parent=1))
        got = self.store.get("hypothesis", "H1")
        self.assertEqual(2, got["version"])

    def test_revision_must_point_to_current_version(self):
        self.store.add_hypothesis(_hypothesis())
        with self.assertRaises(TransitionError):
            self.store.revise_hypothesis(_hypothesis(version=2, parent=0))

    def test_new_hypothesis_must_be_v1_without_parent(self):
        with self.assertRaises(TransitionError):
            self.store.add_hypothesis(_hypothesis(version=2))
        with self.assertRaises(TransitionError):
            self.store.add_hypothesis(_hypothesis(version=2, parent=1))
        with self.assertRaisesRegex(TransitionError, "untested"):
            self.store.add_hypothesis(_hypothesis(status=HypothesisStatus.SUPPORTED_IN_SCOPE))

    def test_hypothesis_status_transitions(self):
        self.store.add_hypothesis(_hypothesis())
        self.store.set_hypothesis_status("H1", 1, HypothesisStatus.SUPPORTED_IN_SCOPE)
        self.assertEqual("supported_in_scope",
                         self.store.get("hypothesis", "H1")["status"])
        # 重复置同一状态非法
        with self.assertRaises(TransitionError):
            self.store.set_hypothesis_status("H1", 1, HypothesisStatus.SUPPORTED_IN_SCOPE)
        self.store.set_hypothesis_status("H1", 1, HypothesisStatus.RETIRED)
        with self.assertRaises(TransitionError):
            self.store.set_hypothesis_status("H1", 1, HypothesisStatus.INCONCLUSIVE)

    def test_status_update_must_target_current_version(self):
        self.store.add_hypothesis(_hypothesis())
        with self.assertRaises(TransitionError):
            self.store.set_hypothesis_status("H1", 2, HypothesisStatus.RETIRED)

    def test_retired_hypothesis_cannot_be_revised(self):
        self.store.add_hypothesis(_hypothesis())
        self.store.set_hypothesis_status("H1", 1, HypothesisStatus.RETIRED)
        with self.assertRaises(TransitionError):
            self.store.revise_hypothesis(_hypothesis(version=2, parent=1))


class DesignContractTests(ResearchStoreTestCase):
    def setUp(self):
        super().setUp()
        self.store.create_study(_study())
        self.store.add_hypothesis(_hypothesis())

    def test_design_must_be_created_draft(self):
        with self.assertRaises(TransitionError):
            self.store.create_design(_design(status=DesignStatus.FROZEN))

    def test_freeze_once_only(self):
        self.store.create_design(_design())
        self.store.freeze_design("D1")
        self.assertEqual("frozen", self.store.get("design", "D1")["status"])
        with self.assertRaises(TransitionError):
            self.store.freeze_design("D1")

    def test_run_requires_frozen_design(self):
        self.store.create_design(_design())
        with self.assertRaises(TransitionError):
            self.store.runs.register("R1", "D1")

    def test_run_state_machine(self):
        self.store.create_design(_design())
        self.store.freeze_design("D1")
        self.store.runs.register("R1", "D1")
        with self.assertRaises(TransitionError):
            self.store.runs.set_status("R1", RunStatus.SUCCEEDED)  # 跳过 running
        self.store.runs.set_status("R1", RunStatus.RUNNING)
        self.store.runs.set_status("R1", RunStatus.SUCCEEDED)
        with self.assertRaises(TransitionError):
            self.store.runs.set_status("R1", RunStatus.RUNNING)  # 终态回退

    def test_study_cannot_conclude_with_active_run(self):
        self.store.create_design(_design())
        self.store.freeze_design("D1")
        self.store.runs.register("R1", "D1")
        with self.assertRaisesRegex(TransitionError, "未终结 Run"):
            self.store.set_study_status("S1", StudyStatus.CONCLUDED)
        self.store.set_study_status("S1", StudyStatus.INTEGRITY_FAILED)
        self.store.runs.set_status("R1", RunStatus.CANCELLED)


class AuthorityAndLedgerTests(ResearchStoreTestCase):
    def _successful_run(self, study_id="S1", hypothesis_id="H1",
                        design_id="D1", run_id="R1"):
        self.store.add_hypothesis(_hypothesis(hid=hypothesis_id))
        self.store.create_design(_design(did=design_id))
        self.store.freeze_design(design_id)
        self.store.runs.register(run_id, design_id)
        self.store.runs.set_status(run_id, RunStatus.RUNNING)
        self.store.runs.set_status(run_id, RunStatus.SUCCEEDED)

    def test_authority_is_a_capability_not_a_reported_string(self):
        # A1：写入方不再是可自报的字符串参数，而是只能由 ResearchStore 签发的
        # 窄接口对象。外部无法构造能力，也就无法冒充评估/实验服务。
        self.store.create_study(_study())
        self._successful_run()
        obs = Observation(observation_id="O1", run_id="R1", scorer_id="mse-v1",
                          value=0.5, unit="mse")
        self.store.observations.add(obs)
        self.assertIsNotNone(self.store.get("observation", "O1"))
        # 公开签名里不再存在 writer 参数。
        with self.assertRaises(TypeError):
            self.store.observations.add(obs, writer="research_store")
        # 私有令牌不可伪造：任何外部对象都不是签发令牌。
        with self.assertRaisesRegex(ProtocolError, "只能由 ResearchStore 签发"):
            ObservationWriter(self.store, object())
        with self.assertRaisesRegex(ProtocolError, "只能由 ResearchStore 签发"):
            RunWriter(self.store, object())
        # 旧的字符串权威入口必须彻底消失。
        for name in ("add_observation", "register_run", "set_run_status"):
            self.assertFalse(hasattr(self.store, name), name)

    def test_decision_requires_existing_observations_and_active_study(self):
        self.store.create_study(_study())
        with self.assertRaisesRegex(ProtocolError, "不存在的观察"):
            self.store.add_decision(Decision(decision_id="D1", study_id="S1",
                                             state_version=1, action="run",
                                             observation_refs=("O1",)))

    def test_decision_duplicate_is_idempotent(self):
        self.store.create_study(_study())
        decision = Decision(decision_id="D1", study_id="S1", state_version=1,
                            action="stop", rationale="证据不足")
        self.store.add_decision(decision)
        self.assertEqual("duplicate",
                         self.store.add_decision(decision)["status"])
        with self.assertRaisesRegex(ProtocolError, "不同内容"):
            self.store.add_decision(Decision(decision_id="D1", study_id="S1",
                                             state_version=1, action="confirm"))

    def test_lineage_rejects_cross_study_or_orphan_entities(self):
        self.store.create_study(_study())
        with self.assertRaisesRegex(ProtocolError, "hypothesis 不存在"):
            self.store.create_design(_design())
        self.store.add_hypothesis(_hypothesis())
        self.store.create_study(_study(study_id="S2", family_id="F2"))
        with self.assertRaisesRegex(TransitionError, "不能迁移"):
            self.store.revise_hypothesis(Hypothesis(
                hypothesis_id="H1", study_id="S2", version=2, parent_version=1,
                mechanism="m2", applicability="a"))
        with self.assertRaisesRegex(ProtocolError, "run 不存在"):
            self.store.observations.add(Observation("O1", "MISSING", "mse-v1", 0.5))

    def test_observation_requires_successful_run_and_frozen_scorer(self):
        self.store.create_study(_study())
        self.store.add_hypothesis(_hypothesis())
        self.store.create_design(_design())
        self.store.freeze_design("D1")
        self.store.runs.register("R1", "D1")
        with self.assertRaisesRegex(TransitionError, "成功完成"):
            self.store.observations.add(Observation("O1", "R1", "mse-v1", 0.5))
        self.store.runs.set_status("R1", RunStatus.RUNNING)
        self.store.runs.set_status("R1", RunStatus.SUCCEEDED)
        with self.assertRaisesRegex(ProtocolError, "scorer"):
            self.store.observations.add(Observation("O1", "R1", "other-v1", 0.5))

    def test_run_authority_names_a_real_writer(self):
        # A1 出口条件：AUTHORITY 不再指向不存在的模块。run 的权威写入方是真实
        # 持有 RunWriter 能力并落盘的编排内核 controller.py。
        self.assertEqual("research_controller", AUTHORITY["run"])
        self.store.create_study(_study())
        self._successful_run()
        self.assertEqual(
            {"research_controller"},
            {event["writer"] for event in self.store.history("run", "R1")})

    def test_retired_run_writer_name_stays_verifiable_for_archives(self):
        # 既有归档记录的是 experiment_service（该模块从未存在）。归档是不可改写的
        # 审计证据，所以 verify() 仍接受登记在案的退役名。
        self.store.create_study(_study())
        self._successful_run()
        self._rewrite_run_writer("experiment_service")
        self.assertTrue(self.store.verify()["ok"])
        # 但退役名不是通行证：未登记的写入方仍被拒绝。
        self._rewrite_run_writer("some_model")
        check = self.store.verify()
        self.assertFalse(check["ok"])
        self.assertIn("权威不一致", check["reason"])

    def _rewrite_run_writer(self, writer):
        """把 run 事件与快照改写成指定写入方，并重算事件链（模拟既有归档）。"""
        conn = self.store._unsafe_conn
        previous = "0" * 64
        seq = 0
        for seq, kind, entity_id, event_type, payload, recorded in conn.execute(
                "SELECT seq, kind, entity_id, event_type, payload, writer FROM events "
                "ORDER BY seq").fetchall():
            name = writer if kind == "run" else recorded
            event_hash = digest({"kind": kind, "entity_id": entity_id,
                                 "event_type": event_type, "payload": json.loads(payload),
                                 "writer": name, "previous": previous})
            conn.execute("UPDATE events SET writer = ?, previous_hash = ?, hash = ? "
                         "WHERE seq = ?", (name, previous, event_hash, seq))
            previous = event_hash
        conn.execute("UPDATE snapshots SET writer = ? WHERE kind = 'run'", (writer,))
        conn.execute("INSERT OR REPLACE INTO store_meta(key, value) VALUES('chain_head', ?)",
                     (json.dumps([seq, previous]),))
        conn.commit()

    def test_decision_requires_current_study_version(self):
        self.store.create_study(_study())
        with self.assertRaisesRegex(TransitionError, "study 状态"):
            self.store.add_decision(Decision("D1", "S1", 999, action="stop"))

    def test_event_chain_integrity_and_tamper_detection(self):
        self.store.create_study(_study())
        self.store.add_hypothesis(_hypothesis())
        self.assertTrue(self.store.verify()["ok"])
        # 篡改事件载荷必须被校验捕获
        with self.store._unsafe_conn:
            self.store._unsafe_conn.execute(
                "UPDATE events SET payload = ? WHERE seq = 1",
                ('{"tampered": true}',))
        check = self.store.verify()
        self.assertFalse(check["ok"])

    def test_snapshot_tamper_is_detected_by_verify(self):
        self.store.create_study(_study())
        self.store.add_hypothesis(_hypothesis())
        with self.store._unsafe_conn:
            self.store._unsafe_conn.execute(
                "UPDATE snapshots SET status = ? "
                "WHERE kind = 'hypothesis' AND entity_id = 'H1'",
                (HypothesisStatus.SUPPORTED_IN_SCOPE.value,))
        check = self.store.verify()
        self.assertFalse(check["ok"])
        self.assertIn("快照与事件重放不一致", check["reason"])

    def test_write_path_checks_chain_head_in_constant_time(self):
        # A4：写路径只比对 O(1) 链头，不再全量重放；但事件表被外部删改会被立即拒绝。
        self.store.create_study(_study())
        self.store.add_hypothesis(_hypothesis())
        with self.store._unsafe_conn:
            self.store._unsafe_conn.execute(
                "DELETE FROM events WHERE seq = (SELECT MAX(seq) FROM events)")
        with self.assertRaisesRegex(ProtocolError, "链头校验失败"):
            self.store.create_design(_design())
        # 审计通道仍能全量重放并定位不一致。
        self.assertFalse(self.store.verify()["ok"])

    def test_study_version_tamper_is_detected(self):
        self.store.create_study(_study())
        with self.store._unsafe_conn:
            self.store._unsafe_conn.execute(
                "UPDATE study_versions SET version = 999 WHERE study_id = 'S1'")
        check = self.store.verify()
        self.assertFalse(check["ok"])
        self.assertIn("状态版本", check["reason"])

    def test_reopen_preserves_state_and_chain(self):
        # 崩溃恢复：关闭后重开，快照与事件链完整、幂等可续。
        self.store.create_study(_study())
        self.store.add_hypothesis(_hypothesis())
        version = self.store.get("study", "S1")["state_version"]
        self.store.add_decision(Decision(decision_id="D1", study_id="S1",
                                         state_version=version, action="stop"))
        self.store.close()
        reopened = ResearchStore(self.dir / "research.sqlite")
        self.assertEqual("active", reopened.get("study", "S1")["status"])
        self.assertEqual(1, reopened.get("hypothesis", "H1")["version"])
        self.assertEqual("stop", reopened.get("decision", "D1")["action"])
        self.assertTrue(reopened.verify()["ok"])
        self.assertEqual(3, reopened.verify()["events"])
        reopened.close()

    def test_data_exposure_recorded_in_events(self):
        self.store.create_study(_study(data_exposure=("train.json", "dev.json")))
        event = self.store.history("study", "S1")[0]
        self.assertEqual(["train.json", "dev.json"], event["payload"]["data_exposure"])

    def test_budget_shares_the_research_database(self):
        # A5：预算与研究状态同库同连接，不再有独立 ledger 文件，也就没有跨库补偿。
        self.store.create_study(_study(cap=10.0))
        self.store.budget("S1").reserve("F1", "exp-1", 6.0)
        tables = {row[0] for row in self.store._unsafe_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("families", tables)
        self.assertIn("reservations", tables)
        self.assertFalse((self.dir / "budget.sqlite").exists())
        self.assertEqual(4.0, self.store.budget("S1").balance("F1")["available"])

    def test_budget_ledger_survives_reopen_without_reset(self):
        self.store.create_study(_study(cap=10.0))
        self.store.budget("S1").reserve("F1", "exp-1", 6.0)
        self.store.close()
        reopened = ResearchStore(self.dir / "research.sqlite")
        self.assertEqual(4.0, reopened.budget("S1").balance("F1")["available"])
        reopened.close()

    def test_reconcile_budget_reclaims_only_dangling_dev_reservations(self):
        # A5：开发集 Run 进入终态失败后，其悬空预留在启动对账时可被回收；
        # 非 dev 用途的预留不在回收范围内。
        self.store.create_study(_study(cap=10.0))
        ledger = self.store.budget("S1")
        ledger.reserve("F1", "R-dev", 1.0, kind="dev")
        ledger.reserve("F1", "EXT-1", 2.0, kind="external")
        self.store.add_hypothesis(_hypothesis())
        self.store.create_design(_design())
        self.store.freeze_design("D1")
        self.store.runs.register("R-dev", "D1")
        self.store.runs.set_status("R-dev", RunStatus.CANCELLED)
        self.assertEqual(["R-dev"], self.store.reconcile_budget("S1"))
        balance = ledger.balance("F1")
        # dev 预留已回收，external 预留仍在：reserved = 2.0，available = 10 - 2。
        self.assertEqual(2.0, balance["reserved"])
        self.assertEqual(8.0, balance["available"])


class BudgetLedgerTests(unittest.TestCase):
    def setUp(self):
        # A5：账本不拥有连接，由调用方提供（与研究状态同连接即为同库同事务）。
        self._tmp = tempfile.TemporaryDirectory()
        self._conn = sqlite3.connect(str(Path(self._tmp.name) / "budget.sqlite"))
        migrate_budget(self._conn)
        self.ledger = BudgetLedger(self._conn)
        self.ledger.open_family("F1", 10.0)

    def tearDown(self):
        self.ledger.close()
        self._conn.close()
        self._tmp.cleanup()

    def test_reserve_within_cap_and_overdraw_rejected(self):
        res = self.ledger.reserve("F1", "exp-1", 6.0)
        self.assertEqual(4.0, self.ledger.balance("F1")["available"])
        with self.assertRaisesRegex(ProtocolError, "预算不足"):
            self.ledger.reserve("F1", "exp-2", 5.0)

    def test_settle_refunds_unused_and_rejects_double(self):
        res = self.ledger.reserve("F1", "exp-1", 6.0)
        out = self.ledger.settle(res["reservation_id"], spent=2.0)
        self.assertEqual(4.0, out["refund"])
        # 已花费仍占用硬上限，只有未使用的预留退回。
        self.assertEqual(8.0, self.ledger.balance("F1")["available"])
        self.assertEqual(2.0, self.ledger.balance("F1")["spent"])
        with self.assertRaisesRegex(ProtocolError, "已结算"):
            self.ledger.settle(res["reservation_id"], spent=1.0)

    def test_reservation_and_same_settlement_are_idempotent(self):
        first = self.ledger.reserve("F1", "same-action", 2.0)
        second = self.ledger.reserve("F1", "same-action", 2.0)
        self.assertEqual(first["reservation_id"], second["reservation_id"])
        self.assertTrue(second["duplicate"])
        settled = self.ledger.settle(first["reservation_id"], 1.0)
        repeated = self.ledger.settle(first["reservation_id"], 1.0)
        self.assertFalse(settled["duplicate"])
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(1.0, self.ledger.balance("F1")["spent"])

    def test_spent_plus_reserved_cannot_exceed_cap(self):
        res = self.ledger.reserve("F1", "exp-1", 6.0)
        self.ledger.settle(res["reservation_id"], spent=2.0)
        with self.assertRaisesRegex(ProtocolError, "预算不足"):
            self.ledger.reserve("F1", "exp-2", 9.0)

    def test_family_cap_mismatch_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "不能改为"):
            self.ledger.open_family("F1", 100.0)

    def test_negative_amounts_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "不能为负"):
            self.ledger.reserve("F1", "x", -1.0)
        res = self.ledger.reserve("F1", "x", 1.0)
        with self.assertRaisesRegex(ProtocolError, "不能为负"):
            self.ledger.settle(res["reservation_id"], -0.5)

    def test_unknown_family_rejected(self):
        with self.assertRaisesRegex(ProtocolError, "未登记"):
            self.ledger.reserve("NOPE", "x", 1.0)


class SchemaMigrationTests(unittest.TestCase):
    """A6：各物理库共用同一迁移入口，且版本可查询。"""

    def test_every_store_reports_the_same_schema_version(self):
        import runpy
        from popper.core import Experiment, initialize
        from popper.research.confirmation_service import HoldoutService
        from popper.research.schema import SCHEMA_VERSION, schema_version

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            for name in ("experiment.json", "model.py"):
                shutil.copyfile(EXAMPLE / name, project / name)
            runpy.run_path(str(EXAMPLE / "generate_data.py"))["generate"](project)
            initialize(project)

            exp = Experiment(project)
            try:
                self.assertEqual(SCHEMA_VERSION, schema_version(exp.db))
            finally:
                exp.close()

            store = ResearchStore(root / "research" / "research.sqlite")
            try:
                # 预算总账与研究状态同库，因此只有一个版本号。
                self.assertEqual(SCHEMA_VERSION, schema_version(store._unsafe_conn))
            finally:
                store.close()

            holdout = HoldoutService(root / "holdout")
            try:
                self.assertEqual(SCHEMA_VERSION, schema_version(holdout._db))
            finally:
                holdout.close()


class PackageImportsTests(unittest.TestCase):
    def test_public_api_exports(self):
        import popper.research as research
        for name in ("Study", "Hypothesis", "ExperimentDesign", "Observation",
                     "Decision", "ResearchStore", "BudgetLedger", "TransitionError"):
            self.assertTrue(hasattr(research, name), name)


if __name__ == "__main__":
    unittest.main()

import tempfile
import sqlite3
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.core import ProtocolError
from popper.sessions import AgentSessionStore, SESSION_SCHEMA_VERSION
from popper.storage import SQLiteConnection


class AgentSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AgentSessionStore(self.root)
        self.session = self.store.default_session(create=True, model="test-model")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_turn_is_durable_idempotent_and_builds_model_history(self):
        turn, replayed = self.store.start_turn(
            self.session["session_id"], "研究问题", "request-0001",
            actor="test-user", trace_id="trc_turn0001")
        self.assertFalse(replayed)
        self.assertEqual("running", turn["status"])
        self.assertEqual([{"role": "user", "content": "研究问题"}],
                         self.store.model_messages(
                             self.session["session_id"], turn["turn_id"]))
        completed = self.store.complete_turn(turn["turn_id"], "研究回答")
        self.assertEqual("completed", completed["status"])

        replay, replayed = self.store.start_turn(
            self.session["session_id"], "研究问题", "request-0001",
            actor="test-user", trace_id="trc_retry001")
        self.assertTrue(replayed)
        self.assertEqual(completed, replay)
        self.assertEqual(1, len(self.store.turns(self.session["session_id"])))
        self.assertEqual(
            [{"role": "user", "content": "研究问题"},
             {"role": "assistant", "content": "研究回答"}],
            self.store.model_messages(self.session["session_id"]))

    def test_same_key_cannot_change_turn(self):
        self.store.start_turn(self.session["session_id"], "A", "request-0001",
                              actor="test-user", trace_id="trc_turn0001")
        with self.assertRaisesRegex(ProtocolError, "不同 turn"):
            self.store.start_turn(self.session["session_id"], "B", "request-0001",
                                  actor="test-user", trace_id="trc_turn0002")

    def test_restart_marks_inflight_turn_interrupted(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "执行中", "request-0001",
            actor="test-user", trace_id="trc_turn0001")
        self.store.close()
        reopened = AgentSessionStore(self.root)
        self.store = reopened
        self.assertEqual({"turns": 1, "tool_calls": 0}, reopened.recover_interrupted())
        recovered = reopened.get_turn(turn["turn_id"])
        self.assertEqual("interrupted", recovered["status"])
        self.assertEqual("Interrupted", recovered["error"]["type"])
        messages = reopened.chat_messages(self.session["session_id"])
        self.assertEqual(["user", "assistant"], [m["role"] for m in messages])
        self.assertIn("error", messages[1])

    def test_failure_is_terminal_and_not_sent_back_to_model(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "失败", "request-0001",
            actor="test-user", trace_id="trc_turn0001")
        self.store.fail_turn(turn["turn_id"], {"type": "Timeout", "message": "超时"})
        self.assertEqual([], self.store.model_messages(self.session["session_id"]))
        with self.assertRaisesRegex(ProtocolError, "终态"):
            self.store.complete_turn(turn["turn_id"], "迟到回复")

    def test_sessions_are_isolated(self):
        other = self.store.create_session("另一个会话", model="test-model")
        turn, _ = self.store.start_turn(
            self.session["session_id"], "仅默认会话", "request-0001",
            actor="test-user", trace_id="trc_turn0001")
        self.store.complete_turn(turn["turn_id"], "回复")
        self.assertEqual([], self.store.turns(other["session_id"]))
        self.assertEqual(2, len(self.store.list_sessions()))

    def test_legacy_session_harness_can_be_bound_once(self):
        bound = self.store.bind_legacy_chat_harness(
            self.session["session_id"], "openai_compatible", "1.0", "model-a")
        self.assertEqual("openai_compatible", bound["harness"])
        self.assertEqual("1.0", bound["harness_version"])
        with self.assertRaisesRegex(ProtocolError, "legacy"):
            self.store.bind_legacy_chat_harness(
                self.session["session_id"], "other", "1.0", "model-b")

    def test_schema_v1_turns_gain_provenance_columns_without_data_loss(self):
        self.store.close()
        db = self.root / ".popper" / "sessions.sqlite"
        db.unlink()
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE session_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sessions(
                session_id TEXT PRIMARY KEY, title TEXT NOT NULL, harness TEXT NOT NULL,
                model TEXT, status TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL);
            CREATE TABLE turns(
                turn_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE, user_message TEXT NOT NULL,
                status TEXT NOT NULL, assistant_message TEXT, error_json TEXT,
                created TEXT NOT NULL, updated TEXT NOT NULL);
            PRAGMA user_version = 1;
        """)
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
            ("ses_aaaaaaaaaaaaaaaaaaaaaaaaaa", "旧会话", "chat", None, "active", "t", "t"))
        conn.execute(
            "INSERT INTO turns VALUES(?,?,?,?,?,?,?,?,?)",
            ("trn_aaaaaaaaaaaaaaaaaaaaaaaaaa", "ses_aaaaaaaaaaaaaaaaaaaaaaaaaa",
             "legacy-key", "旧问题", "completed", "旧回答", None, "t", "t"))
        conn.commit()
        conn.close()

        self.store = AgentSessionStore(self.root)
        turn = self.store.get_turn("trn_aaaaaaaaaaaaaaaaaaaaaaaaaa")
        self.assertEqual("旧回答", turn["assistant_message"])
        self.assertEqual("legacy", turn["actor"])
        self.assertEqual("legacy", turn["trace_id"])
        self.assertEqual(SESSION_SCHEMA_VERSION, self.store._conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(0, turn["cancel_requested"])

    def test_schema_v14_tool_risk_migration_preserves_calls(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "旧工具", "request-old-tool",
            actor="test-user", trace_id="trc_oldtoolturn")
        self.store.complete_turn(turn["turn_id"], "旧提议")
        old, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "workspace.task_create",
            {"fid": "f1"}, "write", "request-old-call",
            actor="agent", trace_id="trc_oldtoolcall")
        self.store.close()
        db = self.root / ".popper" / "sessions.sqlite"
        conn = sqlite3.connect(db)
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='tool_calls'").fetchone()[0]
        legacy = schema.replace("CREATE TABLE tool_calls(",
                                "CREATE TABLE tool_calls_v14(", 1).replace(
            "('read','disclose','write','execute')", "('read','write','execute')")
        conn.execute(legacy)
        conn.execute("INSERT INTO tool_calls_v14 SELECT * FROM tool_calls")
        conn.execute("DROP TABLE tool_calls")
        conn.execute("ALTER TABLE tool_calls_v14 RENAME TO tool_calls")
        conn.execute("CREATE INDEX idx_tool_calls_session_created "
                     "ON tool_calls(session_id, created, call_id)")
        conn.execute("PRAGMA user_version=14")
        conn.commit()
        conn.close()

        self.store = AgentSessionStore(self.root)
        self.assertEqual(SESSION_SCHEMA_VERSION, self.store._conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(old, self.store.get_tool_call(old["call_id"]))
        self.assertEqual([], self.store._conn.execute("PRAGMA foreign_key_check").fetchall())
        disclosed, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "research.status",
            {"study_id": "std_test"}, "disclose", "request-disclose-call",
            actor="agent", trace_id="trc_disclosecall")
        self.assertEqual("pending_approval", disclosed["status"])

    def test_schema_v15_tool_calls_remain_readable_without_invented_events(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "旧工具", "request-v15-tool",
            actor="test-user", trace_id="trc_v15toolturn")
        self.store.complete_turn(turn["turn_id"], "建议工具")
        call, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "research.status",
            {"study_id": "std_legacy"}, "read", "request-v15-call",
            actor="agent", trace_id="trc_v15toolcall")
        self.store.close()
        db = self.root / ".popper" / "sessions.sqlite"
        conn = sqlite3.connect(db)
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='tool_calls'").fetchone()[0]
        legacy = schema.replace("CREATE TABLE tool_calls(",
                                "CREATE TABLE tool_calls_v15(", 1).replace(
            "    integrity_version INTEGER NOT NULL DEFAULT 0,\n", "")
        conn.execute(legacy)
        columns = [row[1] for row in conn.execute("PRAGMA table_info(tool_calls)")
                   if row[1] != "integrity_version"]
        names = ",".join(columns)
        conn.execute(f"INSERT INTO tool_calls_v15({names}) SELECT {names} FROM tool_calls")
        conn.execute("DROP TABLE tool_calls")
        conn.execute("ALTER TABLE tool_calls_v15 RENAME TO tool_calls")
        conn.execute("DROP TABLE tool_events")
        conn.execute("PRAGMA user_version=15")
        conn.commit()
        conn.close()
        self.store = AgentSessionStore(self.root)
        self.assertEqual(SESSION_SCHEMA_VERSION, self.store._conn.execute("PRAGMA user_version").fetchone()[0])
        legacy_call = self.store.get_tool_call(call["call_id"])
        self.assertEqual(0, legacy_call["integrity_version"])
        self.assertEqual({"study_id": "std_legacy"}, legacy_call["arguments"])
        self.assertTrue(self.store.verify_tool_events()["ok"])
        self.assertEqual(["user", "assistant"], [item["role"] for item in
                         self.store.model_messages(self.session["session_id"])])

    def test_cancel_turn_is_persistent_idempotent_and_rejects_late_completion(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "耗时问题", "request-cancel-turn",
            actor="test-user", trace_id="trc_cancel001")
        cancelled = self.store.cancel_turn(
            turn["turn_id"], actor="test-user", reason="停止生成")
        self.assertEqual("interrupted", cancelled["status"])
        self.assertEqual(1, cancelled["cancel_requested"])
        self.assertEqual("Cancelled", cancelled["error"]["type"])
        self.assertEqual(cancelled, self.store.cancel_turn(
            turn["turn_id"], actor="test-user", reason="停止生成"))
        with self.assertRaisesRegex(ProtocolError, "终态"):
            self.store.complete_turn(turn["turn_id"], "迟到结果")
        events = self.store.turn_events(self.session["session_id"])["events"]
        self.assertEqual(["turn.started", "turn.cancelled"],
                         [event["kind"] for event in events])
        self.assertEqual("test-user", events[-1]["actor"])
        self.assertEqual("停止生成", events[-1]["payload"]["reason"])

    def test_turn_events_are_ordered_cursor_paginated_and_transactional(self):
        first, _ = self.store.start_turn(
            self.session["session_id"], "问题一", "request-event-one",
            actor="test-user", trace_id="trc_events001")
        self.store.complete_turn(first["turn_id"], "回答一")
        second, _ = self.store.start_turn(
            self.session["session_id"], "问题二", "request-event-two",
            actor="test-user", trace_id="trc_events002")
        self.store.fail_turn(second["turn_id"], {"type": "Timeout", "message": "超时"})
        page = self.store.turn_events(self.session["session_id"], limit=2)
        self.assertEqual(["turn.started", "turn.completed"],
                         [event["kind"] for event in page["events"]])
        self.assertTrue(page["has_more"])
        rest = self.store.turn_events(
            self.session["session_id"], after=page["cursor"], limit=2)
        self.assertEqual(["turn.started", "turn.failed"],
                         [event["kind"] for event in rest["events"]])
        self.assertFalse(rest["has_more"])
        self.assertEqual("Timeout", rest["events"][-1]["payload"]["error_type"])
        self.assertTrue(self.store.verify_turn_events()["ok"])

    def test_turn_event_tampering_is_detected(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "审计问题", "request-event-tamper",
            actor="test-user", trace_id="trc_eventtamper")
        self.store.complete_turn(turn["turn_id"], "回答")
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE turn_events SET payload_json='{}' WHERE kind='turn.completed'")
        check = self.store.verify_turn_events()
        self.assertFalse(check["ok"])
        self.assertIn("事件链", check["reason"])

    def test_legacy_unhashed_turn_events_are_backfilled(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "迁移问题", "request-event-migrate",
            actor="test-user", trace_id="trc_eventmigrate")
        self.store.complete_turn(turn["turn_id"], "迁移回答")
        with self.store._conn:
            self.store._conn.executescript("""
                ALTER TABLE turn_events RENAME TO turn_events_hashed;
                CREATE TABLE turn_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    trace_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created TEXT NOT NULL);
                INSERT INTO turn_events(seq,event_id,session_id,turn_id,kind,actor,
                                        trace_id,payload_json,created)
                SELECT seq,event_id,session_id,turn_id,kind,actor,trace_id,
                       payload_json,created FROM turn_events_hashed;
                DROP TABLE turn_events_hashed;
            """)
        self.store.close()
        self.store = AgentSessionStore(self.root)
        integrity = self.store.verify_turn_events()
        self.assertTrue(integrity["ok"])
        self.assertEqual(2, integrity["events"])

    def test_turn_snapshot_tampering_is_detected_by_replay(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "快照问题", "request-event-snapshot",
            actor="test-user", trace_id="trc_eventsnapshot")
        self.store.complete_turn(turn["turn_id"], "回答")
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE turns SET status='running' WHERE turn_id=?", (turn["turn_id"],))
        check = self.store.verify_turn_events()
        self.assertFalse(check["ok"])
        self.assertIn("快照", check["reason"])

    def test_turn_content_tampering_is_rejected_from_model_history(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "真实问题", "request-content-tamper",
            actor="test-user", trace_id="trc_contenttamper")
        self.store.complete_turn(turn["turn_id"], "真实回答")
        for column, forged in (("user_message", "伪造问题"),
                               ("assistant_message", "伪造回答")):
            with self.subTest(column=column):
                with self.store._conn:
                    self.store._conn.execute(
                        f"UPDATE turns SET {column}=? WHERE turn_id=?",
                        (forged, turn["turn_id"]))
                integrity = self.store.verify_turn_events()
                self.assertFalse(integrity["ok"])
                self.assertIn("内容快照", integrity["reason"])
                with self.assertRaisesRegex(ProtocolError, "内容快照"):
                    self.store.model_messages(self.session["session_id"])
                with self.store._conn:
                    self.store._conn.execute(
                        f"UPDATE turns SET {column}=? WHERE turn_id=?",
                        ("真实问题" if column == "user_message" else "真实回答",
                         turn["turn_id"]))
        self.assertTrue(self.store.verify_turn_events()["ok"])

    def test_turn_original_message_cannot_be_rebound_by_later_event(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "原问题", "request-turn-identity",
            actor="test-user", trace_id="trc_turnidentity")
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE turns SET user_message='伪造问题' WHERE turn_id=?",
                (turn["turn_id"],))
        self.store.complete_turn(turn["turn_id"], "回答")
        integrity = self.store.verify_turn_events()
        self.assertFalse(integrity["ok"])
        self.assertIn("不可变字段", integrity["reason"])

    def test_streamed_assistant_draft_is_durable_and_terminally_fenced(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "流式问题", "request-stream-draft",
            actor="test-user", trace_id="trc_stream001")
        self.assertEqual("研究", self.store.append_turn_delta(turn["turn_id"], "研究"))
        self.assertEqual("研究完成", self.store.append_turn_delta(turn["turn_id"], "完成"))
        self.assertEqual("研究完成", self.store.get_turn(turn["turn_id"])["assistant_draft"])
        self.store.complete_turn(turn["turn_id"], "研究完成")
        with self.assertRaisesRegex(ProtocolError, "终态"):
            self.store.append_turn_delta(turn["turn_id"], "迟到")

    def test_agent_job_queue_claim_lease_and_terminal_reconciliation(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "排队问题", "request-job-queue",
            actor="test-user", trace_id="trc_jobqueue1", enqueue=True)
        job = self.store.agent_job_for_turn(turn["turn_id"])
        self.assertEqual("queued", job["status"])
        self.assertEqual(0, job["attempt"])
        claimed = self.store.claim_agent_job(
            job["job_id"], worker_id="worker-test", lease_seconds=30)
        self.assertEqual("running", claimed["status"])
        self.assertEqual(1, claimed["attempt"])
        self.assertEqual("worker-test", claimed["worker_id"])
        self.assertIsNotNone(claimed["lease_expires"])
        renewed = self.store.renew_agent_job(
            job["job_id"], worker_id="worker-test", lease_seconds=60)
        self.assertGreater(renewed["lease_expires"], claimed["lease_expires"])
        with self.assertRaisesRegex(ProtocolError, "所有权"):
            self.store.renew_agent_job(
                job["job_id"], worker_id="other-worker", lease_seconds=60)
        self.store.complete_turn(turn["turn_id"], "排队回答")
        finished = self.store.finish_agent_job(job["job_id"], usage={
            "prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17})
        self.assertEqual("succeeded", finished["status"])
        self.assertIsNone(finished["lease_expires"])
        self.assertEqual((12, 5, 17), (finished["prompt_tokens"],
                         finished["completion_tokens"], finished["total_tokens"]))
        self.assertEqual({"jobs": 1, "prompt_tokens": 12,
                          "completion_tokens": 5, "total_tokens": 17,
                          "reserved_tokens": 0, "token_budget": None,
                          "remaining_tokens": None, "cost_micros": 0,
                          "reserved_cost_micros": 0, "cost_budget_micros": None,
                          "remaining_cost_micros": None, "currency": None},
                         self.store.usage_summary(self.session["session_id"]))

    def test_recovery_resumes_queued_but_interrupts_running_agent_job(self):
        queued_turn, _ = self.store.start_turn(
            self.session["session_id"], "尚未领取", "request-job-queued",
            actor="test-user", trace_id="trc_jobqueued", enqueue=True)
        queued_job = self.store.agent_job_for_turn(queued_turn["turn_id"])
        self.assertEqual({"turns": 0, "tool_calls": 0}, self.store.recover_interrupted())
        self.assertEqual("running", self.store.get_turn(queued_turn["turn_id"])["status"])
        self.assertEqual("queued", self.store.get_agent_job(queued_job["job_id"])["status"])
        self.store.claim_agent_job(
            queued_job["job_id"], worker_id="crashed-worker", lease_seconds=30)
        self.assertEqual({"turns": 1, "tool_calls": 0}, self.store.recover_interrupted())
        self.assertEqual("interrupted", self.store.get_turn(queued_turn["turn_id"])["status"])
        self.assertEqual("interrupted",
                         self.store.get_agent_job(queued_job["job_id"])["status"])

    def test_retry_creates_new_turn_job_lineage_and_is_idempotent(self):
        source, _ = self.store.start_turn(
            self.session["session_id"], "失败问题", "request-retry-source",
            actor="test-user", trace_id="trc_retrysrc1")
        self.store.fail_turn(source["turn_id"], {
            "type": "ProviderError", "message": "失败"})
        retry, job, replayed = self.store.retry_turn(
            source["turn_id"], "request-retry-child",
            actor="test-user", trace_id="trc_retrynew1")
        self.assertFalse(replayed)
        self.assertEqual(source["turn_id"], retry["parent_turn_id"])
        self.assertEqual(source["user_message"], retry["user_message"])
        self.assertEqual("queued", job["status"])
        again, again_job, replayed = self.store.retry_turn(
            source["turn_id"], "request-retry-child",
            actor="test-user", trace_id="trc_retrynew2")
        self.assertTrue(replayed)
        self.assertEqual(retry["turn_id"], again["turn_id"])
        self.assertEqual(job["job_id"], again_job["job_id"])
        with self.assertRaisesRegex(ProtocolError, "失败或中断"):
            self.store.retry_turn(
                retry["turn_id"], "request-retry-running",
                actor="test-user", trace_id="trc_retryrun1")

    def test_steer_atomically_cancels_source_and_enqueues_child(self):
        source, _ = self.store.start_turn(
            self.session["session_id"], "原方向", "request-steer-source",
            actor="test-user", trace_id="trc_steersrc1", enqueue=True)
        source_job = self.store.agent_job_for_turn(source["turn_id"])
        child, child_job, replayed = self.store.steer_turn(
            source["turn_id"], "新方向", "request-steer-child",
            actor="test-user", trace_id="trc_steernew1")
        self.assertFalse(replayed)
        stopped = self.store.get_turn(source["turn_id"])
        self.assertEqual("interrupted", stopped["status"])
        self.assertEqual("Steered", stopped["error"]["type"])
        self.assertEqual(child["turn_id"], stopped["error"]["child_turn_id"])
        self.assertEqual("cancelled",
                         self.store.get_agent_job(source_job["job_id"])["status"])
        self.assertEqual(source["turn_id"], child["parent_turn_id"])
        self.assertEqual("新方向", child["user_message"])
        self.assertEqual("queued", child_job["status"])
        again, again_job, replayed = self.store.steer_turn(
            source["turn_id"], "新方向", "request-steer-child",
            actor="test-user", trace_id="trc_steernew2")
        self.assertTrue(replayed)
        self.assertEqual(child["turn_id"], again["turn_id"])
        self.assertEqual(child_job["job_id"], again_job["job_id"])

    def test_session_token_budget_atomically_gates_job_reservations(self):
        self.store.set_token_budget(self.session["session_id"], 10_000)
        first, _ = self.store.start_turn(
            self.session["session_id"], "预算内", "request-budget-first",
            actor="test-user", trace_id="trc_budget001", enqueue=True,
            token_reservation=6_000)
        usage = self.store.usage_summary(self.session["session_id"])
        self.assertEqual(6_000, usage["reserved_tokens"])
        self.assertEqual(4_000, usage["remaining_tokens"])
        with self.assertRaisesRegex(ProtocolError, "预算不足"):
            self.store.start_turn(
                self.session["session_id"], "超预算", "request-budget-second",
                actor="test-user", trace_id="trc_budget002", enqueue=True,
                token_reservation=5_000)
        self.assertEqual(1, len(self.store.agent_jobs(self.session["session_id"])))
        self.store.cancel_turn(first["turn_id"], actor="test-user", reason="释放预留")
        second, _ = self.store.start_turn(
            self.session["session_id"], "释放后可入队", "request-budget-second",
            actor="test-user", trace_id="trc_budget003", enqueue=True,
            token_reservation=5_000)
        self.assertEqual("running", second["status"])

    def test_versioned_pricing_catalog_snapshots_job_cost(self):
        prices = [{"model": "deepseek-flash",
                   "prompt_micros_per_million": 2_000_000,
                   "completion_micros_per_million": 4_000_000}]
        catalog = self.store.register_pricing_catalog(
            "contract-2026-09", "usd", prices, actor="admin")
        self.assertTrue(catalog["catalog_id"].startswith("prc_"))
        same = self.store.register_pricing_catalog(
            "contract-2026-09", "USD", prices, actor="another-admin")
        self.assertEqual(catalog["catalog_id"], same["catalog_id"])
        self.store.set_price_catalog(self.session["session_id"], catalog["catalog_id"])
        self.store.set_cost_budget(self.session["session_id"], 4_000)
        turn, _ = self.store.start_turn(
            self.session["session_id"], "计费", "request-priced-job",
            actor="test-user", trace_id="trc_pricing01", enqueue=True,
            token_reservation=1000, model_name="deepseek-flash")
        job = self.store.agent_job_for_turn(turn["turn_id"])
        self.assertEqual(catalog["catalog_id"], job["price_catalog_id"])
        self.store.claim_agent_job(job["job_id"], worker_id="priced-worker")
        self.store.complete_turn(turn["turn_id"], "完成")
        finished = self.store.finish_agent_job(job["job_id"], usage={
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
        self.assertEqual(400, finished["cost_micros"])
        summary = self.store.usage_summary(self.session["session_id"])
        self.assertEqual("USD", summary["currency"])
        self.assertEqual(400, summary["cost_micros"])
        self.assertEqual(0, summary["reserved_cost_micros"])
        self.assertEqual(3_600, summary["remaining_cost_micros"])
        with self.assertRaisesRegex(ProtocolError, "货币预算不足"):
            self.store.start_turn(
                self.session["session_id"], "费用超限", "request-priced-over-budget",
                actor="test-user", trace_id="trc_priceover1", enqueue=True,
                token_reservation=1000, model_name="deepseek-flash")
        other = self.store.register_pricing_catalog(
            "contract-next", "USD", [{"model": "deepseek-flash",
                "prompt_micros_per_million": 1, "completion_micros_per_million": 1}],
            actor="admin")
        with self.assertRaisesRegex(ProtocolError, "不能更换"):
            self.store.set_price_catalog(self.session["session_id"], other["catalog_id"])

    def test_write_tool_requires_approval_and_has_terminal_result(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "创建任务", "request-tool-turn",
            actor="test-user", trace_id="trc_toolturn1")
        self.store.complete_turn(turn["turn_id"], "我建议创建任务")
        call, replayed = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "workspace.task_create",
            {"fid": "f1", "title": "验证假设", "expected_version": 1}, "write",
            "tool-request-0001", actor="agent", trace_id="trc_toolcall1")
        self.assertFalse(replayed)
        self.assertEqual("pending_approval", call["status"])
        with self.assertRaisesRegex(ProtocolError, "未获批准"):
            self.store.claim_tool_call(call["call_id"])
        approved = self.store.decide_tool_call(
            call["call_id"], "approve", actor="test-user", reason="范围明确")
        self.assertEqual("approved", approved["status"])
        self.assertEqual("running", self.store.claim_tool_call(call["call_id"])["status"])
        completed = self.store.finish_tool_call(
            call["call_id"], result={"state_version": 2})
        self.assertEqual("completed", completed["status"])
        self.assertEqual({"state_version": 2}, completed["result"])
        self.assertTrue(self.store.verify_tool_events()["ok"])
        first = self.store.tool_events(self.session["session_id"], limit=2)
        self.assertEqual(["tool.pending_approval", "tool.approved"],
                         [event["kind"] for event in first["events"]])
        self.assertTrue(first["has_more"])
        rest = self.store.tool_events(self.session["session_id"],
                                      after=first["cursor"], limit=2)
        self.assertEqual(["tool.running", "tool.completed"],
                         [event["kind"] for event in rest["events"]])
        self.assertFalse(rest["has_more"])
        self.assertTrue(all(event["call_id"] == call["call_id"] for event in
                            [*first["events"], *rest["events"]]))
        self.assertNotIn("state_version", json.dumps(rest["events"]),
                         "审计事件只披露摘要，不复制工具结果正文")

    def test_tampered_tool_result_and_missing_event_are_rejected_from_model_history(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "读取证据", "request-tool-integrity",
            actor="test-user", trace_id="trc_toolintegrity")
        self.store.complete_turn(turn["turn_id"], "查看登记结果")
        call, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "research.status",
            {"study_id": "std_test"}, "read", "tool-integrity-request",
            actor="agent", trace_id="trc_toolintegrity")
        self.store.claim_tool_call(call["call_id"])
        self.store.finish_tool_call(call["call_id"], result={"status": "verified"})
        self.assertTrue(self.store.verify_tool_events()["ok"])
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE tool_calls SET result_json=? WHERE call_id=?",
                (json.dumps({"status": "forged"}), call["call_id"]))
        self.assertIn("工具快照", self.store.verify_tool_events()["reason"])
        with self.assertRaisesRegex(ProtocolError, "工具快照"):
            self.store.model_messages(self.session["session_id"])
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE tool_calls SET result_json=? WHERE call_id=?",
                (json.dumps({"status": "verified"}, ensure_ascii=False, sort_keys=True),
                 call["call_id"]))
            self.store._conn.execute("DELETE FROM tool_events")
        self.assertIn("工具事件缺失", self.store.verify_tool_events()["reason"])
        with self.assertRaisesRegex(ProtocolError, "工具事件缺失"):
            self.store.model_messages(self.session["session_id"])

    def test_tampered_tool_arguments_cannot_be_claimed_for_execution(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "查看", "request-tool-claim-integrity",
            actor="test-user", trace_id="trc_claimintegrity")
        self.store.complete_turn(turn["turn_id"], "提议只读工具")
        call, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "research.status",
            {"study_id": "std_real"}, "read", "tool-claim-integrity",
            actor="agent", trace_id="trc_claimintegrity")
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE tool_calls SET arguments_json=? WHERE call_id=?",
                (json.dumps({"study_id": "std_forged"}), call["call_id"]))
        with self.assertRaisesRegex(ProtocolError, "工具快照"):
            self.store.claim_tool_call(call["call_id"])
        self.assertEqual("approved", self.store._conn.execute(
            "SELECT status FROM tool_calls WHERE call_id=?",
            (call["call_id"],)).fetchone()[0])

    def test_tool_arguments_cannot_be_rebound_by_later_event(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "查看", "request-tool-rebind",
            actor="test-user", trace_id="trc_toolrebind")
        self.store.complete_turn(turn["turn_id"], "提议工具")
        call, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "research.status",
            {"study_id": "std_real"}, "read", "tool-rebind-request",
            actor="agent", trace_id="trc_toolrebind")
        with self.store._conn:
            self.store._conn.execute(
                "UPDATE tool_calls SET arguments_json=? WHERE call_id=?",
                (json.dumps({"study_id": "std_forged"}), call["call_id"]))
            self.store._conn.execute(
                "UPDATE tool_calls SET status='running' WHERE call_id=?",
                (call["call_id"],))
            self.store._append_tool_event(call["call_id"], actor="system")
        integrity = self.store.verify_tool_events()
        self.assertFalse(integrity["ok"])
        self.assertIn("不可变字段", integrity["reason"])

    def test_tool_continuation_has_no_fake_user_message_and_is_idempotent(self):
        source, _ = self.store.start_turn(
            self.session["session_id"], "检查研究", "request-continue-source",
            actor="test-user", trace_id="trc_contsource")
        self.store.complete_turn(source["turn_id"], "请求查看")
        call, _ = self.store.propose_tool_call(
            self.session["session_id"], source["turn_id"], "research.status",
            {"study_id": "S-test"}, "disclose", "tool-continue-request",
            actor="agent", trace_id="trc_conttool1")
        with self.assertRaisesRegex(ProtocolError, "尚未全部进入终态"):
            self.store.continue_after_tools(source["turn_id"],
                                            trace_id="trc_contbefore")
        self.store.decide_tool_call(call["call_id"], "approve",
                                    actor="test-user", reason="允许披露")
        self.store.claim_tool_call(call["call_id"])
        self.store.finish_tool_call(call["call_id"], result={"status": "active"})
        turn, job, replayed = self.store.continue_after_tools(
            source["turn_id"], trace_id="trc_contafter1", token_reservation=100)
        self.assertFalse(replayed)
        self.assertEqual("agent_continuation", turn["actor"])
        self.assertEqual("", turn["user_message"])
        self.assertEqual(source["turn_id"], turn["parent_turn_id"])
        self.assertEqual("queued", job["status"])
        history = self.store.model_messages(self.session["session_id"], turn["turn_id"])
        self.assertEqual(["user", "assistant", "tool"],
                         [message["role"] for message in history])
        self.assertEqual("active", json.loads(history[2]["content"])["result"]["status"])
        again, same_job, replayed = self.store.continue_after_tools(
            source["turn_id"], trace_id="trc_contafter2", token_reservation=100)
        self.assertTrue(replayed)
        self.assertEqual(turn["turn_id"], again["turn_id"])
        self.assertEqual(job["job_id"], same_job["job_id"])
        self.store.complete_turn(turn["turn_id"], "研究处于 active 状态")
        self.assertEqual(["user", "assistant", "assistant"],
                         [message["role"] for message in
                          self.store.chat_messages(self.session["session_id"])])

    def test_tool_call_rejection_cancel_and_crash_recovery(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "工具", "request-tool-turn",
            actor="test-user", trace_id="trc_toolturn1")
        self.store.complete_turn(turn["turn_id"], "建议")
        rejected, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "workspace.task_create",
            {}, "write", "tool-request-reject", actor="agent", trace_id="trc_toolreject")
        rejected = self.store.decide_tool_call(
            rejected["call_id"], "reject", actor="test-user", reason="不需要")
        self.assertEqual("rejected", rejected["status"])

        cancelled, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "workspace.task_create",
            {}, "write", "tool-request-cancel", actor="agent", trace_id="trc_toolcancel")
        cancelled = self.store.cancel_tool_call(
            cancelled["call_id"], actor="test-user", reason="改变计划")
        self.assertEqual("cancelled", cancelled["status"])

        running, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "workspace.inspect",
            {}, "read", "tool-request-running", actor="agent", trace_id="trc_toolrunning")
        self.assertEqual("approved", running["status"])
        self.store.claim_tool_call(running["call_id"])
        self.assertEqual({"turns": 0, "tool_calls": 1}, self.store.recover_interrupted())
        self.assertEqual("interrupted", self.store.get_tool_call(running["call_id"])["status"])
        history = self.store.model_messages(self.session["session_id"])
        self.assertEqual(["user", "assistant", "tool", "tool", "tool"],
                         [item["role"] for item in history])
        outcomes = [json.loads(item["content"]) for item in history[2:]]
        self.assertEqual({"rejected", "cancelled", "interrupted"},
                         {item["status"] for item in outcomes})
        self.assertTrue(all("result" not in item for item in outcomes))
        self.assertEqual({"不需要", "改变计划"},
                         {item["decision_reason"] for item in outcomes
                          if "decision_reason" in item})

    def test_turn_completion_and_model_tool_proposals_are_atomic(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "原子工具", "request-atomic-tool",
            actor="test-user", trace_id="trc_atomicturn")
        base = {"tool_name": "workspace.task_create", "arguments": {"fid": "f1"},
                "risk": "write", "idempotency_key": "atomic-tool-0001",
                "actor": "agent", "trace_id": "trc_atomictool"}
        invalid = {**base, "risk": "forged", "idempotency_key": "atomic-tool-0002"}
        with self.assertRaises(ProtocolError):
            self.store.complete_turn_with_tool_calls(
                turn["turn_id"], "准备工具", [base, invalid])
        self.assertEqual("running", self.store.get_turn(turn["turn_id"])["status"])
        self.assertEqual([], self.store.tool_calls(self.session["session_id"]))

        completed, calls = self.store.complete_turn_with_tool_calls(
            turn["turn_id"], "准备工具", [base])
        self.assertEqual("completed", completed["status"])
        self.assertEqual(1, len(calls))
        self.assertEqual("pending_approval", calls[0]["status"])

    def test_model_history_includes_only_settled_tool_outcomes(self):
        turn, _ = self.store.start_turn(
            self.session["session_id"], "创建任务", "request-feedback-turn",
            actor="test-user", trace_id="trc_feedbackturn")
        self.store.complete_turn(turn["turn_id"], "建议调用工具")
        call, _ = self.store.propose_tool_call(
            self.session["session_id"], turn["turn_id"], "workspace.task_create",
            {"fid": "f1", "title": "核验"}, "write", "tool-feedback-0001",
            actor="agent", trace_id="trc_feedbacktool")
        pending = self.store.model_messages(self.session["session_id"])
        self.assertEqual(["user", "assistant"], [item["role"] for item in pending])
        self.assertNotIn("tool_calls", pending[1])
        self.store.decide_tool_call(
            call["call_id"], "approve", actor="test-user", reason="确认")
        approved = self.store.model_messages(self.session["session_id"])
        self.assertNotIn("tool_calls", approved[1])
        self.store.claim_tool_call(call["call_id"])
        self.store.finish_tool_call(call["call_id"], result={"state_version": 2})
        next_turn, _ = self.store.start_turn(
            self.session["session_id"], "结果如何？", "request-feedback-next",
            actor="test-user", trace_id="trc_feedbacknext")
        messages = self.store.model_messages(self.session["session_id"], next_turn["turn_id"])
        self.assertEqual(["user", "assistant", "tool", "user"],
                         [item["role"] for item in messages])
        self.assertEqual(call["call_id"], messages[1]["tool_calls"][0]["id"])
        self.assertEqual("workspace_task_create",
                         messages[1]["tool_calls"][0]["function"]["name"])
        self.assertEqual(call["call_id"], messages[2]["tool_call_id"])
        self.assertEqual({"status": "completed", "call_id": call["call_id"],
                          "decision_reason": "确认",
                          "result": {"state_version": 2}},
                         json.loads(messages[2]["content"]))
        self.assertEqual("结果如何？", messages[-1]["content"])
        self.assertEqual(["user"],
                         [item["role"] for item in self.store.model_messages(
                             self.session["session_id"], next_turn["turn_id"], limit=3)])


class AgentRecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = AgentSessionStore(self.root)
        self.session = self.store.default_session(create=True, model="test-model")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def source(self, key="request-invalid-source", enqueue=False):
        turn, _ = self.store.start_turn(self.session["session_id"], "检查", key,
            actor="test-user", trace_id="trc_invalidsource", enqueue=enqueue,
            token_reservation=50, model_name="test-model")
        if enqueue:
            job = self.store.agent_job_for_turn(turn["turn_id"])
            self.store.claim_agent_job(job["job_id"], worker_id="test-worker")
        return turn

    def invalid(self, turn, suffix="0", raw='{"approval_sha256":BROKEN,"api_key":"hidden"}'):
        return {"tool_name": "research.revision_create", "arguments": {}, "risk": "write",
                "idempotency_key": "invalid-" + turn["turn_id"] + "-" + suffix,
                "actor": "agent", "trace_id": "trc_invalidcall",
                "validation_error": {"type": "NativeArgumentError", "code": "invalid_json",
                                     "message": "不是合法 JSON", "raw_arguments": raw}}

    def finish_invalid(self, turn):
        result = self.store.complete_turn_with_tool_calls(
            turn["turn_id"], "提议被校验拒绝", [self.invalid(turn)])
        job = self.store.agent_job_for_turn(turn["turn_id"])
        if job:
            self.store.finish_agent_job(job["job_id"], usage={"prompt_tokens": 7,
                "completion_tokens": 5, "total_tokens": 12})
        return result

    def downgrade_continuation_schema(self):
        path = self.store.path
        self.store.close()
        with sqlite3.connect(path) as conn:
            schema = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='continuation_states'").fetchone()[0]
            legacy = schema.replace("CREATE TABLE continuation_states(",
                                    "CREATE TABLE continuation_states_old(", 1).replace(
                                        "'waiting_job',", "")
            conn.execute(legacy)
            conn.execute("INSERT INTO continuation_states_old SELECT * FROM continuation_states")
            conn.execute("DROP TABLE continuation_states")
            conn.execute("ALTER TABLE continuation_states_old RENAME TO continuation_states")
            conn.execute("CREATE INDEX custom_continuation_root ON continuation_states(root_turn_id)")
            conn.execute("CREATE TABLE continuation_update_audit(source_turn_id TEXT, status TEXT)")
            conn.execute("CREATE TRIGGER custom_continuation_update AFTER UPDATE ON continuation_states "
                         "BEGIN INSERT INTO continuation_update_audit VALUES(NEW.source_turn_id, NEW.status); END")
            conn.execute("PRAGMA user_version=18")
        return path

    def test_waiting_job_survives_restart_without_changing_real_tool_or_turn(self):
        source = self.source()
        _, calls = self.finish_invalid(source)
        original_turn = self.store.get_turn(source["turn_id"])
        original_call = self.store.get_tool_call(calls[0]["call_id"])
        original_events = self.store.turn_events(self.session["session_id"])
        original_tool_events = self.store.tool_events(self.session["session_id"])
        expected = self.store.set_continuation_state(
            source["turn_id"], "waiting_job", {"type": "ResearchJobPending", "message": "等待真实结果", "job_id": "JOBR-synthetic"})
        self.store.close()
        self.store = AgentSessionStore(self.root)
        self.assertEqual(expected, self.store.get_continuation_state(source["turn_id"]))
        self.assertEqual(original_turn, self.store.get_turn(source["turn_id"]))
        self.assertEqual(original_call, self.store.get_tool_call(calls[0]["call_id"]))
        self.assertEqual(original_events, self.store.turn_events(self.session["session_id"]))
        self.assertEqual(original_tool_events, self.store.tool_events(self.session["session_id"]))
        self.assertTrue(self.store.verify_turn_events()["ok"])
        self.assertTrue(self.store.verify_tool_events()["ok"])

    def test_version18_continuation_upgrade_keeps_states_events_indexes_and_triggers(self):
        originals = {}
        for status in ("waiting_approval", "ready", "blocked"):
            source = self.source(key="continuation-upgrade-" + status)
            self.finish_invalid(source)
            originals[source["turn_id"]] = self.store.set_continuation_state(
                source["turn_id"], status, "旧状态保留：" + status)
        turn_events = self.store.turn_events(self.session["session_id"])
        tool_events = self.store.tool_events(self.session["session_id"])
        self.downgrade_continuation_schema()
        self.store = AgentSessionStore(self.root)
        self.assertEqual(SESSION_SCHEMA_VERSION, self.store._conn.execute("PRAGMA user_version").fetchone()[0])
        for turn_id, expected in originals.items():
            self.assertEqual(expected, self.store.get_continuation_state(turn_id))
        self.assertEqual(turn_events, self.store.turn_events(self.session["session_id"]))
        self.assertEqual(tool_events, self.store.tool_events(self.session["session_id"]))
        for name in ("custom_continuation_root", "custom_continuation_update"):
            self.assertIsNotNone(self.store._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())
        self.assertEqual([], self.store._conn.execute("SELECT * FROM continuation_update_audit").fetchall())
        turn_id = next(iter(originals))
        self.store.set_continuation_state(turn_id, "waiting_job", "仍在运行")
        self.assertEqual([(turn_id, "waiting_job")], [tuple(row) for row in
            self.store._conn.execute("SELECT * FROM continuation_update_audit")])
        self.assertEqual([], self.store._conn.execute("PRAGMA foreign_key_check").fetchall())
        self.store.close()
        self.store = AgentSessionStore(self.root)
        self.assertEqual("waiting_job", self.store.get_continuation_state(turn_id)["status"])
        self.assertEqual(1, self.store._conn.execute("SELECT count(*) FROM continuation_update_audit").fetchone()[0])

    def test_continuation_migration_failure_rolls_back_schema_rows_and_version(self):
        source = self.source()
        self.finish_invalid(source)
        expected = self.store.set_continuation_state(source["turn_id"], "blocked", "保留失败理由")
        path = self.downgrade_continuation_schema()
        with sqlite3.connect(path) as conn:
            before = conn.execute("SELECT sql FROM sqlite_master WHERE name='continuation_states'").fetchone()[0]
            events = conn.execute("SELECT * FROM tool_events ORDER BY seq").fetchall()
        execute = SQLiteConnection.execute

        def fail_after_table_rebuild(connection, sql, parameters=()):
            if sql.startswith("CREATE INDEX custom_continuation_root "):
                raise RuntimeError("synthetic interrupted continuation migration")
            return execute(connection, sql, parameters)

        with patch.object(SQLiteConnection, "execute", new=fail_after_table_rebuild):
            with self.assertRaisesRegex(RuntimeError, "interrupted continuation migration"):
                AgentSessionStore(self.root)
        with sqlite3.connect(path) as conn:
            self.assertEqual(before, conn.execute("SELECT sql FROM sqlite_master WHERE name='continuation_states'").fetchone()[0])
            self.assertEqual(18, conn.execute("PRAGMA user_version").fetchone()[0])
            self.assertEqual(events, conn.execute("SELECT * FROM tool_events ORDER BY seq").fetchall())
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='continuation_states_v19'").fetchone())
        self.store = AgentSessionStore(self.root)
        self.assertEqual(expected, self.store.get_continuation_state(source["turn_id"]))

    def test_invalid_is_direct_terminal_audit_not_approved_or_executed(self):
        source = self.source()
        _, calls = self.finish_invalid(source)
        call = calls[0]
        self.assertEqual("invalid", call["status"])
        self.assertFalse(call["error"]["executed"])
        self.assertEqual("invalid_json", call["error"]["code"])
        self.assertNotIn("hidden", json.dumps(call))
        self.assertEqual(["tool.invalid"], [event["kind"] for event in
                         self.store.tool_events(self.session["session_id"])["events"]])
        for operation in (lambda: self.store.decide_tool_call(call["call_id"], "approve",
                         actor="user", reason="no"), lambda: self.store.claim_tool_call(call["call_id"])):
            with self.assertRaises(ProtocolError):
                operation()
        history = self.store.model_messages(self.session["session_id"])
        self.assertEqual(["user", "assistant", "tool"], [item["role"] for item in history])
        native_args = history[1]["tool_calls"][0]["function"]["arguments"]
        self.assertIn("BROKEN", native_args)
        self.assertNotEqual("{}", native_args)
        outcome = json.loads(history[2]["content"])
        self.assertFalse(outcome["executed"])
        self.assertEqual(call["call_id"], history[2]["tool_call_id"])
        self.assertTrue(self.store.verify_tool_events()["ok"])

    def test_mixed_batch_waits_for_real_approval_and_preserves_invalid_group(self):
        source = self.source()
        valid = {"tool_name": "workspace.task_create", "arguments": {"title": "real"},
                 "risk": "write", "idempotency_key": "valid-with-invalid",
                 "actor": "agent", "trace_id": "trc_validcall"}
        _, calls = self.store.complete_turn_with_tool_calls(source["turn_id"], "两项", [self.invalid(source), valid])
        self.assertEqual({"invalid", "pending_approval"}, {call["status"] for call in calls})
        self.assertEqual("waiting_approval", self.store.get_continuation_state(source["turn_id"])["status"])
        self.assertNotIn("tool", [item["role"] for item in self.store.model_messages(self.session["session_id"])])
        actual = next(call for call in calls if call["status"] == "pending_approval")
        self.store.decide_tool_call(actual["call_id"], "reject", actor="user", reason="不批准")
        self.assertEqual("ready", self.store.get_continuation_state(source["turn_id"])["status"])
        history = self.store.model_messages(self.session["session_id"])
        self.assertEqual(2, sum(item["role"] == "tool" for item in history))

    def test_two_correction_requests_are_atomic_idempotent_and_survive_restart(self):
        source = self.source(enqueue=True)
        self.finish_invalid(source)
        current = source
        for depth in range(2):
            child, job, replay = self.store.continue_after_tools(current["turn_id"],
                trace_id=f"trc_correct{depth}", token_reservation=50)
            self.assertFalse(replay)
            repeated, same_job, replay = self.store.continue_after_tools(current["turn_id"],
                trace_id=f"trc_repeat{depth}", token_reservation=50)
            self.assertTrue(replay)
            self.assertEqual((child["turn_id"], job["job_id"]), (repeated["turn_id"], same_job["job_id"]))
            self.store.claim_agent_job(job["job_id"], worker_id="test-worker")
            self.finish_invalid(child)
            current = child
        self.store.close()
        self.store = AgentSessionStore(self.root)
        with self.assertRaisesRegex(ProtocolError, "上限 2"):
            self.store.continue_after_tools(current["turn_id"], trace_id="trc_correctblocked", token_reservation=50)
        state = self.store.get_continuation_state(current["turn_id"])
        self.assertEqual("blocked", state["status"])
        self.assertEqual(2, state["correction_count"])
        self.assertEqual(source["turn_id"], state["root_turn_id"])
        self.assertEqual(3, len(self.store.agent_jobs(self.session["session_id"])))

    def test_unknown_or_unsettled_usage_cannot_dispatch_correction(self):
        source = self.source(enqueue=True)
        self.store.complete_turn_with_tool_calls(source["turn_id"], "提议", [self.invalid(source)])
        with self.assertRaisesRegex(ProtocolError, "已知用量"):
            self.store.continue_after_tools(source["turn_id"], trace_id="trc_unknownblocked")
        job = self.store.agent_job_for_turn(source["turn_id"])
        self.store.finish_agent_job(job["job_id"])
        with self.assertRaisesRegex(ProtocolError, "已知用量"):
            self.store.continue_after_tools(source["turn_id"], trace_id="trc_stillunknown")
        self.assertEqual(1, len(self.store.agent_jobs(self.session["session_id"])))
        self.store.settle_agent_job_usage(job["job_id"], {"total_tokens": 12})
        _, child, _ = self.store.continue_after_tools(source["turn_id"], trace_id="trc_knownsettled")
        self.assertEqual("queued", child["status"])

    def test_late_known_usage_does_not_reverse_cancel_and_is_not_double_counted(self):
        source = self.source(enqueue=True)
        job = self.store.agent_job_for_turn(source["turn_id"])
        cancelled = self.store.cancel_turn(source["turn_id"], actor="user", reason="停止")
        usage = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
        settled = self.store.finish_agent_job(job["job_id"], usage=usage)
        self.assertEqual("cancelled", settled["status"])
        self.assertEqual(12, settled["total_tokens"])
        self.assertEqual(cancelled, self.store.get_turn(source["turn_id"]))
        self.assertEqual(settled, self.store.settle_agent_job_usage(job["job_id"], usage))
        self.assertEqual(settled, self.store.finish_agent_job(job["job_id"], usage=usage))
        self.assertEqual(12, self.store.usage_summary(self.session["session_id"])["total_tokens"])
        with self.assertRaisesRegex(ProtocolError, "冲突"):
            self.store.settle_agent_job_usage(job["job_id"], {"total_tokens": 13})
        self.assertEqual(12, self.store.get_agent_job(job["job_id"])["total_tokens"])

    def test_corrections_share_existing_eight_step_chain_ceiling(self):
        source = self.source(enqueue=True)
        self.finish_invalid(source)
        current = source
        for depth in range(8):
            child, job, _ = self.store.continue_after_tools(current["turn_id"],
                trace_id=f"trc_shared{depth}", token_reservation=50)
            self.store.claim_agent_job(job["job_id"], worker_id="test-worker")
            if depth == 0:
                self.finish_invalid(child)
            else:
                valid = {"tool_name": "workspace.file_list", "arguments": {}, "risk": "read",
                         "idempotency_key": "chain-valid-" + str(depth),
                         "actor": "agent", "trace_id": "trc_sharedtool"}
                _, calls = self.store.complete_turn_with_tool_calls(child["turn_id"], "读取", [valid])
                self.store.claim_tool_call(calls[0]["call_id"])
                self.store.finish_tool_call(calls[0]["call_id"], result={"files": []})
                self.store.finish_agent_job(job["job_id"], usage={"total_tokens": 12})
            current = child
        with self.assertRaisesRegex(ProtocolError, "上限 8"):
            self.store.continue_after_tools(current["turn_id"], trace_id="trc_sharedblocked")
        state = self.store.get_continuation_state(current["turn_id"])
        self.assertEqual(2, state["correction_count"])
        self.assertEqual("blocked", state["status"])
        self.assertEqual(9, len(self.store.agent_jobs(self.session["session_id"])))

    def test_budget_refusal_persists_reason_without_erasing_invalid_result(self):
        source = self.source(enqueue=True)
        _, calls = self.finish_invalid(source)
        self.store.set_token_budget(self.session["session_id"], 12)
        with self.assertRaisesRegex(ProtocolError, "预算"):
            self.store.continue_after_tools(source["turn_id"], trace_id="trc_budgetblocked", token_reservation=1)
        self.assertEqual("blocked", self.store.get_continuation_state(source["turn_id"])["status"])
        self.assertEqual("invalid", self.store.get_tool_call(calls[0]["call_id"])["status"])
        self.assertEqual(1, len(self.store.agent_jobs(self.session["session_id"])))

    def test_cancelled_late_usage_uses_immutable_job_price_once(self):
        catalog = self.store.register_pricing_catalog("late-usage-price", "USD", [{
            "model": "test-model", "prompt_micros_per_million": 1000000,
            "completion_micros_per_million": 2000000}], actor="user")
        self.store.set_price_catalog(self.session["session_id"], catalog["catalog_id"])
        source = self.source(enqueue=True)
        job = self.store.agent_job_for_turn(source["turn_id"])
        self.store.cancel_turn(source["turn_id"], actor="user", reason="停止")
        usage = {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
        final = self.store.finish_agent_job(job["job_id"], usage=usage)
        self.assertEqual("cancelled", final["status"])
        self.assertEqual(17, final["cost_micros"])
        self.store.settle_agent_job_usage(job["job_id"], usage)
        self.assertEqual(17, self.store.usage_summary(self.session["session_id"])["cost_micros"])

    def test_invalid_audit_oversize_is_digest_only_not_clipped_original(self):
        source = self.source()
        proposal = self.invalid(source)
        proposal["arguments"] = {"replacement": "x" * 70000, "api_key": "hidden"}
        _, calls = self.store.complete_turn_with_tool_calls(source["turn_id"], "拒绝", [proposal])
        audit = calls[0]["arguments"]["audit"]
        self.assertFalse(audit["retained"])
        self.assertGreater(audit["bytes"], 65536)
        self.assertEqual(64, len(audit["sha256"]))
        self.assertNotIn("replacement", calls[0]["arguments"])
        self.assertNotIn("hidden", json.dumps(calls[0]))

    def test_version17_check_upgrade_preserves_real_calls_and_event_chain(self):
        source = self.source()
        valid = {"tool_name": "workspace.task_create", "arguments": {"title": "retained"},
                 "risk": "write", "idempotency_key": "migration-call",
                 "actor": "agent", "trace_id": "trc_migratecall"}
        _, calls = self.store.complete_turn_with_tool_calls(source["turn_id"], "旧提议", [valid])
        path = self.store.path
        self.store.close()
        with sqlite3.connect(path) as conn:
            schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='tool_calls'").fetchone()[0]
            schema = schema.replace("CREATE TABLE tool_calls(", "CREATE TABLE tool_calls_old(").replace(",\'invalid\'", "")
            conn.execute(schema)
            conn.execute("INSERT INTO tool_calls_old SELECT * FROM tool_calls")
            conn.execute("DROP TABLE tool_calls")
            conn.execute("ALTER TABLE tool_calls_old RENAME TO tool_calls")
            conn.execute("CREATE INDEX custom_call_name ON tool_calls(tool_name)")
            conn.execute("PRAGMA user_version=17")
        self.store = AgentSessionStore(self.root)
        self.assertEqual(calls[0], self.store.get_tool_call(calls[0]["call_id"]))
        self.assertTrue(self.store.verify_tool_events()["ok"])
        self.assertIsNotNone(self.store._conn.execute("SELECT 1 FROM sqlite_master WHERE name='custom_call_name'").fetchone())
        fresh = self.source(key="migration-new-invalid")
        _, invalid = self.finish_invalid(fresh)
        self.assertEqual("invalid", invalid[0]["status"])
        self.assertEqual(SESSION_SCHEMA_VERSION, self.store._conn.execute("PRAGMA user_version").fetchone()[0])


if __name__ == "__main__":
    unittest.main()

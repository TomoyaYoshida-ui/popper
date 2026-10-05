"""Durable local Agent sessions and crash-recoverable turns.

The session store owns conversational execution state. A user turn is written
before any model call, and its terminal result is committed afterwards. If the
process dies between those commits, startup recovery records ``interrupted``;
it never invents an assistant answer or silently retries a side effect.
"""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import ProtocolError
from .eventlog import GENESIS_HASH, chained_body, verify_hash_chain
from .identity import EntityKind, content_id, new_id, parse_id
from .storage import SQLiteStorageAdapter, SQLiteConnection
from .task_context import CONTEXT_DDL, save_input, redact
from .tool_results import summarize_tool_result, read_detail

SESSIONS_REL = ".popper/sessions.sqlite"

def read_session_summaries(root):
    """Navigation-only read: never initialize, migrate or recover another project."""
    path = Path(root) / SESSIONS_REL
    if not path.is_file():
        return []
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows = connection.execute("SELECT session_id,title,status,created,updated FROM sessions ORDER BY updated DESC,session_id").fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["running"] = bool(connection.execute("SELECT 1 FROM turns WHERE session_id=? AND status='running' LIMIT 1", (row["session_id"],)).fetchone()) if "turns" in tables else False
            if "tool_calls" in tables:
                item["running"] = item["running"] or bool(connection.execute("SELECT 1 FROM tool_calls WHERE session_id=? AND status='running' LIMIT 1", (row["session_id"],)).fetchone())
            item["pending_approvals"] = connection.execute("SELECT count(*) FROM tool_calls WHERE session_id=? AND status='pending_approval'", (row["session_id"],)).fetchone()[0] if "tool_calls" in tables else 0
            result.append(item)
        return result
    finally:
        connection.close()

class SessionMetadataConflict(ProtocolError):
    pass
SESSION_SCHEMA_VERSION = 19
TURN_STATES = {"running", "completed", "failed", "interrupted"}

_DDL = """
CREATE TABLE IF NOT EXISTS session_meta(
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
    session_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    harness TEXT NOT NULL,
    harness_version TEXT,
    model TEXT,
    token_budget INTEGER,
    price_catalog_id TEXT,
    cost_budget_micros INTEGER,
    status TEXT NOT NULL CHECK(status IN ('active','archived')),
    created TEXT NOT NULL,
    updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS turns(
    turn_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    parent_turn_id TEXT,
    user_message TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted')),
    assistant_message TEXT,
    assistant_draft TEXT NOT NULL DEFAULT '',
    error_json TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
    FOREIGN KEY(parent_turn_id) REFERENCES turns(turn_id));
CREATE INDEX IF NOT EXISTS idx_turns_session_created
    ON turns(session_id, created, turn_id);
CREATE TABLE IF NOT EXISTS tool_calls(
    call_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    risk TEXT NOT NULL CHECK(risk IN ('read','disclose','write','execute')),
    status TEXT NOT NULL CHECK(status IN (
        'pending_approval','approved','rejected','running','completed','failed',
        'cancelled','interrupted','invalid')),
    proposed_by TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    decided_by TEXT,
    decision_reason TEXT,
    result_json TEXT,
    error_json TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    integrity_version INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
    FOREIGN KEY(turn_id) REFERENCES turns(turn_id));
CREATE INDEX IF NOT EXISTS idx_tool_calls_session_created
    ON tool_calls(session_id, created, call_id);
CREATE TABLE IF NOT EXISTS tool_events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    projection_sha256 TEXT NOT NULL,
    immutable_sha256 TEXT,
    previous_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_tool_events_call_seq ON tool_events(call_id,seq);
CREATE TABLE IF NOT EXISTS turn_events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN (
        'turn.started','turn.completed','turn.failed','turn.cancelled','turn.interrupted')),
    actor TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    created TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
    FOREIGN KEY(turn_id) REFERENCES turns(turn_id));
CREATE INDEX IF NOT EXISTS idx_turn_events_session_seq
    ON turn_events(session_id, seq);
CREATE TABLE IF NOT EXISTS agent_jobs(
    job_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    turn_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK(status IN (
        'queued','running','succeeded','failed','cancelled','interrupted')),
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    worker_id TEXT,
    lease_expires TEXT,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    reserved_tokens INTEGER NOT NULL DEFAULT 0,
    model_name TEXT,
    price_catalog_id TEXT,
    cost_micros INTEGER NOT NULL DEFAULT 0,
    reserved_cost_micros INTEGER NOT NULL DEFAULT 0,
    error_json TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id),
    FOREIGN KEY(turn_id) REFERENCES turns(turn_id));
CREATE INDEX IF NOT EXISTS idx_agent_jobs_status_created
    ON agent_jobs(status, created, job_id);
CREATE TABLE IF NOT EXISTS pricing_catalogs(
    catalog_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    currency TEXT NOT NULL,
    prices_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS continuation_states(
    source_turn_id TEXT PRIMARY KEY,
    root_turn_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('waiting_approval','waiting_job','ready','blocked')),
    reason_json TEXT,
    correction INTEGER NOT NULL DEFAULT 0,
    child_turn_id TEXT,
    updated TEXT NOT NULL,
    FOREIGN KEY(source_turn_id) REFERENCES turns(turn_id));
CREATE TABLE IF NOT EXISTS agent_usage_receipts(
    job_id TEXT PRIMARY KEY,
    receipt_id TEXT NOT NULL UNIQUE,
    usage_json TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    created TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES agent_jobs(job_id));
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _model_call_arguments(call):
    error = call.get("error") or {}
    if call["status"] == "invalid" and isinstance(error.get("raw_arguments"), str):
        # An OpenAI assistant function argument is a string, not necessarily
        # parseable JSON. Preserve the rejected native string, never substitute
        # a valid empty object and pretend it was the original model proposal.
        return redact(error["raw_arguments"])
    return json.dumps(redact(call["arguments"]), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


class SessionNotFound(ProtocolError):
    """The requested durable Agent session does not exist."""


class SessionIdempotencyConflict(ProtocolError):
    """A turn idempotency key was reused with different input or actor."""


class SessionBudgetExceeded(ProtocolError):
    """A queued model call would exceed the session's hard token budget."""


class TurnNotFound(ProtocolError):
    """The requested durable Agent turn does not exist."""


class ToolCallNotFound(ProtocolError):
    """The requested tool call does not exist."""


class ToolCallConflict(ProtocolError):
    """A tool-call transition or idempotency constraint was violated."""


class AgentSessionStore:
    """SQLite-backed sessions scoped to one workspace root."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / SESSIONS_REL
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._storage = SQLiteStorageAdapter(self.path, timeout=30)
        self._conn = self._storage.connect()
        with self._conn.transaction(write=True):
            self._conn.apply_schema(_DDL)
            self._conn.apply_schema(CONTEXT_DDL)
            tool_schema = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tool_calls'").fetchone()[0]
            if "'disclose'" not in tool_schema:
                old_risks = "CHECK(risk IN ('read','write','execute'))"
                upgraded = re.sub(
                    r'^CREATE TABLE\s+"?tool_calls"?\s*\(',
                    "CREATE TABLE tool_calls_v15(", tool_schema, count=1,
                    flags=re.IGNORECASE)
                if old_risks not in tool_schema or upgraded == tool_schema:
                    raise ProtocolError("tool_calls schema 不兼容，拒绝自动迁移")
                upgraded = upgraded.replace(
                    old_risks, "CHECK(risk IN ('read','disclose','write','execute'))", 1)
                self._conn.execute(upgraded)
                self._conn.execute("INSERT INTO tool_calls_v15 SELECT * FROM tool_calls")
                self._conn.execute("DROP TABLE tool_calls")
                self._conn.execute("ALTER TABLE tool_calls_v15 RENAME TO tool_calls")
                self._conn.execute("CREATE INDEX idx_tool_calls_session_created "
                                   "ON tool_calls(session_id, created, call_id)")
            tool_columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(tool_calls)")}
            if "integrity_version" not in tool_columns:
                self._conn.execute(
                    "ALTER TABLE tool_calls ADD COLUMN integrity_version INTEGER "
                    "NOT NULL DEFAULT 0")
            tool_schema = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='tool_calls'").fetchone()[0]
            if "'invalid'" not in tool_schema:
                # CHECK constraints require a transactional table rebuild. Keep
                # all columns, rows, indexes and triggers, including extensions.
                objects = [row[0] for row in self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name='tool_calls' "
                    "AND type IN ('index','trigger') AND sql IS NOT NULL")]
                upgraded = re.sub(r'^CREATE TABLE\s+"?tool_calls"?\s*\(',
                                  "CREATE TABLE tool_calls_v18(", tool_schema,
                                  count=1, flags=re.IGNORECASE)
                upgraded, replaced = re.subn(
                    r"(CHECK\s*\(\s*status\s+IN\s*\()([^)]*)(\)\s*\))",
                    lambda match: match[1] + match[2] + ",'invalid'" + match[3],
                    upgraded, count=1, flags=re.IGNORECASE)
                if replaced != 1 or upgraded == tool_schema:
                    raise ProtocolError("tool_calls status schema 不兼容，拒绝自动迁移")
                self._conn.execute(upgraded)
                self._conn.execute("INSERT INTO tool_calls_v18 SELECT * FROM tool_calls")
                self._conn.execute("DROP TABLE tool_calls")
                self._conn.execute("ALTER TABLE tool_calls_v18 RENAME TO tool_calls")
                for sql in objects:
                    self._conn.execute(sql)
            continuation_schema = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='continuation_states'").fetchone()[0]
            if "'waiting_job'" not in continuation_schema:
                objects = [row[0] for row in self._conn.execute(
                    "SELECT sql FROM sqlite_master WHERE tbl_name='continuation_states' "
                    "AND type IN ('index','trigger') AND sql IS NOT NULL")]
                upgraded = re.sub(
                    r'^CREATE TABLE\s+"?continuation_states"?\s*\(',
                    "CREATE TABLE continuation_states_v19(", continuation_schema,
                    count=1, flags=re.IGNORECASE)
                upgraded, replaced = re.subn(
                    r"(CHECK\s*\(\s*status\s+IN\s*\()([^)]*)(\)\s*\))",
                    lambda match: match[1] + match[2] + ",'waiting_job'" + match[3],
                    upgraded, count=1, flags=re.IGNORECASE)
                if replaced != 1 or upgraded == continuation_schema:
                    raise ProtocolError("continuation_states status schema 不兼容，拒绝自动迁移")
                # Rebuild CHECK constraints in the surrounding transaction.
                # Scheduling feedback and existing event chains stay intact;
                # copying rows must not fire the old table's custom triggers.
                self._conn.execute(upgraded)
                self._conn.execute(
                    "INSERT INTO continuation_states_v19 SELECT * FROM continuation_states")
                self._conn.execute("DROP TABLE continuation_states")
                self._conn.execute(
                    "ALTER TABLE continuation_states_v19 RENAME TO continuation_states")
                for sql in objects:
                    self._conn.execute(sql)
            tool_event_columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(tool_events)")}
            if "immutable_sha256" not in tool_event_columns:
                self._conn.execute(
                    "ALTER TABLE tool_events ADD COLUMN immutable_sha256 TEXT")
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(turns)")}
            if "actor" not in columns:
                self._conn.execute(
                    "ALTER TABLE turns ADD COLUMN actor TEXT NOT NULL DEFAULT 'legacy'")
            if "trace_id" not in columns:
                self._conn.execute(
                    "ALTER TABLE turns ADD COLUMN trace_id TEXT NOT NULL DEFAULT 'legacy'")
            if "cancel_requested" not in columns:
                self._conn.execute(
                    "ALTER TABLE turns ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
            if "assistant_draft" not in columns:
                self._conn.execute(
                    "ALTER TABLE turns ADD COLUMN assistant_draft TEXT NOT NULL DEFAULT ''")
            if "parent_turn_id" not in columns:
                self._conn.execute("ALTER TABLE turns ADD COLUMN parent_turn_id TEXT")
            session_columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(sessions)")}
            if "token_budget" not in session_columns:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN token_budget INTEGER")
            if "price_catalog_id" not in session_columns:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN price_catalog_id TEXT")
            if "cost_budget_micros" not in session_columns:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN cost_budget_micros INTEGER")
            if "harness_version" not in session_columns:
                self._conn.execute("ALTER TABLE sessions ADD COLUMN harness_version TEXT")
            job_columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(agent_jobs)")}
            for name in ("prompt_tokens", "completion_tokens", "total_tokens",
                         "reserved_tokens", "cost_micros", "reserved_cost_micros"):
                if name not in job_columns:
                    self._conn.execute(
                        f"ALTER TABLE agent_jobs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
            for name in ("model_name", "price_catalog_id"):
                if name not in job_columns:
                    self._conn.execute(f"ALTER TABLE agent_jobs ADD COLUMN {name} TEXT")
            event_columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(turn_events)")}
            legacy_events = "previous_hash" not in event_columns and "hash" not in event_columns
            if "previous_hash" not in event_columns:
                self._conn.execute("ALTER TABLE turn_events ADD COLUMN previous_hash TEXT")
            if "hash" not in event_columns:
                self._conn.execute("ALTER TABLE turn_events ADD COLUMN hash TEXT")
            if legacy_events:
                previous = GENESIS_HASH
                for row in self._conn.execute(
                        "SELECT seq,event_id,session_id,turn_id,kind,actor,trace_id,"
                        "payload_json,created FROM turn_events ORDER BY seq").fetchall():
                    fields = {"event_id": row[1], "session_id": row[2],
                              "turn_id": row[3], "kind": row[4], "actor": row[5],
                              "trace_id": row[6], "payload": json.loads(row[7]),
                              "created": row[8]}
                    _, event_hash = chained_body(fields, previous)
                    self._conn.execute(
                        "UPDATE turn_events SET previous_hash=?,hash=? WHERE seq=?",
                        (previous, event_hash, row[0]))
                    previous = event_hash
            elif self._conn.execute(
                    "SELECT 1 FROM turn_events WHERE previous_hash IS NULL OR hash IS NULL "
                    "LIMIT 1").fetchone():
                raise ProtocolError("Agent turn 事件哈希缺失，拒绝自动重建")
            self._conn.execute(f"PRAGMA user_version = {SESSION_SCHEMA_VERSION}")

    def close(self):
        self._conn.close()

    @classmethod
    def readonly(cls, root):
        """Read navigation/context without schema initialization or recovery."""
        instance = cls.__new__(cls)
        instance.root = Path(root).resolve()
        instance.path = instance.root / SESSIONS_REL
        if not instance.path.is_file():
            raise FileNotFoundError("尚无会话数据库")
        connection = sqlite3.connect(instance.path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        instance._conn = SQLiteConnection(connection)
        return instance

    def _append_turn_event(self, turn, kind, *, actor=None, payload=None, now=None):
        """Append an event inside the caller's active transaction."""
        created = now or _now()
        event_id = new_id(EntityKind.AUDIT_EVENT)
        event_payload = dict(payload or {})
        projection = self._conn.execute(
            "SELECT session_id,idempotency_key,actor,trace_id,parent_turn_id,"
            "user_message,status,assistant_message,error_json FROM turns WHERE turn_id=?",
            (turn["turn_id"],)).fetchone()
        if projection is None:
            raise ProtocolError("Turn 事件缺少快照")
        event_payload["turn_projection_sha256"] = self._turn_projection_hash(projection)
        event_payload["turn_identity_sha256"] = self._turn_identity_hash(projection)
        prior = self._conn.execute(
            "SELECT hash FROM turn_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = prior[0] if prior else GENESIS_HASH
        _, event_hash = chained_body(
            {"event_id": event_id, "session_id": turn["session_id"],
             "turn_id": turn["turn_id"], "kind": kind,
             "actor": actor or turn["actor"], "trace_id": turn["trace_id"],
             "payload": event_payload, "created": created}, previous)
        self._conn.execute(
            "INSERT INTO turn_events(event_id,session_id,turn_id,kind,actor,trace_id,"
            "payload_json,previous_hash,hash,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_id, turn["session_id"], turn["turn_id"], kind,
             actor or turn["actor"], turn["trace_id"],
             json.dumps(event_payload, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False),
             previous, event_hash, created))

    @staticmethod
    def _turn_projection_hash(row):
        projection = dict(row)
        if projection["error_json"] is not None:
            projection["error_json"] = json.loads(projection["error_json"])
        encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _turn_identity_hash(row):
        immutable = {key: row[key] for key in (
            "session_id", "idempotency_key", "actor", "trace_id",
            "parent_turn_id", "user_message")}
        encoded = json.dumps(immutable, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _tool_projection_hash(row):
        encoded = json.dumps(dict(row), ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _tool_immutable_hash(row):
        immutable = {key: row[key] for key in (
            "call_id", "session_id", "turn_id", "idempotency_key", "tool_name",
            "arguments_json", "risk", "proposed_by", "trace_id", "created")}
        encoded = json.dumps(immutable, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _append_tool_event(self, call_id, *, actor, now=None):
        """Bind one committed tool transition to its complete SQLite projection."""
        snapshot = self._conn.execute(
            "SELECT * FROM tool_calls WHERE call_id=?", (call_id,)).fetchone()
        if snapshot is None:
            raise ProtocolError("工具事件缺少快照")
        created = now or _now()
        prior = self._conn.execute(
            "SELECT hash FROM tool_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = prior[0] if prior else GENESIS_HASH
        fields = {"event_id": new_id(EntityKind.AUDIT_EVENT),
                  "session_id": snapshot["session_id"],
                  "turn_id": snapshot["turn_id"], "call_id": call_id,
                  "kind": "tool." + snapshot["status"], "actor": actor,
                  "trace_id": snapshot["trace_id"],
                  "projection_sha256": self._tool_projection_hash(snapshot),
                  "immutable_sha256": self._tool_immutable_hash(snapshot),
                  "created": created}
        _, event_hash = chained_body(fields, previous)
        self._conn.execute(
            "INSERT INTO tool_events(event_id,session_id,turn_id,call_id,kind,"
            "actor,trace_id,projection_sha256,immutable_sha256,previous_hash,hash,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (fields["event_id"], fields["session_id"], fields["turn_id"], call_id,
             fields["kind"], actor, fields["trace_id"],
             fields["projection_sha256"], fields["immutable_sha256"], previous,
             event_hash, created))

    def verify_tool_events(self):
        rows = list(self._conn.execute(
            "SELECT seq,event_id,session_id,turn_id,call_id,kind,actor,trace_id,"
            "projection_sha256,immutable_sha256,previous_hash,hash,created "
            "FROM tool_events ORDER BY seq"))
        integrity = verify_hash_chain(
            rows,
            lambda row: {"event_id": row[1], "session_id": row[2],
                         "turn_id": row[3], "call_id": row[4], "kind": row[5],
                         "actor": row[6], "trace_id": row[7],
                         "projection_sha256": row[8],
                         **({"immutable_sha256": row[9]} if row[9] is not None else {}),
                         "created": row[12]},
            lambda row: row[10], lambda row: row[11],
            label="Agent 工具事件链", seq_for=lambda row: row[0])
        if not integrity["ok"]:
            return integrity
        latest = {}
        identities = {}
        allowed = {
            None: {"pending_approval", "approved", "invalid"},
            "pending_approval": {"approved", "rejected", "cancelled"},
            "approved": {"running", "cancelled"},
            "running": {"completed", "failed", "interrupted"},
        }
        for row in rows:
            status = row[5].removeprefix("tool.")
            prior = latest.get(row[4])
            if (row[5] != "tool." + status
                    or status not in allowed.get(prior[0] if prior else None, set())):
                return {"ok": False, "events": row[0] - 1,
                        "reason": f"工具状态重放失败: {row[4]}/{row[5]}"}
            if row[9] is not None:
                previous_identity = identities.setdefault(row[4], row[9])
                if previous_identity != row[9]:
                    return {"ok": False, "events": row[0] - 1,
                            "reason": f"工具不可变字段跨事件不一致: {row[4]}"}
            latest[row[4]] = (status, row[8], row[2], row[3], row[7])
        for snapshot in self._conn.execute("SELECT * FROM tool_calls"):
            event = latest.pop(snapshot["call_id"], None)
            if snapshot["integrity_version"] == 1 and event is None:
                return {"ok": False, "events": integrity["events"],
                        "reason": f"工具事件缺失: {snapshot['call_id']}"}
            if event is not None and (
                    event[0] != snapshot["status"]
                    or event[1] != self._tool_projection_hash(snapshot)
                    or (snapshot["call_id"] in identities and
                        identities[snapshot["call_id"]] !=
                        self._tool_immutable_hash(snapshot))
                    or event[2:] != (snapshot["session_id"], snapshot["turn_id"],
                                     snapshot["trace_id"])):
                return {"ok": False, "events": integrity["events"],
                        "reason": f"工具快照与事件重放不一致: {snapshot['call_id']}"}
        if latest:
            return {"ok": False, "events": integrity["events"],
                    "reason": "工具事件缺少快照"}
        return integrity

    def create_session(self, title="新研究会话", harness="chat", model=None,
                       harness_version=None):
        if not isinstance(title, str) or not title.strip():
            raise ProtocolError("session title 不能为空")
        if not isinstance(harness, str) or not harness.strip():
            raise ProtocolError("session harness 不能为空")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ProtocolError("session model 不合法")
        if harness_version is not None and (
                not isinstance(harness_version, str) or not harness_version.strip()):
            raise ProtocolError("session harness_version 不合法")
        session_id, now = new_id(EntityKind.SESSION), _now()
        with self._conn.transaction(write=True):
            self._conn.execute(
                "INSERT INTO sessions(session_id,title,harness,harness_version,model,status,"
                "created,updated) VALUES(?,?,?,?,?,?,?,?)",
                (session_id, title.strip(), harness.strip(), harness_version,
                 model, "active", now, now))
        return self.get_session(session_id)

    def default_session(self, *, create=False, model=None):
        row = self._conn.execute(
            "SELECT value FROM session_meta WHERE key='default_session_id'").fetchone()
        if row is not None:
            return self.get_session(row[0])
        if not create:
            return None
        session = self.create_session("研究对话", "chat", model)
        with self._conn.transaction(write=True):
            self._conn.execute(
                "INSERT INTO session_meta(key,value) VALUES('default_session_id',?)",
                (session["session_id"],))
        return session

    def set_default_session(self, session_id):
        session = self.get_session(session_id)
        if session["status"] != "active":
            raise ProtocolError("归档会话需恢复后才能继续")
        with self._conn.transaction(write=True):
            self._conn.execute(
                "INSERT OR REPLACE INTO session_meta(key,value) "
                "VALUES('default_session_id',?)", (session_id,))
        return session

    def bind_legacy_chat_harness(self, session_id, harness, harness_version, model=None):
        session = self.get_session(session_id)
        if session["harness"] != "chat" or session["harness_version"] is not None:
            raise ProtocolError("只有未版本化的 legacy chat session 可以绑定 Harness")
        if (not isinstance(harness, str) or not harness.strip()
                or not isinstance(harness_version, str) or not harness_version.strip()):
            raise ProtocolError("Harness 绑定字段不合法")
        with self._conn.transaction(write=True):
            self._conn.execute(
                "UPDATE sessions SET harness=?,harness_version=?,model=?,updated=? "
                "WHERE session_id=?",
                (harness.strip(), harness_version.strip(), model, _now(), session_id))
        return self.get_session(session_id)

    def import_legacy_chat(self, path, model=None):
        """Import chat.json once, preserving honest assistant/error semantics."""
        done = self._conn.execute(
            "SELECT value FROM session_meta WHERE key='legacy_chat_imported'").fetchone()
        if done is not None:
            return 0
        source = Path(path)
        messages = []
        if source.is_file():
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                if isinstance(data, dict) and isinstance(data.get("messages"), list):
                    messages = data["messages"]
            except (OSError, ValueError):
                # A corrupt legacy file remains on disk for manual recovery; the new
                # authority starts empty instead of silently treating it as valid.
                messages = []
        imported = 0
        if messages:
            session = self.default_session(create=True, model=model)
            index = 0
            while index < len(messages):
                user = messages[index]
                if (not isinstance(user, dict) or user.get("role") != "user"
                        or not isinstance(user.get("text"), str)):
                    index += 1
                    continue
                assistant = messages[index + 1] if index + 1 < len(messages) else None
                key = "legacy-" + str(user.get("id") or new_id(EntityKind.TURN))
                turn, _ = self.start_turn(
                    session["session_id"], user["text"], key[:128],
                    actor="legacy_import", trace_id="trc_legacyimport")
                if isinstance(assistant, dict) and assistant.get("role") == "assistant":
                    if isinstance(assistant.get("error"), dict):
                        self.fail_turn(turn["turn_id"], assistant["error"])
                    elif isinstance(assistant.get("text"), str) and assistant["text"].strip():
                        self.complete_turn(turn["turn_id"], assistant["text"])
                    else:
                        self._finish(turn["turn_id"], "interrupted",
                                     error={"type": "LegacyIncomplete",
                                            "message": "旧对话助手消息不完整"})
                    index += 2
                else:
                    self._finish(turn["turn_id"], "interrupted",
                                 error={"type": "LegacyIncomplete",
                                        "message": "旧对话缺少助手回复"})
                    index += 1
                imported += 1
        with self._conn.transaction(write=True):
            self._conn.execute(
                "INSERT INTO session_meta(key,value) VALUES('legacy_chat_imported',?)",
                (str(imported),))
        return imported

    def get_session(self, session_id):
        parse_id(session_id, EntityKind.SESSION)
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise SessionNotFound(f"session 不存在: {session_id}")
        return dict(row)

    def list_sessions(self):
        return [dict(row) for row in self._conn.execute(
            "SELECT * FROM sessions ORDER BY updated DESC, session_id")]

    def update_session_metadata(self, session_id, expected_updated, *, title=None, status=None):
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title.strip()) > 200):
            raise ProtocolError("会话标题须为1到200个字符")
        if status is not None and (not isinstance(status, str) or status not in {"active", "archived"}):
            raise ProtocolError("会话状态不合法")
        if title is None and status is None:
            raise ProtocolError("没有会话元数据变更")
        with self._conn.transaction(write=True):
            current = self.get_session(session_id)
            if current["updated"] != expected_updated:
                raise SessionMetadataConflict("会话已有变化，请重新读取后操作")
            if status == "archived" and self._conn.execute("SELECT 1 FROM turns WHERE session_id=? AND status='running' LIMIT 1", (session_id,)).fetchone():
                raise ProtocolError("运行中的会话必须先停止才能归档")
            if status == "archived" and self._conn.execute("SELECT 1 FROM tool_calls WHERE session_id=? AND status='running' LIMIT 1", (session_id,)).fetchone():
                raise ProtocolError("工具仍在运行，请先停止工具再归档")
            self._conn.execute("UPDATE sessions SET title=?,status=?,updated=? WHERE session_id=?", (title.strip() if title is not None else current["title"], status or current["status"], _now(), session_id))
            selected = self._conn.execute("SELECT value FROM session_meta WHERE key='default_session_id'").fetchone()
            if status == "archived" and selected and selected[0] == session_id:
                next_session = self._conn.execute("SELECT session_id FROM sessions WHERE status='active' ORDER BY updated DESC,session_id LIMIT 1").fetchone()
                if next_session:
                    self._conn.execute("UPDATE session_meta SET value=? WHERE key='default_session_id'", (next_session[0],))
                else:
                    self._conn.execute("DELETE FROM session_meta WHERE key='default_session_id'")
        return self.get_session(session_id)

    def register_pricing_catalog(self, name, currency, prices, *, actor):
        if not isinstance(name, str) or not name.strip():
            raise ProtocolError("pricing catalog name 不能为空")
        if (not isinstance(currency, str) or len(currency.strip()) != 3
                or not currency.strip().isalpha()):
            raise ProtocolError("pricing catalog currency 必须是三字母代码")
        if not isinstance(actor, str) or not actor.strip():
            raise ProtocolError("pricing catalog actor 不能为空")
        if not isinstance(prices, list) or not prices:
            raise ProtocolError("pricing catalog prices 不能为空")
        normalized = []
        seen = set()
        for item in prices:
            if (not isinstance(item, dict) or set(item) != {
                    "model", "prompt_micros_per_million",
                    "completion_micros_per_million"}):
                raise ProtocolError("pricing catalog price 字段不合法")
            model = item["model"]
            prompt = item["prompt_micros_per_million"]
            completion = item["completion_micros_per_million"]
            if (not isinstance(model, str) or not model.strip() or model.strip() in seen
                    or not isinstance(prompt, int) or isinstance(prompt, bool) or prompt < 0
                    or not isinstance(completion, int) or isinstance(completion, bool)
                    or completion < 0):
                raise ProtocolError("pricing catalog price 值不合法")
            seen.add(model.strip())
            normalized.append({"model": model.strip(),
                               "prompt_micros_per_million": prompt,
                               "completion_micros_per_million": completion})
        normalized.sort(key=lambda item: item["model"])
        payload = {"name": name.strip(), "currency": currency.strip().upper(),
                   "prices": normalized}
        catalog_id = content_id(EntityKind.PRICING_CATALOG, payload)
        encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        with self._conn.transaction(write=True):
            self._conn.execute(
                "INSERT OR IGNORE INTO pricing_catalogs(catalog_id,name,currency,prices_json,"
                "actor,created) VALUES(?,?,?,?,?,?)",
                (catalog_id, payload["name"], payload["currency"], encoded,
                 actor.strip(), _now()))
        return self.get_pricing_catalog(catalog_id)

    def get_pricing_catalog(self, catalog_id):
        parse_id(catalog_id, EntityKind.PRICING_CATALOG)
        row = self._conn.execute(
            "SELECT * FROM pricing_catalogs WHERE catalog_id=?", (catalog_id,)).fetchone()
        if row is None:
            raise ProtocolError(f"pricing catalog 不存在: {catalog_id}")
        result = dict(row)
        result["prices"] = json.loads(result.pop("prices_json"))
        return result

    def list_pricing_catalogs(self):
        return [self.get_pricing_catalog(row[0]) for row in self._conn.execute(
            "SELECT catalog_id FROM pricing_catalogs ORDER BY created,catalog_id")]

    def set_price_catalog(self, session_id, catalog_id):
        session = self.get_session(session_id)
        if catalog_id is not None:
            self.get_pricing_catalog(catalog_id)
        if session["price_catalog_id"] != catalog_id and self._conn.execute(
                "SELECT 1 FROM agent_jobs WHERE session_id=? LIMIT 1",
                (session_id,)).fetchone():
            raise ProtocolError("已有 Job 的 session 不能更换 pricing catalog")
        with self._conn.transaction(write=True):
            self._conn.execute(
                "UPDATE sessions SET price_catalog_id=?,updated=? WHERE session_id=?",
                (catalog_id, _now(), session_id))
        return self.get_session(session_id)

    def set_cost_budget(self, session_id, cost_budget_micros):
        session = self.get_session(session_id)
        if cost_budget_micros is not None and (
                not isinstance(cost_budget_micros, int)
                or isinstance(cost_budget_micros, bool) or cost_budget_micros < 1):
            raise ProtocolError("session cost_budget_micros 必须为正整数或 null")
        if cost_budget_micros is not None and session["price_catalog_id"] is None:
            raise ProtocolError("设置货币预算前必须绑定 pricing catalog")
        usage = self.usage_summary(session_id)
        committed = usage["cost_micros"] + usage["reserved_cost_micros"]
        if cost_budget_micros is not None and cost_budget_micros < committed:
            raise SessionBudgetExceeded(
                f"cost budget 低于已结算与活跃预留总量 {committed}")
        with self._conn.transaction(write=True):
            self._conn.execute(
                "UPDATE sessions SET cost_budget_micros=?,updated=? WHERE session_id=?",
                (cost_budget_micros, _now(), session_id))
        return self.get_session(session_id)

    def _session_price(self, session_id, model_name):
        session = self.get_session(session_id)
        if session["price_catalog_id"] is None:
            return None, None
        if not isinstance(model_name, str) or not model_name.strip():
            raise ProtocolError("已配置价格目录时 model_name 不能为空")
        catalog = self.get_pricing_catalog(session["price_catalog_id"])
        price = next((item for item in catalog["prices"]
                      if item["model"] == model_name.strip()), None)
        if price is None:
            raise ProtocolError(f"价格目录未登记模型: {model_name}")
        return catalog, price

    @staticmethod
    def _cost_reservation(price, token_reservation):
        if price is None:
            return 0
        worst_rate = max(price["prompt_micros_per_million"],
                         price["completion_micros_per_million"])
        return (token_reservation * worst_rate + 999_999) // 1_000_000

    def _reserve_cost_budget(self, session_id, reservation):
        session = self.get_session(session_id)
        if session["cost_budget_micros"] is None:
            return
        usage = self.usage_summary(session_id)
        projected = (usage["cost_micros"] + usage["reserved_cost_micros"]
                     + reservation)
        if projected > session["cost_budget_micros"]:
            raise SessionBudgetExceeded(
                f"会话货币预算不足: limit={session['cost_budget_micros']}, "
                f"projected={projected}")

    def set_token_budget(self, session_id, token_budget):
        session = self.get_session(session_id)
        if token_budget is not None and (
                not isinstance(token_budget, int) or isinstance(token_budget, bool)
                or token_budget < 1):
            raise ProtocolError("session token_budget 必须为正整数或 null")
        usage = self.usage_summary(session_id)
        committed = usage["total_tokens"] + usage["reserved_tokens"]
        if token_budget is not None and token_budget < committed:
            raise SessionBudgetExceeded(
                f"token_budget 低于已消费与活跃预留总量 {committed}")
        with self._conn.transaction(write=True):
            self._conn.execute(
                "UPDATE sessions SET token_budget=?,updated=? WHERE session_id=?",
                (token_budget, _now(), session_id))
        return self.get_session(session_id)

    def _reserve_session_budget(self, session_id, reservation):
        if (not isinstance(reservation, int) or isinstance(reservation, bool)
                or reservation < 1):
            raise ProtocolError("agent job token reservation 不合法")
        session = self.get_session(session_id)
        if session["token_budget"] is None:
            return
        usage = self.usage_summary(session_id)
        projected = usage["total_tokens"] + usage["reserved_tokens"] + reservation
        if projected > session["token_budget"]:
            raise SessionBudgetExceeded(
                f"会话 token 预算不足: limit={session['token_budget']}, projected={projected}")

    def start_turn(self, session_id, text, idempotency_key, *, actor, trace_id,
                   enqueue=False, token_reservation=8192, model_name=None, model_input=None):
        parse_id(session_id, EntityKind.SESSION)
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("消息不能为空")
        if (not isinstance(idempotency_key, str) or not 8 <= len(idempotency_key) <= 128):
            raise ProtocolError("turn idempotency_key 不合法")
        if not isinstance(actor, str) or not actor.strip():
            raise ProtocolError("turn actor 不能为空")
        if (not isinstance(trace_id, str) or not trace_id.startswith("trc_")
                or len(trace_id) < 8):
            raise ProtocolError("turn trace_id 不合法")
        self._conn.begin(write=True)
        try:
            prior = self._conn.execute(
                "SELECT * FROM turns WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                if (prior["session_id"] != session_id
                        or prior["user_message"] != text.strip()
                        or prior["actor"] != actor):
                    raise SessionIdempotencyConflict(
                        "相同 idempotency_key 对应不同 turn")
                self._conn.commit()
                return self._decode_turn(prior), True
            session = self.get_session(session_id)
            if session["status"] != "active":
                raise ProtocolError("session 已归档")
            if enqueue:
                self._reserve_session_budget(session_id, token_reservation)
                catalog, price = self._session_price(session_id, model_name)
                cost_reservation = self._cost_reservation(price, token_reservation)
                self._reserve_cost_budget(session_id, cost_reservation)
            turn_id, now = new_id(EntityKind.TURN), _now()
            self._conn.execute(
                "INSERT INTO turns(turn_id,session_id,idempotency_key,actor,trace_id,"
                "user_message,status,created,updated) VALUES(?,?,?,?,?,?,?,?,?)",
                (turn_id, session_id, idempotency_key, actor, trace_id,
                 text.strip(), "running", now, now))
            self._conn.execute(
                "UPDATE sessions SET updated=? WHERE session_id=?", (now, session_id))
            self._append_turn_event(
                {"session_id": session_id, "turn_id": turn_id, "actor": actor,
                 "trace_id": trace_id}, "turn.started", now=now,
                payload={"status": "running"})
            if enqueue:
                self._conn.execute(
                    "INSERT INTO agent_jobs(job_id,session_id,turn_id,status,reserved_tokens,"
                    "reserved_cost_micros,model_name,price_catalog_id,created,updated) "
                    "VALUES(?,?,?,'queued',?,?,?,?,?,?)",
                    (new_id(EntityKind.JOB), session_id, turn_id, token_reservation,
                     cost_reservation, model_name,
                     catalog["catalog_id"] if catalog else None, now, now))
            if model_input is not None:
                save_input(self, turn_id, model_input)
            self._conn.commit()
            return self.get_turn(turn_id), False
        except Exception:
            self._conn.rollback()
            raise

    def _continuation_root(self, turn_id):
        cursor, seen = self.get_turn(turn_id), set()
        while cursor.get("parent_turn_id"):
            if cursor["turn_id"] in seen:
                raise ProtocolError("Turn parent 链存在循环")
            seen.add(cursor["turn_id"])
            cursor = self.get_turn(cursor["parent_turn_id"])
        return cursor["turn_id"]

    def get_continuation_state(self, source_turn_id):
        """Read persisted scheduling feedback; legacy read-only stores have none."""
        self.get_turn(source_turn_id)
        if not self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                  "AND name='continuation_states'").fetchone():
            return None
        row = self._conn.execute("SELECT * FROM continuation_states WHERE source_turn_id=?",
                                 (source_turn_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        encoded = result.pop("reason_json")
        result["reason"] = json.loads(encoded) if encoded else None
        result["correction_count"] = self._conn.execute(
            "SELECT count(*) FROM continuation_states WHERE root_turn_id=? "
            "AND correction=1 AND child_turn_id IS NOT NULL", (row["root_turn_id"],)).fetchone()[0]
        return result

    def _write_continuation_state(self, source_turn_id, state, reason=None,
                                  *, correction=None, child_turn_id=None):
        if state not in {"waiting_approval", "waiting_job", "ready", "blocked"}:
            raise ProtocolError("continuation state 不合法")
        if isinstance(reason, str):
            reason = {"type": "ContinuationBlocked", "message": reason}
        if reason is not None and not isinstance(reason, dict):
            raise ProtocolError("continuation reason 不合法")
        root = self._continuation_root(source_turn_id)
        prior = self.get_continuation_state(source_turn_id)
        is_correction = (int(bool(correction)) if correction is not None else
                         prior["correction"] if prior else 0)
        child = child_turn_id or (prior["child_turn_id"] if prior else None)
        self._conn.execute(
            "INSERT INTO continuation_states VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(source_turn_id) DO UPDATE SET status=excluded.status,"
            "reason_json=excluded.reason_json,correction=excluded.correction,"
            "child_turn_id=excluded.child_turn_id,updated=excluded.updated",
            (source_turn_id, root, state, json.dumps(redact(reason), ensure_ascii=False,
             sort_keys=True) if reason is not None else None, is_correction, child, _now()))

    def set_continuation_state(self, source_turn_id, state, reason=None):
        """Record a refusal/wait without changing tool or Turn terminal state."""
        with self._conn.transaction(write=True):
            self._write_continuation_state(source_turn_id, state, reason)
        return self.get_continuation_state(source_turn_id)

    def _refresh_continuation_state(self, source_turn_id):
        statuses = [row[0] for row in self._conn.execute(
            "SELECT status FROM tool_calls WHERE turn_id=?", (source_turn_id,))]
        settled = {"completed", "failed", "rejected", "cancelled", "interrupted", "invalid"}
        self._write_continuation_state(source_turn_id,
            "ready" if statuses and all(status in settled for status in statuses)
            else "waiting_approval")

    def continue_after_tools(self, source_turn_id, *, trace_id,
                             token_reservation=8192, model_name=None, model_input=None):
        """Queue a model-only turn after every proposal from the latest turn settles.

        No synthetic user message is written. The source turn and its native
        tool results become the continuation's provider history. A fixed key
        makes duplicate HTTP requests and crash-window retries idempotent.
        """
        parse_id(source_turn_id, EntityKind.TURN)
        if (not isinstance(trace_id, str) or not trace_id.startswith("trc_")
                or len(trace_id) < 8):
            raise ProtocolError("continuation trace_id 不合法")
        key = "continue-" + source_turn_id
        self._conn.begin(write=True)
        try:
            source = self.get_turn(source_turn_id)
            prior = self._conn.execute(
                "SELECT * FROM turns WHERE idempotency_key=?", (key,)).fetchone()
            if prior is not None:
                decoded = self._decode_turn(prior)
                if (decoded["parent_turn_id"] != source_turn_id
                        or decoded["actor"] != "agent_continuation"):
                    raise SessionIdempotencyConflict("continuation 幂等键冲突")
                self._conn.commit()
                return decoded, self.agent_job_for_turn(decoded["turn_id"]), True
            if source["status"] != "completed":
                raise ProtocolError("只有已完成的工具提议 turn 可以继续")
            latest = self.turns(source["session_id"])[-1]
            if latest["turn_id"] != source_turn_id:
                raise ProtocolError("已有后续 turn，不能从旧工具提议继续")
            calls = [call for call in self.tool_calls(source["session_id"])
                     if call["turn_id"] == source_turn_id]
            settled = {"completed", "failed", "rejected", "cancelled", "interrupted", "invalid"}
            if not calls or any(call["status"] not in settled for call in calls):
                self._write_continuation_state(source_turn_id, "waiting_approval",
                                              "工具提议尚未全部进入终态")
                self._conn.commit()
                raise ProtocolError("工具提议尚未全部进入终态")
            correction = any(call["status"] == "invalid" for call in calls)
            source_job = self.agent_job_for_turn(source_turn_id)
            if correction and source_job is not None:
                known_usage = self._conn.execute(
                    "SELECT 1 FROM agent_usage_receipts WHERE job_id=?",
                    (source_job["job_id"],)).fetchone()
                if source_job["status"] in {"queued", "running"} or not known_usage:
                    self._write_continuation_state(source_turn_id, "blocked",
                                                  "原模型调用尚未结算已知用量；参数纠正未发送")
                    self._conn.commit()
                    raise ProtocolError("原模型调用尚未结算已知用量，禁止参数自动纠正")
            root_id = self._continuation_root(source_turn_id)
            correction_count = self._conn.execute(
                "SELECT count(*) FROM continuation_states WHERE root_turn_id=? "
                "AND correction=1 AND child_turn_id IS NOT NULL", (root_id,)).fetchone()[0]
            if correction and correction_count >= 2:
                self._write_continuation_state(source_turn_id, "blocked",
                                              "参数自动纠正达到上限 2；请核对后明确继续")
                self._conn.commit()
                raise ProtocolError("参数自动纠正达到上限 2")
            depth, cursor = 0, source
            while cursor["actor"] == "agent_continuation":
                depth += 1
                if depth >= 8:
                    self._write_continuation_state(source_turn_id, "blocked",
                                                  "自动工具推理链达到上限 8")
                    self._conn.commit()
                    raise ProtocolError("自动工具推理链达到上限 8")
                cursor = self.get_turn(cursor["parent_turn_id"])
            session = self.get_session(source["session_id"])
            if session["status"] != "active":
                raise ProtocolError("session 已归档")
            self._reserve_session_budget(source["session_id"], token_reservation)
            catalog, price = self._session_price(source["session_id"], model_name)
            cost_reservation = self._cost_reservation(price, token_reservation)
            self._reserve_cost_budget(source["session_id"], cost_reservation)
            turn_id, now = new_id(EntityKind.TURN), _now()
            self._conn.execute(
                "INSERT INTO turns(turn_id,session_id,idempotency_key,actor,trace_id,"
                "parent_turn_id,user_message,status,created,updated) "
                "VALUES(?,?,?,?,?,?,'','running',?,?)",
                (turn_id, source["session_id"], key, "agent_continuation",
                 trace_id, source_turn_id, now, now))
            continuation = {"turn_id": turn_id, "session_id": source["session_id"],
                            "actor": "agent_continuation", "trace_id": trace_id}
            self._append_turn_event(
                continuation, "turn.started", now=now,
                payload={"status": "running", "continues_after": source_turn_id})
            job_id = new_id(EntityKind.JOB)
            self._conn.execute(
                "INSERT INTO agent_jobs(job_id,session_id,turn_id,status,reserved_tokens,"
                "reserved_cost_micros,model_name,price_catalog_id,created,updated) "
                "VALUES(?,?,?,'queued',?,?,?,?,?,?)",
                (job_id, source["session_id"], turn_id, token_reservation,
                 cost_reservation, model_name,
                 catalog["catalog_id"] if catalog else None, now, now))
            self._conn.execute("UPDATE sessions SET updated=? WHERE session_id=?",
                               (now, source["session_id"]))
            if model_input is not None:
                save_input(self, turn_id, model_input)
            self._write_continuation_state(source_turn_id, "ready", correction=correction,
                                           child_turn_id=turn_id)
            self._conn.commit()
            return self.get_turn(turn_id), self.get_agent_job(job_id), False
        except Exception as error:
            self._conn.rollback()
            if isinstance(error, SessionBudgetExceeded):
                self.set_continuation_state(source_turn_id, "blocked", str(error))
            raise

    def retry_turn(self, turn_id, idempotency_key, *, actor, trace_id,
                   token_reservation=8192, model_name=None, model_input=None):
        """Create a new queued Turn/Job linked to a failed or interrupted ancestor."""
        parse_id(turn_id, EntityKind.TURN)
        if (not isinstance(idempotency_key, str)
                or not 8 <= len(idempotency_key) <= 128):
            raise ProtocolError("turn retry idempotency_key 不合法")
        if not isinstance(actor, str) or not actor.strip():
            raise ProtocolError("turn retry actor 不能为空")
        if (not isinstance(trace_id, str) or not trace_id.startswith("trc_")
                or len(trace_id) < 8):
            raise ProtocolError("turn retry trace_id 不合法")
        self._conn.begin(write=True)
        try:
            source = self.get_turn(turn_id)
            if source["status"] not in {"failed", "interrupted"}:
                raise ProtocolError(f"只有失败或中断的 turn 可以重试: {source['status']}")
            self._reserve_session_budget(source["session_id"], token_reservation)
            catalog, price = self._session_price(source["session_id"], model_name)
            cost_reservation = self._cost_reservation(price, token_reservation)
            self._reserve_cost_budget(source["session_id"], cost_reservation)
            prior = self._conn.execute(
                "SELECT * FROM turns WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                decoded = self._decode_turn(prior)
                if (decoded["parent_turn_id"] != turn_id or decoded["actor"] != actor
                        or decoded["user_message"] != source["user_message"]):
                    raise SessionIdempotencyConflict(
                        "相同 idempotency_key 对应不同 retry turn")
                self._conn.commit()
                return decoded, self.agent_job_for_turn(decoded["turn_id"]), True
            depth, cursor = 0, source
            while cursor.get("parent_turn_id"):
                depth += 1
                if depth >= 10:
                    raise ProtocolError("turn 重试链达到上限 10")
                cursor = self.get_turn(cursor["parent_turn_id"])
            retry_id, now = new_id(EntityKind.TURN), _now()
            self._conn.execute(
                "INSERT INTO turns(turn_id,session_id,idempotency_key,actor,trace_id,"
                "parent_turn_id,user_message,status,created,updated) "
                "VALUES(?,?,?,?,?,?,?,'running',?,?)",
                (retry_id, source["session_id"], idempotency_key, actor.strip(), trace_id,
                 turn_id, source["user_message"], now, now))
            retry = {"turn_id": retry_id, "session_id": source["session_id"],
                     "actor": actor.strip(), "trace_id": trace_id}
            self._append_turn_event(
                retry, "turn.started", now=now,
                payload={"status": "running", "retry_of": turn_id})
            job_id = new_id(EntityKind.JOB)
            self._conn.execute(
                    "INSERT INTO agent_jobs(job_id,session_id,turn_id,status,reserved_tokens,"
                "reserved_cost_micros,model_name,price_catalog_id,created,updated) "
                "VALUES(?,?,?,'queued',?,?,?,?,?,?)",
                (job_id, source["session_id"], retry_id, token_reservation,
                 cost_reservation, model_name,
                 catalog["catalog_id"] if catalog else None, now, now))
            self._conn.execute(
                "UPDATE sessions SET updated=? WHERE session_id=?",
                (now, source["session_id"]))
            if model_input is not None:
                save_input(self, retry_id, model_input)
            self._conn.commit()
            return self.get_turn(retry_id), self.get_agent_job(job_id), False
        except Exception:
            self._conn.rollback()
            raise

    def steer_turn(self, turn_id, text, idempotency_key, *, actor, trace_id,
                   token_reservation=8192, model_name=None, model_input=None):
        """Atomically interrupt a running Turn and enqueue a linked new direction."""
        parse_id(turn_id, EntityKind.TURN)
        if not isinstance(text, str) or not text.strip():
            raise ProtocolError("转向消息不能为空")
        if (not isinstance(idempotency_key, str)
                or not 8 <= len(idempotency_key) <= 128):
            raise ProtocolError("turn steer idempotency_key 不合法")
        if not isinstance(actor, str) or not actor.strip():
            raise ProtocolError("turn steer actor 不能为空")
        if (not isinstance(trace_id, str) or not trace_id.startswith("trc_")
                or len(trace_id) < 8):
            raise ProtocolError("turn steer trace_id 不合法")
        self._conn.begin(write=True)
        try:
            source = self.get_turn(turn_id)
            prior = self._conn.execute(
                "SELECT * FROM turns WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior is not None:
                decoded = self._decode_turn(prior)
                if (decoded["parent_turn_id"] != turn_id or decoded["actor"] != actor
                        or decoded["user_message"] != text.strip()):
                    raise SessionIdempotencyConflict(
                        "相同 idempotency_key 对应不同 steer turn")
                self._conn.commit()
                return decoded, self.agent_job_for_turn(decoded["turn_id"]), True
            if source["status"] != "running":
                raise ProtocolError(f"只有运行中的 turn 可以转向: {source['status']}")
            catalog, price = self._session_price(source["session_id"], model_name)
            cost_reservation = self._cost_reservation(price, token_reservation)
            # The source reservation is released by cancellation in this same
            # transaction, so exclude it before checking the replacement.
            source_job = self.agent_job_for_turn(turn_id)
            released = source_job["reserved_tokens"] if source_job and source_job[
                "status"] in {"queued", "running"} else 0
            session = self.get_session(source["session_id"])
            if session["token_budget"] is not None:
                usage = self.usage_summary(source["session_id"])
                projected = (usage["total_tokens"] + usage["reserved_tokens"]
                             - released + token_reservation)
                if projected > session["token_budget"]:
                    raise SessionBudgetExceeded(
                        f"会话 token 预算不足: limit={session['token_budget']}, "
                        f"projected={projected}")
            if session["cost_budget_micros"] is not None:
                usage = self.usage_summary(source["session_id"])
                released_cost = (source_job["reserved_cost_micros"] if source_job
                                 and source_job["status"] in {"queued", "running"} else 0)
                projected_cost = (usage["cost_micros"] + usage["reserved_cost_micros"]
                                  - released_cost + cost_reservation)
                if projected_cost > session["cost_budget_micros"]:
                    raise SessionBudgetExceeded(
                        f"会话货币预算不足: limit={session['cost_budget_micros']}, "
                        f"projected={projected_cost}")
            child_id, job_id, now = new_id(EntityKind.TURN), new_id(EntityKind.JOB), _now()
            error = {"type": "Steered", "message": "用户提交了新的转向指令",
                     "actor": actor.strip(), "child_turn_id": child_id}
            self._conn.execute(
                "UPDATE turns SET status='interrupted',cancel_requested=1,error_json=?,"
                "updated=? WHERE turn_id=?",
                (json.dumps(error, ensure_ascii=False, sort_keys=True), now, turn_id))
            self._conn.execute(
                "UPDATE agent_jobs SET status='cancelled',error_json=?,lease_expires=NULL,"
                "updated=? WHERE turn_id=? AND status IN ('queued','running')",
                (json.dumps(error, ensure_ascii=False, sort_keys=True), now, turn_id))
            self._append_turn_event(
                source, "turn.cancelled", actor=actor.strip(), now=now,
                payload={"status": "interrupted", "reason": "steered",
                         "child_turn_id": child_id})
            self._conn.execute(
                "INSERT INTO turns(turn_id,session_id,idempotency_key,actor,trace_id,"
                "parent_turn_id,user_message,status,created,updated) "
                "VALUES(?,?,?,?,?,?,?,'running',?,?)",
                (child_id, source["session_id"], idempotency_key, actor.strip(), trace_id,
                 turn_id, text.strip(), now, now))
            child = {"turn_id": child_id, "session_id": source["session_id"],
                     "actor": actor.strip(), "trace_id": trace_id}
            self._append_turn_event(
                child, "turn.started", now=now,
                payload={"status": "running", "steered_from": turn_id})
            self._conn.execute(
                "INSERT INTO agent_jobs(job_id,session_id,turn_id,status,reserved_tokens,"
                "reserved_cost_micros,model_name,price_catalog_id,created,updated) "
                "VALUES(?,?,?,'queued',?,?,?,?,?,?)",
                (job_id, source["session_id"], child_id, token_reservation,
                 cost_reservation, model_name,
                 catalog["catalog_id"] if catalog else None, now, now))
            self._conn.execute(
                "UPDATE sessions SET updated=? WHERE session_id=?",
                (now, source["session_id"]))
            if model_input is not None:
                save_input(self, child_id, model_input)
            self._conn.commit()
            return self.get_turn(child_id), self.get_agent_job(job_id), False
        except Exception:
            self._conn.rollback()
            raise

    def get_turn(self, turn_id):
        parse_id(turn_id, EntityKind.TURN)
        row = self._conn.execute("SELECT * FROM turns WHERE turn_id=?", (turn_id,)).fetchone()
        if row is None:
            raise TurnNotFound(f"turn 不存在: {turn_id}")
        return self._decode_turn(row)

    @staticmethod
    def _decode_turn(row):
        result = dict(row)
        encoded_error = result.pop("error_json")
        result["error"] = json.loads(encoded_error) if encoded_error else None
        return result

    def _finish(self, turn_id, status, assistant_message=None, error=None):
        if status not in TURN_STATES - {"running"}:
            raise ProtocolError("turn 终态不合法")
        if status == "completed" and (not isinstance(assistant_message, str)
                                       or not assistant_message.strip()):
            raise ProtocolError("完成的 turn 必须有 assistant_message")
        now = _now()
        self._conn.begin(write=True)
        try:
            current = self.get_turn(turn_id)
            if current["status"] != "running":
                if (current["status"] == status
                        and current["assistant_message"] == assistant_message
                        and current["error"] == error):
                    self._conn.commit()
                    return current
                raise ProtocolError("turn 已进入终态")
            self._conn.execute(
                "UPDATE turns SET status=?,assistant_message=?,error_json=?,updated=? "
                "WHERE turn_id=?",
                (status, assistant_message,
                 json.dumps(error, ensure_ascii=False, sort_keys=True) if error else None,
                 now, turn_id))
            self._conn.execute(
                "UPDATE sessions SET updated=? WHERE session_id=?",
                (now, current["session_id"]))
            kind = {"completed": "turn.completed", "failed": "turn.failed",
                    "interrupted": "turn.interrupted"}[status]
            payload = {"status": status}
            if error:
                payload["error_type"] = error.get("type")
            self._append_turn_event(current, kind, payload=payload, now=now)
            self._conn.commit()
            return self.get_turn(turn_id)
        except Exception:
            self._conn.rollback()
            raise

    def complete_turn(self, turn_id, assistant_message):
        return self._finish(turn_id, "completed", assistant_message=assistant_message)

    def append_turn_delta(self, turn_id, delta):
        """Persist one streamed assistant text delta while the turn is running."""
        if not isinstance(delta, str) or not delta:
            raise ProtocolError("turn delta 必须是非空文本")
        if len(delta) > 65_536:
            raise ProtocolError("单个 turn delta 超过大小上限")
        self._conn.begin(write=True)
        try:
            current = self.get_turn(turn_id)
            if current["status"] != "running":
                raise ProtocolError("turn 已进入终态")
            draft = current["assistant_draft"] + delta
            if len(draft.encode("utf-8")) > 1_000_000:
                raise ProtocolError("turn 流式草稿超过大小上限")
            self._conn.execute(
                "UPDATE turns SET assistant_draft=?,updated=? WHERE turn_id=?",
                (draft, _now(), turn_id))
            self._conn.commit()
            return draft
        except Exception:
            self._conn.rollback()
            raise

    def complete_turn_with_tool_calls(self, turn_id, assistant_message, proposals):
        """Persist valid proposals and unexecuted validation failures atomically."""
        if (not isinstance(assistant_message, str) or not assistant_message.strip()
                or not isinstance(proposals, list) or not proposals or len(proposals) > 8):
            raise ProtocolError("assistant tool proposal 响应不合法")
        now = _now()
        self._conn.begin(write=True)
        try:
            turn = self.get_turn(turn_id)
            if turn["status"] != "running":
                raise ToolCallConflict(f"turn 已进入终态: {turn['status']}")
            prepared = []
            for proposal in proposals:
                required = {"tool_name", "arguments", "risk", "idempotency_key",
                            "actor", "trace_id"}
                if (not isinstance(proposal, dict) or not required.issubset(proposal)
                        or set(proposal) - required - {"validation_error"}
                        or not isinstance(proposal["tool_name"], str)
                        or not isinstance(proposal["arguments"], dict)
                        or proposal["risk"] not in {"read", "disclose", "write", "execute"}
                        or not isinstance(proposal["idempotency_key"], str)
                        or not isinstance(proposal["actor"], str)
                        or not isinstance(proposal["trace_id"], str)
                        or not proposal["trace_id"].startswith("trc_")):
                    raise ProtocolError("tool proposal 字段不合法")
                if self._conn.execute(
                        "SELECT 1 FROM tool_calls WHERE idempotency_key=?",
                        (proposal["idempotency_key"],)).fetchone():
                    raise SessionIdempotencyConflict("tool proposal 幂等键已存在")
                validation_error = proposal.get("validation_error")
                arguments = proposal["arguments"]
                if validation_error is not None:
                    if (not isinstance(validation_error, dict)
                            or not isinstance(validation_error.get("message"), str)
                            or not validation_error["message"].strip()):
                        raise ProtocolError("tool validation_error 不合法")
                    validation_error = {**redact(validation_error),
                                        "type": "ToolValidationError", "executed": False}
                    arguments = redact(arguments)
                    safe_body = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                                           separators=(",", ":"), allow_nan=False)
                    size = len(safe_body.encode("utf-8"))
                    if size > 65536:
                        arguments = {"audit": {"retained": False, "bytes": size,
                            "sha256": hashlib.sha256(safe_body.encode("utf-8")).hexdigest(),
                            "reason": "invalid arguments exceed 64 KiB; digest only, not a summary"}}
                    if len(json.dumps(validation_error, ensure_ascii=False).encode("utf-8")) > 65536:
                        raise ProtocolError("tool validation_error 超过大小上限")
                encoded = json.dumps(arguments, ensure_ascii=False,
                                     sort_keys=True, separators=(",", ":"), allow_nan=False)
                prepared.append((new_id(EntityKind.TOOL_CALL), proposal, encoded, validation_error))
            self._conn.execute(
                "UPDATE turns SET status='completed',assistant_message=?,updated=? "
                "WHERE turn_id=?", (assistant_message.strip(), now, turn_id))
            for call_id, proposal, encoded, validation_error in prepared:
                status = ("invalid" if validation_error is not None else
                          "approved" if proposal["risk"] == "read" else "pending_approval")
                self._conn.execute(
                    "INSERT INTO tool_calls(call_id,session_id,turn_id,idempotency_key,"
                    "tool_name,arguments_json,risk,status,proposed_by,trace_id,created,updated,"
                    "integrity_version,error_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
                    (call_id, turn["session_id"], turn_id, proposal["idempotency_key"],
                     proposal["tool_name"], encoded, proposal["risk"], status,
                     proposal["actor"], proposal["trace_id"], now, now,
                     json.dumps(validation_error, ensure_ascii=False, sort_keys=True)
                     if validation_error is not None else None))
                self._append_tool_event(call_id, actor=proposal["actor"], now=now)
            self._write_continuation_state(
                turn_id, "waiting_approval" if any(item[3] is None for item in prepared) else "ready")
            self._conn.execute(
                "UPDATE sessions SET updated=? WHERE session_id=?",
                (now, turn["session_id"]))
            self._append_turn_event(
                turn, "turn.completed", now=now,
                payload={"status": "completed", "tool_call_count": len(prepared)})
            self._conn.commit()
            return self.get_turn(turn_id), [self.get_tool_call(item[0]) for item in prepared]
        except Exception:
            self._conn.rollback()
            raise

    def fail_turn(self, turn_id, error):
        if not isinstance(error, dict) or not isinstance(error.get("type"), str):
            raise ProtocolError("turn error 不合法")
        return self._finish(turn_id, "failed", error=error)

    def cancel_turn(self, turn_id, *, actor, reason=""):
        """Persist a user cancellation as an interrupted terminal turn.

        The terminal write wins against a late model result because every normal
        completion path requires the turn to still be ``running``.
        """
        if not isinstance(actor, str) or not actor.strip():
            raise ProtocolError("turn cancel actor 不能为空")
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ProtocolError("turn cancel reason 不合法")
        error = {"type": "Cancelled", "message": reason.strip() or "用户取消了模型调用",
                 "actor": actor.strip()}
        now = _now()
        self._conn.begin(write=True)
        try:
            current = self.get_turn(turn_id)
            if current["status"] != "running":
                if current["cancel_requested"] and current["error"] == error:
                    self._conn.commit()
                    return current
                raise ProtocolError("turn 已进入终态")
            self._conn.execute(
                "UPDATE turns SET status='interrupted',cancel_requested=1,error_json=?,updated=? "
                "WHERE turn_id=?", (json.dumps(error, ensure_ascii=False, sort_keys=True),
                                    now, turn_id))
            self._conn.execute("UPDATE sessions SET updated=? WHERE session_id=?",
                               (now, current["session_id"]))
            self._append_turn_event(
                current, "turn.cancelled", actor=actor.strip(), now=now,
                payload={"status": "interrupted", "reason": error["message"]})
            self._conn.execute(
                "UPDATE agent_jobs SET status='cancelled',error_json=?,lease_expires=NULL,"
                "updated=? WHERE turn_id=? AND status IN ('queued','running')",
                (json.dumps(error, ensure_ascii=False, sort_keys=True), now, turn_id))
            self._conn.commit()
            return self.get_turn(turn_id)
        except Exception:
            self._conn.rollback()
            raise

    def recover_interrupted(self):
        for integrity in (self.verify_turn_events(), self.verify_tool_events()):
            if not integrity["ok"]:
                raise ProtocolError(integrity["reason"])
        now = _now()
        error = json.dumps({"type": "Interrupted", "message": "进程退出前模型调用未完成"},
                           ensure_ascii=False, sort_keys=True)
        tool_error = json.dumps({
            "type": "OutcomeUnknown",
            "message": "进程退出前工具调用未结束，且没有匹配的持久化命令收据；副作用可能已发生，请核查工作区状态",
        }, ensure_ascii=False, sort_keys=True)
        with self._conn.transaction(write=True):
            # A process may die after committing the turn terminal state but before
            # finalizing its job. Reconcile from the turn, which is authoritative.
            self._conn.execute(
                "UPDATE agent_jobs SET status=CASE "
                "WHEN (SELECT status FROM turns WHERE turns.turn_id=agent_jobs.turn_id)='completed' "
                "THEN 'succeeded' WHEN (SELECT cancel_requested FROM turns WHERE "
                "turns.turn_id=agent_jobs.turn_id)=1 THEN 'cancelled' ELSE 'failed' END, "
                "lease_expires=NULL,updated=? WHERE status='running' AND "
                "(SELECT status FROM turns WHERE turns.turn_id=agent_jobs.turn_id)!='running'",
                (now,))
            running = list(self._conn.execute(
                "SELECT * FROM turns WHERE status='running' AND (NOT EXISTS (SELECT 1 FROM "
                "agent_jobs WHERE agent_jobs.turn_id=turns.turn_id) OR EXISTS (SELECT 1 FROM "
                "agent_jobs WHERE agent_jobs.turn_id=turns.turn_id AND "
                "agent_jobs.status='running'))"))
            turns = self._conn.execute(
                "UPDATE turns SET status='interrupted',error_json=?,updated=? "
                "WHERE status='running' AND (NOT EXISTS (SELECT 1 FROM agent_jobs WHERE "
                "agent_jobs.turn_id=turns.turn_id) OR EXISTS (SELECT 1 FROM agent_jobs WHERE "
                "agent_jobs.turn_id=turns.turn_id AND agent_jobs.status='running'))",
                (error, now)).rowcount
            for row in running:
                self._append_turn_event(
                    dict(row), "turn.interrupted", actor="system", now=now,
                    payload={"status": "interrupted", "error_type": "Interrupted"})
            self._conn.execute(
                "UPDATE agent_jobs SET status='interrupted',error_json=?,lease_expires=NULL,"
                "updated=? WHERE status='running'", (error, now))
            running_tools = list(self._conn.execute(
                "SELECT call_id FROM tool_calls WHERE status='running'"))
            tools = self._conn.execute(
                "UPDATE tool_calls SET status='interrupted',error_json=?,updated=? "
                "WHERE status='running'", (tool_error, now)).rowcount
            for row in running_tools:
                self._append_tool_event(row[0], actor="system", now=now)
        return {"turns": turns, "tool_calls": tools}

    # ---- Persistent Agent jobs --------------------------------------
    @staticmethod
    def _decode_job(row):
        result = dict(row)
        encoded = result.pop("error_json")
        result["error"] = json.loads(encoded) if encoded else None
        return result

    def get_agent_job(self, job_id):
        parse_id(job_id, EntityKind.JOB)
        row = self._conn.execute(
            "SELECT * FROM agent_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ProtocolError(f"agent job 不存在: {job_id}")
        return self._decode_job(row)

    def agent_job_for_turn(self, turn_id):
        parse_id(turn_id, EntityKind.TURN)
        row = self._conn.execute(
            "SELECT * FROM agent_jobs WHERE turn_id=?", (turn_id,)).fetchone()
        return self._decode_job(row) if row else None

    def queued_agent_jobs(self):
        return [self._decode_job(row) for row in self._conn.execute(
            "SELECT * FROM agent_jobs WHERE status='queued' ORDER BY created,job_id")]

    def agent_jobs(self, session_id):
        self.get_session(session_id)
        return [self._decode_job(row) for row in self._conn.execute(
            "SELECT * FROM agent_jobs WHERE session_id=? ORDER BY created,job_id",
            (session_id,))]

    def usage_summary(self, session_id):
        self.get_session(session_id)
        row = self._conn.execute(
            "SELECT COUNT(*) AS jobs,COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,"
            "COALESCE(SUM(completion_tokens),0) AS completion_tokens,"
            "COALESCE(SUM(total_tokens),0) AS total_tokens,"
            "COALESCE(SUM(CASE WHEN status IN ('queued','running') THEN reserved_tokens "
            "ELSE 0 END),0) AS reserved_tokens,"
            "COALESCE(SUM(cost_micros),0) AS cost_micros,"
            "COALESCE(SUM(CASE WHEN status IN ('queued','running') THEN "
            "reserved_cost_micros ELSE 0 END),0) AS reserved_cost_micros "
            "FROM agent_jobs "
            "WHERE session_id=?", (session_id,)).fetchone()
        result = dict(row)
        session = self.get_session(session_id)
        result["token_budget"] = session["token_budget"]
        result["currency"] = (self.get_pricing_catalog(session["price_catalog_id"])["currency"]
                              if session["price_catalog_id"] else None)
        result["cost_budget_micros"] = session["cost_budget_micros"]
        result["remaining_cost_micros"] = (
            None if session["cost_budget_micros"] is None else max(
                0, session["cost_budget_micros"] - result["cost_micros"]
                - result["reserved_cost_micros"]))
        result["remaining_tokens"] = (None if session["token_budget"] is None else max(
            0, session["token_budget"] - result["total_tokens"]
            - result["reserved_tokens"]))
        return result

    def claim_agent_job(self, job_id, *, worker_id, lease_seconds=180):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ProtocolError("agent job worker_id 不能为空")
        if not isinstance(lease_seconds, int) or not 10 <= lease_seconds <= 3600:
            raise ProtocolError("agent job lease_seconds 不合法")
        self._conn.begin(write=True)
        try:
            job = self.get_agent_job(job_id)
            if job["status"] != "queued":
                raise ProtocolError(f"agent job 不能领取: {job['status']}")
            now = datetime.now(timezone.utc)
            lease = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds")
            self._conn.execute(
                "UPDATE agent_jobs SET status='running',attempt=attempt+1,worker_id=?,"
                "lease_expires=?,updated=? WHERE job_id=?",
                (worker_id.strip(), lease, now.isoformat(timespec="microseconds"), job_id))
            self._conn.commit()
            return self.get_agent_job(job_id)
        except Exception:
            self._conn.rollback()
            raise

    def renew_agent_job(self, job_id, *, worker_id, lease_seconds=180):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ProtocolError("agent job worker_id 不能为空")
        if not isinstance(lease_seconds, int) or not 10 <= lease_seconds <= 3600:
            raise ProtocolError("agent job lease_seconds 不合法")
        self._conn.begin(write=True)
        try:
            job = self.get_agent_job(job_id)
            if job["status"] != "running" or job["worker_id"] != worker_id.strip():
                raise ProtocolError("agent job lease 所有权不匹配")
            now = datetime.now(timezone.utc)
            lease = (now + timedelta(seconds=lease_seconds)).isoformat(timespec="microseconds")
            self._conn.execute(
                "UPDATE agent_jobs SET lease_expires=?,updated=? WHERE job_id=?",
                (lease, now.isoformat(timespec="microseconds"), job_id))
            self._conn.commit()
            return self.get_agent_job(job_id)
        except Exception:
            self._conn.rollback()
            raise

    def finish_agent_job(self, job_id, *, error=None, usage=None):
        usage = usage or {}
        allowed_usage = {"prompt_tokens", "completion_tokens", "total_tokens"}
        if (not isinstance(usage, dict) or not set(usage).issubset(allowed_usage)
                or any(type(value) is not int or value < 0
                       for value in usage.values())):
            raise ProtocolError("agent job usage 不合法")
        self._conn.begin(write=True)
        try:
            job = self.get_agent_job(job_id)
            if job["status"] != "running":
                if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                    if "total_tokens" in usage:
                        self._settle_agent_job_usage(job, usage)
                    self._conn.commit()
                    return self.get_agent_job(job_id)
                raise ProtocolError(f"agent job 不在 running 状态: {job['status']}")
            turn = self.get_turn(job["turn_id"])
            if turn["status"] == "running":
                raise ProtocolError("agent job 不能先于 turn 进入终态")
            if turn["status"] == "completed":
                status, stored_error = "succeeded", None
            elif turn["cancel_requested"]:
                status, stored_error = "cancelled", turn["error"]
            else:
                status, stored_error = "failed", error or turn["error"]
            cost_micros = 0
            if job["price_catalog_id"] is not None:
                catalog = self.get_pricing_catalog(job["price_catalog_id"])
                price = next((item for item in catalog["prices"]
                              if item["model"] == job["model_name"]), None)
                if price is None:
                    raise ProtocolError("Job 快照的模型价格不存在")
                numerator = (usage.get("prompt_tokens", 0)
                             * price["prompt_micros_per_million"]
                             + usage.get("completion_tokens", 0)
                             * price["completion_micros_per_million"])
                cost_micros = (numerator + 999_999) // 1_000_000
            self._conn.execute(
                "UPDATE agent_jobs SET status=?,error_json=?,lease_expires=NULL,"
                "prompt_tokens=?,completion_tokens=?,total_tokens=?,cost_micros=?,"
                "updated=? WHERE job_id=?",
                (status, json.dumps(stored_error, ensure_ascii=False, sort_keys=True)
                 if stored_error else None, usage.get("prompt_tokens", 0),
                 usage.get("completion_tokens", 0), usage.get("total_tokens", 0),
                 cost_micros, _now(), job_id))
            if "total_tokens" in usage:
                self._settle_agent_job_usage(self.get_agent_job(job_id), usage)
            self._conn.commit()
            return self.get_agent_job(job_id)
        except Exception:
            self._conn.rollback()
            raise

    def _settle_agent_job_usage(self, job, usage, receipt_id=None):
        """Settle known late usage without changing terminal state or its error."""
        allowed = {"prompt_tokens", "completion_tokens", "total_tokens"}
        if (not isinstance(usage, dict) or not set(usage).issubset(allowed)
                or "total_tokens" not in usage
                or any(type(value) is not int or value < 0 for value in usage.values())):
            raise ProtocolError("已知 provider total_tokens 才能登记用量结算")
        if job["status"] not in {"succeeded", "failed", "cancelled", "interrupted"}:
            raise ProtocolError("用量结算不得先于模型 Job 终态")
        normalized = {name: usage.get(name, 0) for name in sorted(allowed)}
        if normalized["prompt_tokens"] + normalized["completion_tokens"] > normalized["total_tokens"]:
            raise ProtocolError("provider 用量分项超过 total_tokens")
        body = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
        prior = self._conn.execute("SELECT * FROM agent_usage_receipts WHERE job_id=?",
                                   (job["job_id"],)).fetchone()
        if prior:
            if prior["sha256"] != sha:
                raise SessionIdempotencyConflict("模型 Job 用量结算冲突")
            return
        if any(job[name] for name in allowed) and any(job[name] != normalized[name] for name in allowed):
            raise SessionIdempotencyConflict("模型 Job 已登记用量与迟到结算不一致")
        receipt_id = receipt_id or "usage-" + job["job_id"]
        if not isinstance(receipt_id, str) or not 8 <= len(receipt_id) <= 128:
            raise ProtocolError("用量结算 receipt_id 不合法")
        reused = self._conn.execute("SELECT job_id FROM agent_usage_receipts WHERE receipt_id=?",
                                    (receipt_id,)).fetchone()
        if reused:
            raise SessionIdempotencyConflict("用量结算 receipt_id 已属于另一模型 Job")
        cost_micros = 0
        if job["price_catalog_id"] is not None:
            catalog = self.get_pricing_catalog(job["price_catalog_id"])
            price = next((item for item in catalog["prices"] if item["model"] == job["model_name"]), None)
            if price is None:
                raise ProtocolError("Job 快照的模型价格不存在")
            numerator = (normalized["prompt_tokens"] * price["prompt_micros_per_million"]
                         + normalized["completion_tokens"] * price["completion_micros_per_million"])
            cost_micros = (numerator + 999_999) // 1_000_000
        self._conn.execute("INSERT INTO agent_usage_receipts VALUES(?,?,?,?,?)",
                           (job["job_id"], receipt_id, body, sha, _now()))
        self._conn.execute("UPDATE agent_jobs SET prompt_tokens=?,completion_tokens=?,"
                           "total_tokens=?,cost_micros=?,updated=? WHERE job_id=?",
                           (normalized["prompt_tokens"], normalized["completion_tokens"],
                            normalized["total_tokens"], cost_micros, _now(), job["job_id"]))

    def settle_agent_job_usage(self, job_id, usage, *, receipt_id=None):
        """Independent idempotent known-usage receipt, including cancelled jobs."""
        with self._conn.transaction(write=True):
            self._settle_agent_job_usage(self.get_agent_job(job_id), usage, receipt_id)
        return self.get_agent_job(job_id)

    def turns(self, session_id):
        self.get_session(session_id)
        return [self._decode_turn(row) for row in self._conn.execute(
            "SELECT * FROM turns WHERE session_id=? ORDER BY created,turn_id", (session_id,))]

    def turn_events(self, session_id, *, after=0, limit=200):
        self.get_session(session_id)
        if not isinstance(after, int) or after < 0:
            raise ProtocolError("event cursor 不合法")
        if not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ProtocolError("event limit 不合法")
        events = []
        for row in self._conn.execute(
                "SELECT * FROM turn_events WHERE session_id=? AND seq>? "
                "ORDER BY seq LIMIT ?", (session_id, after, limit)):
            event = dict(row)
            event["payload"] = json.loads(event.pop("payload_json"))
            events.append(event)
        return {"events": events, "cursor": events[-1]["seq"] if events else after,
                "has_more": bool(events and self._conn.execute(
                    "SELECT 1 FROM turn_events WHERE session_id=? AND seq>? LIMIT 1",
                    (session_id, events[-1]["seq"])).fetchone())}

    def tool_events(self, session_id, *, after=0, limit=200):
        """Page the session's tool audit events without exposing tool result bodies."""
        self.get_session(session_id)
        if type(after) is not int or after < 0:
            raise ProtocolError("tool event cursor 不合法")
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ProtocolError("tool event limit 不合法")
        events = [dict(row) for row in self._conn.execute(
            "SELECT * FROM tool_events WHERE session_id=? AND seq>? "
            "ORDER BY seq LIMIT ?", (session_id, after, limit))]
        cursor = events[-1]["seq"] if events else after
        return {"events": events, "cursor": cursor,
                "has_more": bool(events and self._conn.execute(
                    "SELECT 1 FROM tool_events WHERE session_id=? AND seq>? LIMIT 1",
                    (session_id, cursor)).fetchone())}

    def verify_turn_events(self):
        """Verify the global turn-event chain and replay event-owned turn states."""
        rows = list(self._conn.execute(
            "SELECT seq,event_id,session_id,turn_id,kind,actor,trace_id,payload_json,"
            "previous_hash,hash,created FROM turn_events ORDER BY seq"))
        integrity = verify_hash_chain(
            rows,
            lambda row: {"event_id": row[1], "session_id": row[2],
                         "turn_id": row[3], "kind": row[4], "actor": row[5],
                         "trace_id": row[6], "payload": json.loads(row[7]),
                         "created": row[10]},
            lambda row: row[8], lambda row: row[9],
            label="Agent turn 事件链", seq_for=lambda row: row[0])
        if not integrity["ok"]:
            return integrity
        projected = {}
        latest_digest = {}
        identities = {}
        targets = {"turn.started": "running", "turn.completed": "completed",
                   "turn.failed": "failed", "turn.cancelled": "interrupted",
                   "turn.interrupted": "interrupted"}
        for row in rows:
            current = projected.get(row[3])
            if row[4] == "turn.started":
                if current is not None:
                    return {"ok": False, "events": row[0] - 1,
                            "reason": f"Turn 重复启动: {row[3]}"}
            elif current != "running":
                return {"ok": False, "events": row[0] - 1,
                        "reason": f"Turn 状态重放失败: {row[3]}/{row[4]}"}
            projected[row[3]] = targets[row[4]]
            payload = json.loads(row[7])
            if "turn_projection_sha256" in payload:
                latest_digest[row[3]] = payload["turn_projection_sha256"]
            if "turn_identity_sha256" in payload:
                previous_identity = identities.setdefault(
                    row[3], payload["turn_identity_sha256"])
                if previous_identity != payload["turn_identity_sha256"]:
                    return {"ok": False, "events": row[0] - 1,
                            "reason": f"Turn 不可变字段跨事件不一致: {row[3]}"}
        actual = {row[0]: row[1] for row in self._conn.execute(
            "SELECT turn_id,status FROM turns WHERE turn_id IN "
            "(SELECT DISTINCT turn_id FROM turn_events)")}
        if actual != projected:
            return {"ok": False, "events": integrity["events"],
                    "reason": "Turn 快照与事件重放不一致"}
        for turn_id, expected in latest_digest.items():
            snapshot = self._conn.execute(
                "SELECT session_id,idempotency_key,actor,trace_id,parent_turn_id,"
                "user_message,status,assistant_message,error_json FROM turns WHERE turn_id=?",
                (turn_id,)).fetchone()
            if (snapshot is None or self._turn_projection_hash(snapshot) != expected
                    or (turn_id in identities and
                        self._turn_identity_hash(snapshot) != identities[turn_id])):
                return {"ok": False, "events": integrity["events"],
                        "reason": f"Turn 内容快照与事件重放不一致: {turn_id}"}
        return integrity

    def model_messages(self, session_id, current_turn_id=None, limit=24):
        """Build provider-compatible history, including settled tool outcomes.

        Never serialize an unapproved proposal as a tool result. Preserve whole
        assistant/tool groups when trimming history so tool_call_id references
        cannot be orphaned by a message-count cutoff.
        """
        integrity = self.verify_turn_events()
        if not integrity["ok"]:
            raise ProtocolError(integrity["reason"])
        tool_integrity = self.verify_tool_events()
        if not tool_integrity["ok"]:
            raise ProtocolError(tool_integrity["reason"])
        calls_by_turn = {}
        for call in self.tool_calls(session_id):
            calls_by_turn.setdefault(call["turn_id"], []).append(call)
        groups = []
        for turn in self.turns(session_id):
            if turn["status"] == "completed":
                group = ([] if turn["actor"] == "agent_continuation" else
                         [{"role": "user", "content": turn["user_message"]}])
                calls = calls_by_turn.get(turn["turn_id"], [])
                settled = {"completed", "failed", "rejected", "cancelled", "interrupted", "invalid"}
                if calls and all(call["status"] in settled for call in calls):
                    # Keep the entire group, including manually added calls.
                    # The context assembler may omit older complete groups or
                    # reject an oversized required group, never orphan a call.
                    visible = calls
                    content = turn["assistant_message"] or ""
                    group.append({
                        "role": "assistant", "content": content,
                        "tool_calls": [{
                            "id": call["call_id"], "type": "function",
                            "function": {
                                "name": call["tool_name"].replace(".", "_"),
                                "arguments": _model_call_arguments(call),
                            },
                        } for call in visible],
                    })
                    for call in visible:
                        outcome = {"status": call["status"], "call_id": call["call_id"]}
                        if call["status"] == "invalid":
                            outcome["executed"] = False
                        if call["decision_reason"]:
                            outcome["decision_reason"] = call["decision_reason"]
                        if call["result"] is not None:
                            outcome["result"] = summarize_tool_result(call["result"], call["call_id"])
                        if call["error"] is not None:
                            outcome["error"] = redact(call["error"])
                        group.append({"role": "tool", "tool_call_id": call["call_id"],
                                      "content": json.dumps(outcome, ensure_ascii=False,
                                                            sort_keys=True, separators=(",", ":"))})
                else:
                    content = turn["assistant_message"]
                    if calls:
                        content += "\n[Popper: tool proposals are not all settled; no tool result is available yet.]"
                    group.append({"role": "assistant", "content": content})
                groups.append(group)
            elif (turn["turn_id"] == current_turn_id and turn["status"] == "running"
                  and turn["actor"] != "agent_continuation"):
                groups.append([{"role": "user", "content": turn["user_message"]}])
        while len(groups) > 1 and sum(len(group) for group in groups) > limit:
            groups.pop(0)
        return [message for group in groups for message in group]

    def chat_messages(self, session_id):
        messages = []
        for turn in self.turns(session_id):
            if turn["actor"] != "agent_continuation":
                messages.append({"id": turn["turn_id"] + ":user", "role": "user",
                                 "text": turn["user_message"], "ts": turn["created"]})
            if turn["status"] == "completed":
                messages.append({"id": turn["turn_id"] + ":assistant", "role": "assistant",
                                 "text": turn["assistant_message"], "ts": turn["updated"]})
            elif turn["status"] in {"failed", "interrupted"}:
                messages.append({"id": turn["turn_id"] + ":assistant", "role": "assistant",
                                 "text": "模型调用失败（已如实记录，非模型回复）",
                                 "ts": turn["updated"],
                                 "error": turn["error"]})
        return messages

    # ---- Structured tool calls -----------------------------------------
    @staticmethod
    def _decode_tool_call(row):
        result = dict(row)
        result["arguments"] = json.loads(result.pop("arguments_json"))
        encoded_result = result.pop("result_json")
        encoded_error = result.pop("error_json")
        result["result"] = json.loads(encoded_result) if encoded_result else None
        result["error"] = json.loads(encoded_error) if encoded_error else None
        return result

    def get_tool_call(self, call_id):
        parse_id(call_id, EntityKind.TOOL_CALL)
        integrity = self.verify_tool_events()
        if not integrity["ok"]:
            raise ProtocolError(integrity["reason"])
        row = self._conn.execute(
            "SELECT * FROM tool_calls WHERE call_id=?", (call_id,)).fetchone()
        if row is None:
            raise ToolCallNotFound(f"tool call 不存在: {call_id}")
        return self._decode_tool_call(row)

    def read_tool_result_details(self, call_id, field_path=None):
        """Read a redacted terminal receipt; caller must enforce disclosure scope."""
        call = self.get_tool_call(call_id)
        if call["status"] != "completed" or call["result"] is None:
            raise ProtocolError("tool detail 必须来自已有 completed 结果")
        return read_detail(call["result"], field_path)

    def tool_calls(self, session_id):
        self.get_session(session_id)
        integrity = self.verify_tool_events()
        if not integrity["ok"]:
            raise ProtocolError(integrity["reason"])
        return [self._decode_tool_call(row) for row in self._conn.execute(
            "SELECT * FROM tool_calls WHERE session_id=? ORDER BY created,call_id",
            (session_id,))]

    def running_tool_calls(self):
        integrity = self.verify_tool_events()
        if not integrity["ok"]:
            raise ProtocolError(integrity["reason"])
        return [self._decode_tool_call(row) for row in self._conn.execute(
            "SELECT * FROM tool_calls WHERE status='running' ORDER BY created,call_id")]

    def propose_tool_call(self, session_id, turn_id, tool_name, arguments, risk,
                          idempotency_key, *, actor, trace_id):
        self.get_session(session_id)
        turn = self.get_turn(turn_id)
        if turn["session_id"] != session_id:
            raise ProtocolError("turn 不属于目标 session")
        if (not isinstance(tool_name, str) or not tool_name.strip()
                or not isinstance(arguments, dict) or risk not in {"read", "disclose", "write", "execute"}
                or not isinstance(idempotency_key, str)
                or not 8 <= len(idempotency_key) <= 128
                or not isinstance(actor, str) or not actor.strip()
                or not isinstance(trace_id, str) or not trace_id.startswith("trc_")):
            raise ProtocolError("tool call 提议字段不合法")
        encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        self._conn.begin(write=True)
        try:
            prior = self._conn.execute(
                "SELECT * FROM tool_calls WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if prior is not None:
                decoded = self._decode_tool_call(prior)
                if (decoded["session_id"] != session_id or decoded["turn_id"] != turn_id
                        or decoded["tool_name"] != tool_name
                        or decoded["arguments"] != arguments
                        or decoded["proposed_by"] != actor):
                    raise SessionIdempotencyConflict(
                        "相同 idempotency_key 对应不同 tool call")
                self._conn.commit()
                return decoded, True
            if self._conn.execute(
                    "SELECT 1 FROM turns WHERE parent_turn_id=? "
                    "AND actor='agent_continuation' LIMIT 1",
                    (turn_id,)).fetchone():
                raise ToolCallConflict("该 turn 已继续推理，不能追加工具提议")
            call_id, now = new_id(EntityKind.TOOL_CALL), _now()
            from .research.envelope import covering_envelope
            envelope = None if risk == "read" else covering_envelope(self.root, tool_name, arguments)
            status = "approved" if risk == "read" or envelope is not None else "pending_approval"
            event_actor = f"envelope:{envelope['envelope_sha256']}" if envelope is not None else actor
            self._conn.execute(
                "INSERT INTO tool_calls(call_id,session_id,turn_id,idempotency_key,"
                "tool_name,arguments_json,risk,status,proposed_by,trace_id,created,updated,"
                "integrity_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)",
                (call_id, session_id, turn_id, idempotency_key, tool_name, encoded,
                 risk, status, actor, trace_id, now, now))
            self._append_tool_event(call_id, actor=event_actor, now=now)
            self._refresh_continuation_state(turn_id)
            self._conn.commit()
            return self.get_tool_call(call_id), False
        except Exception:
            self._conn.rollback()
            raise

    def decide_tool_call(self, call_id, decision, *, actor, reason):
        if decision not in {"approve", "reject"}:
            raise ProtocolError("tool call decision 必须为 approve 或 reject")
        if (not isinstance(actor, str) or not actor.strip()
                or not isinstance(reason, str) or not reason.strip()):
            raise ProtocolError("tool call 决策必须记录 actor 与 reason")
        self._conn.begin(write=True)
        try:
            call = self.get_tool_call(call_id)
            target = "approved" if decision == "approve" else "rejected"
            if call["status"] != "pending_approval":
                if (call["status"] == target and call["decided_by"] == actor
                        and call["decision_reason"] == reason.strip()):
                    self._conn.commit()
                    return call
                raise ToolCallConflict(f"tool call 当前状态不能决策: {call['status']}")
            now = _now()
            self._conn.execute(
                "UPDATE tool_calls SET status=?,decided_by=?,decision_reason=?,updated=? "
                "WHERE call_id=?", (target, actor, reason.strip(), now, call_id))
            self._append_tool_event(call_id, actor=actor, now=now)
            self._refresh_continuation_state(call["turn_id"])
            self._conn.commit()
            return self.get_tool_call(call_id)
        except Exception:
            self._conn.rollback()
            raise

    def claim_tool_call(self, call_id):
        self._conn.begin(write=True)
        try:
            call = self.get_tool_call(call_id)
            if call["status"] != "approved":
                raise ToolCallConflict(f"tool call 未获批准: {call['status']}")
            self._conn.execute(
                "UPDATE tool_calls SET status='running',updated=? WHERE call_id=?",
                (_now(), call_id))
            self._append_tool_event(call_id, actor="system")
            self._refresh_continuation_state(call["turn_id"])
            self._conn.commit()
            return self.get_tool_call(call_id)
        except Exception:
            self._conn.rollback()
            raise

    def finish_tool_call(self, call_id, *, result=None, error=None):
        if (result is None) == (error is None):
            raise ProtocolError("tool call 必须且只能提供 result 或 error")
        self._conn.begin(write=True)
        try:
            call = self.get_tool_call(call_id)
            if call["status"] != "running":
                raise ToolCallConflict(f"tool call 不在 running 状态: {call['status']}")
            status = "completed" if error is None else "failed"
            self._conn.execute(
                "UPDATE tool_calls SET status=?,result_json=?,error_json=?,updated=? "
                "WHERE call_id=?",
                (status,
                 json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None,
                 json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else None,
                 _now(), call_id))
            self._append_tool_event(call_id, actor="system")
            self._refresh_continuation_state(call["turn_id"])
            self._conn.commit()
            return self.get_tool_call(call_id)
        except Exception:
            self._conn.rollback()
            raise

    def cancel_tool_call(self, call_id, *, actor, reason):
        if not isinstance(actor, str) or not actor.strip() or not isinstance(reason, str):
            raise ProtocolError("tool call 取消字段不合法")
        self._conn.begin(write=True)
        try:
            call = self.get_tool_call(call_id)
            if call["status"] not in {"pending_approval", "approved"}:
                raise ToolCallConflict(f"tool call 当前状态不能取消: {call['status']}")
            self._conn.execute(
                "UPDATE tool_calls SET status='cancelled',decided_by=?,"
                "decision_reason=?,updated=? WHERE call_id=?",
                (actor, reason.strip(), _now(), call_id))
            self._append_tool_event(call_id, actor=actor)
            self._refresh_continuation_state(call["turn_id"])
            self._conn.commit()
            return self.get_tool_call(call_id)
        except Exception:
            self._conn.rollback()
            raise

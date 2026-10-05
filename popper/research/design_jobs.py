"""Durable, idempotent queue for approved frozen-design executions.

The queue records intent and outcome only. ResearchController remains the sole
authority for candidate registration, budgets, Runs and Observations.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from ..core import ProtocolError, canonical
from ..eventlog import GENESIS_HASH, chained_body, verify_hash_chain
from ..storage import SQLiteStorageAdapter


TERMINAL = {"succeeded", "failed", "interrupted", "cancelled"}


class DesignExecutionJobStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._storage = SQLiteStorageAdapter(self.path)
        self._conn = self._storage.connect()
        with self._conn.transaction(write=True):
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS design_execution_jobs(
                    job_id TEXT PRIMARY KEY,
                    design_id TEXT NOT NULL UNIQUE,
                    design_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    observation_id TEXT,
                    error TEXT,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL)
            """)
            columns = {row[1] for row in self._conn.execute(
                "PRAGMA table_info(design_execution_jobs)")}
            if "cancel_requested" not in columns:
                self._conn.execute(
                    "ALTER TABLE design_execution_jobs ADD COLUMN "
                    "cancel_requested INTEGER NOT NULL DEFAULT 0")
            if "attempt" not in columns:
                self._conn.execute(
                    "ALTER TABLE design_execution_jobs ADD COLUMN "
                    "attempt INTEGER NOT NULL DEFAULT 1")
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS design_execution_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    created TEXT NOT NULL)
            """)

    @classmethod
    def readonly(cls, path):
        """Inspect existing queue receipts without initialization or migration."""
        instance = cls.__new__(cls)
        instance.path = Path(path).resolve()
        instance._storage = SQLiteStorageAdapter(instance.path)
        instance._conn = instance._storage.connect_readonly()
        return instance

    def close(self):
        self._conn.close()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _event(self, job_id, event_type, payload):
        prior = self._conn.execute(
            "SELECT hash FROM design_execution_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        previous = prior[0] if prior else GENESIS_HASH
        created = self._now()
        _, event_hash = chained_body(
            {"job_id": job_id, "event_type": event_type, "payload": payload,
             "created": created}, previous)
        self._conn.execute(
            "INSERT INTO design_execution_events"
            "(job_id,event_type,payload,previous_hash,hash,created) VALUES(?,?,?,?,?,?)",
            (job_id, event_type, canonical(payload), previous, event_hash, created))

    def enqueue(self, design_id, design_sha256):
        if not isinstance(design_id, str) or not design_id:
            raise ProtocolError("设计执行 Job 缺少 design_id")
        if not isinstance(design_sha256, str) or not re.fullmatch(
                r"[0-9a-f]{64}", design_sha256):
            raise ProtocolError("设计执行 Job 的冻结快照摘要不合法")
        with self._conn.transaction(write=True):
            prior = self._conn.execute(
                "SELECT job_id,design_sha256 FROM design_execution_jobs WHERE design_id=?",
                (design_id,)).fetchone()
            if prior:
                if prior["design_sha256"] != design_sha256:
                    raise ProtocolError("同一设计已有不同冻结快照的执行 Job")
                return self.get(prior["job_id"])
            job_id = "JOBD-" + secrets.token_hex(12)
            now = self._now()
            self._conn.execute(
                "INSERT INTO design_execution_jobs"
                "(job_id,design_id,design_sha256,status,created,updated) "
                "VALUES(?,?,?,'queued',?,?)",
                (job_id, design_id, design_sha256, now, now))
            self._event(job_id, "queued", {"design_id": design_id,
                                            "design_sha256": design_sha256,
                                            "attempt": 1})
        return self.get(job_id)

    def claim(self, job_id):
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE design_execution_jobs SET status='running',updated=? "
                "WHERE job_id=? AND status='queued'", (self._now(), job_id))
            if changed.rowcount != 1:
                raise ProtocolError("设计执行 Job 非 queued 或已被领取")
            self._event(job_id, "started", {})
        return self.get(job_id)

    def finish(self, job_id, *, observation_id=None, error=None):
        if (observation_id is None) == (error is None):
            raise ProtocolError("设计执行 Job 终态必须且只能有 observation_id 或 error")
        status = "succeeded" if observation_id is not None else "failed"
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE design_execution_jobs SET status=?,observation_id=?,error=?,"
                "updated=? WHERE job_id=? AND status='running'",
                (status, observation_id, error, self._now(), job_id))
            if changed.rowcount != 1:
                raise ProtocolError("只有运行中的设计执行 Job 可以结束")
            self._event(job_id, status, {"observation_id": observation_id,
                                          "error": error})
        return self.get(job_id)

    def cancel(self, job_id, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ProtocolError("设计执行取消原因不合法")
        reason = reason.strip()
        with self._conn.transaction(write=True):
            job = self.get(job_id)
            if job["status"] in TERMINAL or job["cancel_requested"]:
                return job
            if job["status"] == "queued":
                self._conn.execute(
                    "UPDATE design_execution_jobs SET status='cancelled',"
                    "cancel_requested=1,error=?,updated=? WHERE job_id=?",
                    (reason, self._now(), job_id))
                self._event(job_id, "cancelled", {"reason": reason})
            elif job["status"] == "running":
                self._conn.execute(
                    "UPDATE design_execution_jobs SET cancel_requested=1,error=?,"
                    "updated=? WHERE job_id=?", (reason, self._now(), job_id))
                self._event(job_id, "cancel_requested", {"reason": reason})
            else:
                raise ProtocolError("设计执行 Job 状态不合法")
        return self.get(job_id)

    def finish_cancelled(self, job_id):
        with self._conn.transaction(write=True):
            job = self.get(job_id)
            if job["status"] != "running" or not job["cancel_requested"]:
                raise ProtocolError("只有收到取消请求的运行中设计 Job 可以取消")
            self._conn.execute(
                "UPDATE design_execution_jobs SET status='cancelled',updated=? "
                "WHERE job_id=?", (self._now(), job_id))
            self._event(job_id, "cancelled", {"reason": job["error"]})
        return self.get(job_id)

    def retry_cancelled(self, job_id, expected_attempt):
        if type(expected_attempt) is not int or expected_attempt < 1:
            raise ProtocolError("设计执行重试次数不合法")
        with self._conn.transaction(write=True):
            job = self.get(job_id)
            if job["attempt"] == expected_attempt + 1:
                return job, False  # Same retry request already applied.
            if job["attempt"] != expected_attempt:
                raise ProtocolError("设计执行 Job 重试次数已变化")
            if job["status"] != "cancelled":
                raise ProtocolError("只有已取消的设计执行 Job 可以重试")
            next_attempt = expected_attempt + 1
            self._conn.execute(
                "UPDATE design_execution_jobs SET status='queued',attempt=?,"
                "cancel_requested=0,observation_id=NULL,error=NULL,updated=? "
                "WHERE job_id=?", (next_attempt, self._now(), job_id))
            self._event(job_id, "retried", {"attempt": next_attempt})
        return self.get(job_id), True

    def interrupt_running(self):
        """Never silently replay a job after workstation restart."""
        with self._conn.transaction(write=True):
            rows = self._conn.execute(
                "SELECT job_id FROM design_execution_jobs WHERE status='running'"
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE design_execution_jobs SET status='interrupted',"
                    "error='workstation restarted during execution',updated=? "
                    "WHERE job_id=?", (self._now(), row[0]))
                self._event(row[0], "interrupted", {"reason": "workstation_restart"})
        return len(rows)

    def get(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"JOBD-[0-9a-f]{24}", job_id):
            raise ProtocolError("设计执行 job_id 格式不合法")
        row = self._conn.execute(
            "SELECT * FROM design_execution_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ProtocolError("设计执行 Job 不存在")
        return dict(row)

    def for_design(self, design_id):
        row = self._conn.execute(
            "SELECT job_id FROM design_execution_jobs WHERE design_id=?",
            (design_id,)).fetchone()
        return self.get(row[0]) if row else None

    def list(self):
        return [dict(row) for row in self._conn.execute(
            "SELECT * FROM design_execution_jobs ORDER BY created,job_id")]

    def verify(self):
        rows = list(self._conn.execute(
            "SELECT seq,job_id,event_type,payload,previous_hash,hash,created "
            "FROM design_execution_events ORDER BY seq"))
        integrity = verify_hash_chain(
            rows,
            lambda row: {"job_id": row[1], "event_type": row[2],
                         "payload": json.loads(row[3]), "created": row[6]},
            lambda row: row[4], lambda row: row[5],
            label="design job 事件链", seq_for=lambda row: row[0])
        if not integrity["ok"]:
            return integrity
        projected = {}
        transitions = {"queued": (None, "queued"),
                       "started": ("queued", "running"),
                       "succeeded": ("running", "succeeded"),
                       "failed": ("running", "failed"),
                       "interrupted": ("running", "interrupted")}
        for row in rows:
            job_id, event = row[1], row[2]
            if event not in transitions and event not in {
                    "cancel_requested", "cancelled", "retried"}:
                return {"ok": False, "reason": "未知设计 Job 事件"}
            payload = json.loads(row[3])
            current = projected.get(job_id, {}).get("status")
            if event == "retried":
                if (current != "cancelled" or type(payload.get("attempt")) is not int
                        or payload["attempt"] != projected[job_id]["attempt"] + 1):
                    return {"ok": False, "reason": "设计 Job 重试顺序不合法"}
                projected[job_id].update(
                    status="queued", attempt=payload["attempt"],
                    cancel_requested=0, observation_id=None, error=None)
                continue
            if event == "cancel_requested":
                if current != "running" or projected[job_id]["cancel_requested"]:
                    return {"ok": False, "reason": "设计 Job 取消请求顺序不合法"}
                projected[job_id]["cancel_requested"] = 1
                projected[job_id]["error"] = payload["reason"]
                continue
            if event == "cancelled":
                if current not in {"queued", "running"}:
                    return {"ok": False, "reason": "设计 Job 取消顺序不合法"}
                if current == "running" and not projected[job_id]["cancel_requested"]:
                    return {"ok": False, "reason": "设计 Job 运行取消缺少请求"}
                projected[job_id]["status"] = "cancelled"
                projected[job_id]["cancel_requested"] = 1
                projected[job_id]["error"] = payload["reason"]
                continue
            expected, status = transitions[event]
            if current != expected:
                return {"ok": False, "reason": "设计 Job 状态事件顺序不合法"}
            if event == "queued":
                projected[job_id] = {"status": status,
                                     "design_id": payload["design_id"],
                                     "design_sha256": payload["design_sha256"],
                                     "observation_id": None, "error": None,
                                     "cancel_requested": 0,
                                     "attempt": payload.get("attempt", 1)}
            else:
                projected[job_id]["status"] = status
                if event in {"succeeded", "failed"}:
                    projected[job_id]["observation_id"] = payload["observation_id"]
                    projected[job_id]["error"] = payload["error"]
                elif event == "interrupted":
                    projected[job_id]["error"] = "workstation restarted during execution"
        snapshots = {job["job_id"]: job for job in self.list()}
        if set(projected) != set(snapshots):
            return {"ok": False, "reason": "设计 Job 事件与快照集合不一致"}
        for job_id, expected in projected.items():
            if any(snapshots[job_id][key] != value for key, value in expected.items()):
                return {"ok": False, "reason": f"设计 Job 快照与事件不一致: {job_id}"}
        return integrity

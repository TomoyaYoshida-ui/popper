"""Persistent queue for approved revision executions.

The queue deliberately does not execute research itself.  It leases durable jobs
to the workstation, while :class:`ResearchController` remains the sole execution
authority.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from ..core import ProtocolError, canonical
from ..eventlog import GENESIS_HASH, chained_body, verify_hash_chain
from ..storage import SQLiteStorageAdapter, SQLiteConnection


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


class RevisionExecutionJobStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._storage = SQLiteStorageAdapter(self.path)
        self._conn = self._storage.connect()
        with self._conn.transaction(write=True):
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS revision_execution_jobs(
                    job_id TEXT PRIMARY KEY,
                    worktree_id TEXT NOT NULL UNIQUE,
                    revision_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    run_id TEXT,
                    error TEXT,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL,
                    started TEXT,
                    finished TEXT)
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS revision_execution_job_events(
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
        """Inspect durable receipts without creating a database or schema."""
        instance = cls.__new__(cls)
        instance.path = Path(path).resolve()
        if not instance.path.is_file():
            raise FileNotFoundError("Revision execution queue does not exist")
        connection = sqlite3.connect(instance.path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        instance._conn = SQLiteConnection(connection)
        return instance

    @staticmethod
    def _now():
        return datetime.now().astimezone().isoformat(timespec="seconds")

    def close(self):
        self._conn.close()

    def _event(self, job_id, event_type, payload):
        prior = self._conn.execute(
            "SELECT hash FROM revision_execution_job_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        previous = prior[0] if prior else GENESIS_HASH
        created = self._now()
        body, event_hash = chained_body(
            {"job_id": job_id, "event_type": event_type, "payload": payload,
             "created": created}, previous)
        self._conn.execute(
            "INSERT INTO revision_execution_job_events"
            "(job_id,event_type,payload,previous_hash,hash,created) VALUES(?,?,?,?,?,?)",
            (job_id, event_type, canonical(payload), previous, event_hash, created))

    def enqueue(self, worktree_id, revision_id):
        prior = self._conn.execute(
            "SELECT job_id,revision_id FROM revision_execution_jobs WHERE worktree_id=?",
            (worktree_id,)).fetchone()
        if prior:
            if prior["revision_id"] != revision_id:
                raise ProtocolError("worktree 已绑定不同 revision execution job")
            return self.get(prior["job_id"])
        job_id = "JOBR-" + secrets.token_hex(12)
        now = self._now()
        with self._conn.transaction(write=True):
            prior = self._conn.execute(
                "SELECT job_id,revision_id FROM revision_execution_jobs WHERE worktree_id=?",
                (worktree_id,)).fetchone()
            if prior:
                if prior["revision_id"] != revision_id:
                    raise ProtocolError("worktree 已绑定不同 revision execution job")
                return self.get(prior["job_id"])
            self._conn.execute(
                "INSERT INTO revision_execution_jobs"
                "(job_id,worktree_id,revision_id,status,phase,progress,created,updated) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (job_id, worktree_id, revision_id, "queued", "queued", 0, now, now))
            self._event(job_id, "queued", {"worktree_id": worktree_id,
                                             "revision_id": revision_id})
        return self.get(job_id)

    def claim(self, job_id):
        job = self.get(job_id)
        if job["status"] == "running":
            raise ProtocolError("revision job 已由其他执行器领取")
        if job["status"] != "queued":
            raise ProtocolError("只有 queued revision job 可以领取")
        now = self._now()
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_execution_jobs SET status='running',phase='preparing',"
                "progress=10,started=COALESCE(started,?),updated=? "
                "WHERE job_id=? AND status='queued' AND cancel_requested=0",
                (now, now, job_id))
            if changed.rowcount != 1:
                raise ProtocolError("revision job 已取消或被其他执行器领取")
            self._event(job_id, "started", {})
        return self.get(job_id)

    def progress(self, job_id, phase, percent):
        if not isinstance(phase, str) or not phase or not 0 <= percent <= 99:
            raise ProtocolError("revision job 进度不合法")
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_execution_jobs SET phase=?,progress=?,updated=? "
                "WHERE job_id=? AND status='running'",
                (phase, percent, self._now(), job_id))
            if changed.rowcount != 1:
                raise ProtocolError("revision job 不在运行中")
            self._event(job_id, "progress", {"phase": phase, "progress": percent})
        return self.get(job_id)

    def finish(self, job_id, *, run_id=None, error=None):
        if (run_id is None) == (error is None):
            raise ProtocolError("revision job 终态必须且只能包含 run_id 或 error")
        status = "succeeded" if run_id is not None else "failed"
        phase = "completed" if run_id is not None else "failed"
        now = self._now()
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_execution_jobs SET status=?,phase=?,progress=100,run_id=?,"
                "error=?,finished=?,updated=? WHERE job_id=? AND status='running'",
                (status, phase, run_id, error, now, now, job_id))
            if changed.rowcount != 1:
                return self.get(job_id)
            self._event(job_id, status, {"run_id": run_id, "error": error})
        return self.get(job_id)

    def finish_cancelled(self, job_id, reason="execution cancelled"):
        now = self._now()
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE revision_execution_jobs SET status='cancelled',phase='cancelled',"
                "progress=100,cancel_requested=1,error=?,finished=?,updated=? "
                "WHERE job_id=? AND status='running'", (reason, now, now, job_id))
            if changed.rowcount == 1:
                self._event(job_id, "cancelled", {"reason": reason})
        return self.get(job_id)

    def cancel(self, job_id, reason):
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
            raise ProtocolError("取消原因不合法")
        now = self._now()
        with self._conn.transaction(write=True):
            job = self.get(job_id)
            if job["status"] in TERMINAL:
                return job
            if job["status"] == "queued":
                self._conn.execute(
                    "UPDATE revision_execution_jobs SET status='cancelled',phase='cancelled',"
                    "cancel_requested=1,error=?,finished=?,updated=? WHERE job_id=?",
                    (reason.strip(), now, now, job_id))
                self._event(job_id, "cancelled", {"reason": reason.strip()})
            else:
                self._conn.execute(
                    "UPDATE revision_execution_jobs SET cancel_requested=1,updated=? WHERE job_id=?",
                    (now, job_id))
                self._event(job_id, "cancel_requested", {"reason": reason.strip()})
        return self.get(job_id)

    def interrupt_running(self):
        """Make crash recovery explicit; queued jobs remain resumable."""
        now = self._now()
        with self._conn.transaction(write=True):
            rows = self._conn.execute(
                "SELECT job_id FROM revision_execution_jobs WHERE status='running'").fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE revision_execution_jobs SET status='interrupted',phase='interrupted',"
                    "error='workstation restarted during execution',finished=?,updated=? "
                    "WHERE job_id=? AND status='running'", (now, now, row[0]))
                self._event(row[0], "interrupted", {"reason": "workstation_restart"})
        return len(rows)

    def get(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"JOBR-[0-9a-f]{24}", job_id):
            raise ProtocolError("revision job_id 格式不合法")
        row = self._conn.execute(
            "SELECT * FROM revision_execution_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ProtocolError("revision execution job 不存在")
        value = dict(row)
        value["cancel_requested"] = bool(value["cancel_requested"])
        return value

    def for_worktree(self, worktree_id):
        row = self._conn.execute(
            "SELECT job_id FROM revision_execution_jobs WHERE worktree_id=?",
            (worktree_id,)).fetchone()
        return self.get(row[0]) if row is not None else None

    def list(self):
        return [dict(row) | {"cancel_requested": bool(row["cancel_requested"])}
                for row in self._conn.execute(
                    "SELECT * FROM revision_execution_jobs ORDER BY created,job_id")]

    def history(self, job_id):
        self.get(job_id)
        return [{"seq": row[0], "event_type": row[1], "payload": json.loads(row[2]),
                 "previous_hash": row[3], "hash": row[4], "created": row[5]}
                for row in self._conn.execute(
                    "SELECT seq,event_type,payload,previous_hash,hash,created "
                    "FROM revision_execution_job_events WHERE job_id=? ORDER BY seq", (job_id,))]

    def verify(self):
        rows = list(self._conn.execute(
            "SELECT seq,job_id,event_type,payload,previous_hash,hash,created "
            "FROM revision_execution_job_events ORDER BY seq"))
        integrity = verify_hash_chain(
            rows,
            lambda row: {"job_id": row[1], "event_type": row[2],
                         "payload": json.loads(row[3]), "created": row[6]},
            lambda row: row[4], lambda row: row[5],
            label="revision job 事件链", seq_for=lambda row: row[0])
        if not integrity["ok"]:
            return integrity
        count = 0
        projected = {}
        for row in rows:
            payload = json.loads(row[3])
            event = row[2]
            current = projected.get(row[1])
            expected = {
                "queued": {None}, "started": {"queued"}, "progress": {"running"},
                "cancel_requested": {"running"}, "succeeded": {"running"},
                "failed": {"running"}, "cancelled": {"queued", "running"},
                "interrupted": {"running"},
            }.get(event)
            target = {
                "queued": "queued", "started": "running", "progress": "running",
                "cancel_requested": "running", "succeeded": "succeeded",
                "failed": "failed", "cancelled": "cancelled",
                "interrupted": "interrupted",
            }.get(event)
            if expected is None or current not in expected:
                return {"ok": False,
                        "reason": f"revision job 状态重放失败: {row[1]}/{event}"}
            projected[row[1]] = target
            count += 1
        actual = {row[0]: row[1] for row in self._conn.execute(
            "SELECT job_id,status FROM revision_execution_jobs")}
        if actual != projected:
            return {"ok": False, "reason": "revision job 快照与事件重放不一致"}
        return {"ok": True, "events": count, "head": integrity["head"]}

"""Durable, audited workbench jobs for explicit cross-domain benchmarks."""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from ..core import ProtocolError, canonical
from ..eventlog import GENESIS_HASH, chained_body, verify_hash_chain
from ..storage import SQLiteStorageAdapter


class BenchmarkJobStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self._conn = SQLiteStorageAdapter(self.path).connect()
        with self._conn.transaction(write=True):
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_jobs(
                    job_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    output_dir TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    error TEXT,
                    created TEXT NOT NULL,
                    updated TEXT NOT NULL)
            """)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS benchmark_job_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    hash TEXT NOT NULL,
                    created TEXT NOT NULL)
            """)

    def close(self):
        self._conn.close()

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _event(self, job_id, event_type, payload):
        row = self._conn.execute(
            "SELECT hash FROM benchmark_job_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = row[0] if row else GENESIS_HASH
        created = self._now()
        _, event_hash = chained_body(
            {"job_id": job_id, "event_type": event_type, "payload": payload,
             "created": created}, previous)
        self._conn.execute(
            "INSERT INTO benchmark_job_events"
            "(job_id,event_type,payload,previous_hash,hash,created) VALUES(?,?,?,?,?,?)",
            (job_id, event_type, canonical(payload), previous, event_hash, created))

    def enqueue(self, idempotency_key, output_dir):
        if (not isinstance(idempotency_key, str)
                or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", idempotency_key)):
            raise ProtocolError("基准 Job 幂等键不合法")
        output = str(Path(output_dir).resolve())
        with self._conn.transaction(write=True):
            prior = self._conn.execute(
                "SELECT job_id,output_dir FROM benchmark_jobs WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            if prior:
                if prior["output_dir"] != output:
                    raise ProtocolError("同一幂等键已绑定不同基准目录")
                return self.get(prior["job_id"]), False
            if Path(output).exists() or self._conn.execute(
                    "SELECT 1 FROM benchmark_jobs WHERE output_dir=?", (output,)).fetchone():
                raise ProtocolError("基准输出目录已存在或已被其他 Job 使用")
            job_id = "JOBB-" + secrets.token_hex(12)
            now = self._now()
            self._conn.execute(
                "INSERT INTO benchmark_jobs"
                "(job_id,idempotency_key,output_dir,status,created,updated) "
                "VALUES(?,?,?,'queued',?,?)",
                (job_id, idempotency_key, output, now, now))
            self._event(job_id, "queued", {"idempotency_key": idempotency_key,
                                            "output_dir": output})
        return self.get(job_id), True

    def claim(self, job_id):
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE benchmark_jobs SET status='running',updated=? "
                "WHERE job_id=? AND status='queued'", (self._now(), job_id))
            if changed.rowcount != 1:
                raise ProtocolError("基准 Job 不是 queued 或已被领取")
            self._event(job_id, "started", {})
        return self.get(job_id)

    def finish(self, job_id, error=None):
        status = "succeeded" if error is None else "failed"
        with self._conn.transaction(write=True):
            changed = self._conn.execute(
                "UPDATE benchmark_jobs SET status=?,error=?,updated=? "
                "WHERE job_id=? AND status='running'",
                (status, error, self._now(), job_id))
            if changed.rowcount != 1:
                raise ProtocolError("只有运行中的基准 Job 可以结束")
            self._event(job_id, status, {"error": error})
        return self.get(job_id)

    def interrupt_running(self):
        with self._conn.transaction(write=True):
            rows = self._conn.execute(
                "SELECT job_id FROM benchmark_jobs WHERE status='running'").fetchall()
            for row in rows:
                self._conn.execute(
                    "UPDATE benchmark_jobs SET status='interrupted',"
                    "error='workstation restarted during benchmark',updated=? "
                    "WHERE job_id=?", (self._now(), row[0]))
                self._event(row[0], "interrupted",
                            {"error": "workstation restarted during benchmark"})
        return len(rows)

    def get(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"JOBB-[0-9a-f]{24}", job_id):
            raise ProtocolError("基准 job_id 格式不合法")
        row = self._conn.execute(
            "SELECT * FROM benchmark_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise LookupError("基准 Job 不存在")
        return dict(row)

    def list(self):
        return [dict(row) for row in self._conn.execute(
            "SELECT * FROM benchmark_jobs ORDER BY created,job_id")]

    def verify(self):
        rows = list(self._conn.execute(
            "SELECT seq,job_id,event_type,payload,previous_hash,hash,created "
            "FROM benchmark_job_events ORDER BY seq"))
        integrity = verify_hash_chain(
            rows,
            lambda row: {"job_id": row[1], "event_type": row[2],
                         "payload": json.loads(row[3]), "created": row[6]},
            lambda row: row[4], lambda row: row[5],
            label="benchmark job 事件链", seq_for=lambda row: row[0])
        if not integrity["ok"]:
            return integrity
        projected = {}
        for row in rows:
            job_id, event, payload = row[1], row[2], json.loads(row[3])
            current = projected.get(job_id, {}).get("status")
            if event == "queued" and current is None:
                projected[job_id] = {"idempotency_key": payload["idempotency_key"],
                                     "output_dir": payload["output_dir"],
                                     "status": "queued", "error": None}
            elif event == "started" and current == "queued":
                projected[job_id]["status"] = "running"
            elif event in {"succeeded", "failed", "interrupted"} and current == "running":
                projected[job_id].update(status=event, error=payload["error"])
            else:
                return {"ok": False, "reason": "基准 Job 状态事件顺序不合法"}
        snapshots = {row["job_id"]: row for row in self.list()}
        if set(projected) != set(snapshots):
            return {"ok": False, "reason": "基准 Job 事件与快照集合不一致"}
        for job_id, expected in projected.items():
            if any(snapshots[job_id][key] != value for key, value in expected.items()):
                return {"ok": False, "reason": f"基准 Job 快照与事件不一致: {job_id}"}
        return integrity

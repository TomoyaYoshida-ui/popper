"""Durable Study lifecycle intentions; scientific facts remain in the controller."""
from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from ..core import Experiment, ProtocolError, canonical, digest, file_hash, read_json
from ..eventlog import GENESIS_HASH, chained_body, verify_hash_chain
from ..storage import SQLiteStorageAdapter
from .readout import read_research_state

TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}


def preview_study_action(run_dir, action, max_steps=1, envelope=None):
    if action not in {"run", "confirm", "autopilot"}:
        raise ProtocolError("未知 Study 动作")
    if type(max_steps) is not int or not 1 <= max_steps <= 100:
        raise ProtocolError("候选执行步数须为 1 到 100 的整数")
    run_dir = Path(run_dir)
    manifest = read_json(run_dir / "research.json")
    state = read_research_state(run_dir)
    if not state.get("ok") or not state["experiment_graph"]["integrity"]["ok"]:
        raise ProtocolError("Study 或证据图完整性检查失败")
    exp = Experiment(manifest["project"], readonly=True)
    try:
        core = exp.verify_inputs()
    finally:
        exp.close()
    if state["study"]["status"] != "active":
        raise ProtocolError("Study 已终结，不能继续执行")
    if core.get("test_consumed"):
        raise ProtocolError("确认资源已经消费，不能创建新的执行作业")
    allowed_phase = {"searching"} if action in {"run", "autopilot"} else {"searching", "frozen"}
    if core["phase"] not in allowed_phase:
        raise ProtocolError("当前实验阶段不允许创建此研究作业")
    decisions = state["decisions"]
    last_action = decisions[-1]["action"] if decisions else None
    selected = next((row for row in state["hypotheses"]
                     if row["hypothesis_id"] in manifest["candidate_hypothesis_ids"]
                     and row["status"] == "supported_in_scope"), None)
    ready = last_action == "request_confirmation"
    if action == "confirm":
        if manifest.get("external_confirmation"):
            raise ProtocolError("此 Study 使用外部签名确认，不能用本地确认替代")
        if not ready or selected is None:
            raise ProtocolError("Study 尚未达到独立确认条件")
        if state["budget"]["available"] < 2:
            raise ProtocolError("独立确认至少需要两次评分预算")
    elif ready or last_action == "request_scope_boundary_confirmation":
        raise ProtocolError("研究已请求独立确认，请审查确认方案")
    if action == "autopilot":
        from .envelope import build_envelope
        if not isinstance(envelope, dict):
            raise ProtocolError("自治实验需要包络")
        envelope = build_envelope(
            manifest["study_id"], max_dev_runs=envelope["max_dev_runs"],
            max_model_tokens=envelope["max_model_tokens"],
            max_wall_clock_minutes=envelope["max_wall_clock_minutes"],
            code_files=envelope["code_files"], state_version=state["state_version"],
            allowed_actions=envelope.get("allowed_actions"),
            approved_at=envelope.get("approved_at"))
        if envelope["max_dev_runs"] > max_steps:
            raise ProtocolError("包络里的开发运行上限不能超过本次步数")
    request = {"study_id": manifest["study_id"], "action": action,
               "max_steps": max_steps if action in {"run", "autopilot"} else 1,
               "state_version": state["state_version"],
               "manifest_sha256": file_hash(run_dir / "research.json"),
               "input_hashes": core["input_hashes"],
               "graph_sha256": digest(state["experiment_graph"]),
               "budget": state["budget"],
               "selected_hypothesis_id": selected["hypothesis_id"] if selected else None}
    if envelope is not None:
        request["envelope"] = envelope
    return {**request, "approval_sha256": digest(request),
            "objective": state["study"]["question"], "metric": manifest["metric"],
            "scope": ("registered_independent_confirmation" if action == "confirm"
                      else "dev_search" if action == "autopilot" else "dev"),
            "confirmation_cost": 2 if action == "confirm" else 0,
            "stop_conditions": ["用户停止", "步数达到上限", "预算耗尽", "策略停止或请求确认",
                                "自治包络超限"] if action == "autopilot" else
            ["用户停止", "步数达到上限", "预算耗尽", "策略停止或请求确认"],
            "confirmation_warning": ("确认数据只消费一次；执行开始后失败或中断不会自动重试"
                                     if action == "confirm" else None)}


class StudyJobStore:
    @classmethod
    def readonly(cls, run_dir):
        instance = cls.__new__(cls)
        instance.path = Path(run_dir) / "study-jobs.sqlite"
        instance.db = SQLiteStorageAdapter(instance.path).connect_readonly()
        return instance

    def __init__(self, run_dir):
        self.path = Path(run_dir) / "study-jobs.sqlite"
        self.db = SQLiteStorageAdapter(self.path).connect()
        with self.db.transaction(write=True):
            self.db.execute("CREATE TABLE IF NOT EXISTS study_jobs (job_id TEXT PRIMARY KEY, "
                            "request_key TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL)")
            self.db.execute("CREATE TABLE IF NOT EXISTS study_job_events (seq INTEGER PRIMARY KEY "
                            "AUTOINCREMENT, job_id TEXT NOT NULL, payload TEXT NOT NULL, "
                            "previous_hash TEXT NOT NULL, hash TEXT NOT NULL)")

    def close(self):
        self.db.close()

    def list(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT snapshot FROM study_jobs ORDER BY rowid")]

    def get(self, job_id):
        row = self.db.execute("SELECT snapshot FROM study_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ProtocolError("Study 作业不存在")
        return json.loads(row[0])

    def by_key(self, key):
        row = self.db.execute("SELECT snapshot FROM study_jobs WHERE request_key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _write(self, job):
        job = {**job, "updated": datetime.now(timezone.utc).isoformat()}
        prior = self.db.execute("SELECT hash FROM study_job_events ORDER BY seq DESC LIMIT 1").fetchone()
        previous = prior[0] if prior else GENESIS_HASH
        _, event_hash = chained_body(job, previous)
        self.db.execute("INSERT INTO study_jobs VALUES (?,?,?) ON CONFLICT(job_id) "
                        "DO UPDATE SET snapshot=excluded.snapshot",
                        (job["job_id"], job["idempotency_key"], canonical(job)))
        self.db.execute("INSERT INTO study_job_events(job_id,payload,previous_hash,hash) VALUES(?,?,?,?)",
                        (job["job_id"], canonical(job), previous, event_hash))
        return job

    def verify(self):
        rows = list(self.db.execute("SELECT seq,job_id,payload,previous_hash,hash FROM study_job_events ORDER BY seq"))
        integrity = verify_hash_chain(rows, lambda r: json.loads(r[2]), lambda r: r[3],
                                      lambda r: r[4], label="Study 作业", seq_for=lambda r: r[0])
        if not integrity["ok"]:
            raise ProtocolError(f"Study 作业事件链损坏: {integrity.get('reason')}")
        latest = {}
        transitions = {None: {"queued"}, "queued": {"running", "cancelled", "interrupted"},
                       "running": {"running", *TERMINAL}}
        for row in rows:
            job = json.loads(row[2])
            old = latest.get(row[1])
            if (job["job_id"] != row[1] or job["status"] not in transitions.get(
                    old["status"] if old else None, set())):
                raise ProtocolError("Study 作业状态转换不合法")
            if old and any(job[key] != old[key] for key in ("request", "idempotency_key", "created")):
                raise ProtocolError("Study 作业审批内容发生变化")
            latest[row[1]] = job
        snapshots = list(self.db.execute(
            "SELECT job_id,request_key,snapshot FROM study_jobs"))
        if any(json.loads(snapshot).get("job_id") != job_id or
               json.loads(snapshot).get("idempotency_key") != key
               for job_id, key, snapshot in snapshots):
            raise ProtocolError("Study 作业索引与快照不一致")
        if latest != {json.loads(snapshot)["job_id"]: json.loads(snapshot)
                      for _, _, snapshot in snapshots}:
            raise ProtocolError("Study 作业快照与事件链不一致")
        return integrity

    def enqueue(self, key, request):
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", key):
            raise ProtocolError("Study 作业幂等键不合法")
        with self.db.transaction(write=True):
            self.verify()
            old = self.by_key(key)
            if old:
                if old["request"] != request:
                    raise ProtocolError("幂等键已绑定不同 Study 请求")
                return old
            if any(job["status"] not in TERMINAL for job in self.list()):
                raise ProtocolError("此 Study 已有待执行或执行中的作业")
            return self._write({"job_id": "JOBS-" + secrets.token_hex(12),
                "idempotency_key": key, "request": request, "status": "queued",
                "cancel_requested": False, "error": None, "result": None,
                "created": datetime.now(timezone.utc).isoformat()})

    def claim(self, job_id):
        with self.db.transaction(write=True):
            self.verify()
            job = self.get(job_id)
            if job["status"] != "queued":
                raise ProtocolError("Study 作业已领取或结束")
            return self._write({**job, "status": "running"})

    def finish(self, job_id, status, *, result=None, error=None):
        if status not in TERMINAL:
            raise ProtocolError("Study 作业终态不合法")
        with self.db.transaction(write=True):
            self.verify()
            job = self.get(job_id)
            if job["status"] in TERMINAL:
                return job
            return self._write({**job, "status": status, "result": result, "error": error})

    def cancel(self, job_id):
        with self.db.transaction(write=True):
            self.verify()
            job = self.get(job_id)
            if job["status"] in TERMINAL or job["cancel_requested"]:
                return job
            return self._write({**job, "cancel_requested": True,
                               "status": "cancelled" if job["status"] == "queued" else "running"})

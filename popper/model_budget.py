"""Persistent reservation for every HTTP attempt, including retries and unknown usage."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from .core import ProtocolError, canonical


class ModelRequestBudget:
    def __init__(self, path, *, global_tokens, global_calls, scope, scope_tokens, scope_calls):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = scope
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS limits(scope TEXT PRIMARY KEY, tokens INTEGER, calls INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, scope TEXT, reserved INTEGER, "
                        "used INTEGER, status TEXT, request_hash TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)")
        with self.db:
            for name, tokens, calls in (("__global__", global_tokens, global_calls), (scope, scope_tokens, scope_calls)):
                if type(tokens) is not int or tokens < 1 or type(calls) is not int or calls < 1:
                    raise ProtocolError("模型预算必须是正整数")
                previous = self.db.execute("SELECT tokens,calls FROM limits WHERE scope=?", (name,)).fetchone()
                if previous is not None and previous != (tokens, calls):
                    raise ProtocolError("不能改变已登记的模型预算")
                self.db.execute("INSERT OR IGNORE INTO limits VALUES(?,?,?)", (name, tokens, calls))

    def _balance(self, scope):
        where, values = ("", ()) if scope == "__global__" else ("WHERE scope=?", (scope,))
        count, accounted = self.db.execute(
            "SELECT COUNT(*),COALESCE(SUM(COALESCE(used,reserved)),0) FROM requests " + where, values).fetchone()
        token_cap, call_cap = self.db.execute("SELECT tokens,calls FROM limits WHERE scope=?", (scope,)).fetchone()
        return {"calls": count, "accounted_tokens": accounted, "token_cap": token_cap, "call_cap": call_cap}

    def reserve(self, body):
        from .core import digest
        # UTF-8 byte length is a conservative token bound; reserve framing and hidden reasoning separately.
        input_bound = len(canonical(body.get("messages", [])).encode("utf-8")) + 4096
        output_bound = int(body.get("max_tokens", 8000)) + int(body.get("thinking_budget", 0))
        amount = input_bound + output_bound
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            unknown = self.db.execute("SELECT 1 FROM requests WHERE scope=? AND status IN ('reserved','unknown')",
                                      (self.scope,)).fetchone()
            if unknown:
                raise ProtocolError("上一调用用量未知或仍在执行，该运行停止调用模型")
            for scope in ("__global__", self.scope):
                balance = self._balance(scope)
                if balance["calls"] >= balance["call_cap"] or balance["accounted_tokens"] + amount > balance["token_cap"]:
                    raise ProtocolError("模型调用预算不足，发送前已停止")
            request_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO requests(id,scope,reserved,status,request_hash) VALUES(?,?,?,'reserved',?)",
                            (request_id, self.scope, amount, digest(body)))
            return request_id

    def settle(self, request_id, usage):
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if total is None and isinstance(usage, dict):
            a, b = usage.get("prompt_tokens"), usage.get("completion_tokens")
            if type(a) is int and type(b) is int and a >= 0 and b >= 0:
                total = a + b
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT scope,reserved,used,status FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None or row[0] != self.scope:
                raise ProtocolError("模型调用预留不存在")
            if row[3] != "reserved":
                if row[3] == "settled" and row[2] == total:
                    return
                raise ProtocolError("模型调用预留已结算或用量未知")
            if type(total) is not int or total < 0:
                self.db.execute("UPDATE requests SET status='unknown' WHERE id=?", (request_id,))
            else:
                status = "settled" if total <= row[1] else "unknown"
                self.db.execute("UPDATE requests SET used=?,status=? WHERE id=?", (total, status, request_id))
        if type(total) is not int or total < 0:
            raise ProtocolError("提供商缺少有效用量，保留预留并停止该运行")
        if total > row[1]:
            raise ProtocolError("提供商用量超过请求预留，停止该运行")

    def snapshot(self):
        with self._lock:
            rows = self.db.execute("SELECT id,scope,reserved,used,status,request_hash,created FROM requests ORDER BY rowid").fetchall()
            return {"global": self._balance("__global__"), "scope": self._balance(self.scope),
                    "attempts": [dict(zip(("id", "scope", "reserved_tokens", "used_tokens", "status", "request_sha256", "created"), r)) for r in rows]}

    @property
    def tokens_used(self):
        return self._balance(self.scope)["accounted_tokens"]

    def close(self):
        self.db.close()

"""Persistent reservation for every HTTP attempt, including retries and unknown usage."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path

from .core import ProtocolError, canonical


class ModelBudgetFailure(ProtocolError):
    def __init__(self, category, message):
        self.category = category
        super().__init__(message)


class ModelRequestBudget:
    @classmethod
    def readonly(cls, path, *, scope):
        """Inspect an existing ledger without migration, policy changes or sends."""
        instance = cls.__new__(cls)
        instance.path = Path(path).resolve()
        instance.scope = scope
        instance._lock = threading.Lock()
        instance.db = sqlite3.connect(instance.path.as_uri() + '?mode=ro', uri=True,
                                      check_same_thread=False)
        row = instance.db.execute('SELECT unknown_usage_policy FROM scope_policies WHERE scope=?', (scope,)).fetchone()
        if row is None:
            instance.db.close()
            raise ProtocolError('Ledger scope is not registered; audit cannot create it')
        instance.unknown_usage_policy = row[0]
        return instance

    def __init__(self, path, *, global_tokens, global_calls, scope, scope_tokens, scope_calls,
                 unknown_usage_policy='stop'):
        if unknown_usage_policy not in {'stop', 'reserve'}:
            raise ProtocolError('未知用量策略必须是 stop 或 reserve')
        self.unknown_usage_policy = unknown_usage_policy
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.scope = scope
        self._lock = threading.Lock()
        self.db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS limits(scope TEXT PRIMARY KEY, tokens INTEGER, calls INTEGER)")
        self.db.execute("CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, scope TEXT, reserved INTEGER, "
                        "used INTEGER, status TEXT, request_hash TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP)")
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(requests)')}
        additional = {'usage_status':'TEXT', 'usage_reason':'TEXT', 'usage_source':'TEXT',
                      'transport_phase':'TEXT', 'logical_request_id':'TEXT', 'attempt_index':'INTEGER',
                      'reported_tokens':'INTEGER', 'raw_usage_json':'TEXT', 'stream_completed':'INTEGER',
                      'output_started':'INTEGER'}
        for name, ddl in additional.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE requests ADD COLUMN {name} {ddl}')
        self.db.execute('CREATE TABLE IF NOT EXISTS scope_policies(scope TEXT PRIMARY KEY, unknown_usage_policy TEXT)')
        with self.db:
            previous_policy = self.db.execute('SELECT unknown_usage_policy FROM scope_policies WHERE scope=?', (scope,)).fetchone()
            if previous_policy and previous_policy[0] != unknown_usage_policy:
                raise ProtocolError('不能改变已有运行的未知用量策略')
            legacy = self.db.execute('SELECT 1 FROM requests WHERE scope=? LIMIT 1', (scope,)).fetchone()
            if previous_policy is None and legacy and unknown_usage_policy != 'stop':
                raise ProtocolError('历史严格预算不能静默切换到宽松策略')
            self.db.execute('INSERT OR IGNORE INTO scope_policies VALUES(?,?)', (scope,unknown_usage_policy))
            for name, tokens, calls in (("__global__", global_tokens, global_calls), (scope, scope_tokens, scope_calls)):
                if type(tokens) is not int or tokens < 1 or type(calls) is not int or calls < 1:
                    raise ProtocolError("模型预算必须是正整数")
                previous = self.db.execute("SELECT tokens,calls FROM limits WHERE scope=?", (name,)).fetchone()
                if previous is not None and previous != (tokens, calls):
                    raise ProtocolError("不能改变已登记的模型预算")
                self.db.execute("INSERT OR IGNORE INTO limits VALUES(?,?,?)", (name, tokens, calls))

    def _balance(self, scope):
        where, values = ("", ()) if scope == "__global__" else ("WHERE scope=?", (scope,))
        attempts, count, accounted, known, unresolved = self.db.execute(
            "SELECT COUNT(*),SUM(CASE WHEN status='not_sent' THEN 0 ELSE 1 END),"
            "COALESCE(SUM(COALESCE(used,reserved)),0),COALESCE(SUM(used),0),"
            "COALESCE(SUM(CASE WHEN used IS NULL THEN reserved ELSE 0 END),0) FROM requests " + where, values).fetchone()
        token_cap, call_cap = self.db.execute("SELECT tokens,calls FROM limits WHERE scope=?", (scope,)).fetchone()
        return {"calls": count or 0, 'attempts': attempts, "accounted_tokens": accounted,
                'known_tokens': known, 'unresolved_reserved_tokens': unresolved,
                "token_cap": token_cap, "call_cap": call_cap}

    def reserve(self, body, *, logical_request_id=None, attempt_index=1, pre_output_retry=False):
        from .core import digest
        # UTF-8 byte length is a conservative token bound; reserve framing and hidden reasoning separately.
        input_bound = len(canonical(body.get("messages", [])).encode("utf-8")) + 4096
        output_bound = int(body.get('max_completion_tokens', body.get("max_tokens", 8000))) + int(body.get("thinking_budget", 0))
        amount = input_bound + output_bound
        if logical_request_id is not None and (not isinstance(logical_request_id,str) or not logical_request_id or len(logical_request_id)>256):
            raise ProtocolError('逻辑请求 ID 不合法')
        if type(attempt_index) is not int or attempt_index < 1:
            raise ProtocolError('请求尝试编号必须是正整数')
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            statuses = "('reserved','unknown')" if self.unknown_usage_policy=='stop' else "('reserved')"
            unknown = self.db.execute("SELECT 1 FROM requests WHERE scope=? AND status IN " + statuses,
                                      (self.scope,)).fetchone()
            if unknown:
                raise ModelBudgetFailure('usage_unresolved', "上一调用用量未知或仍在执行，该运行停止调用模型")
            if logical_request_id is not None:
                conflict = self.db.execute("SELECT 1 FROM requests WHERE scope=? AND logical_request_id=? "
                    "AND (attempt_index=? OR (status='unknown' AND (?=0 OR COALESCE(output_started,1)<>0))) LIMIT 1",
                    (self.scope,logical_request_id,attempt_index,int(pre_output_retry and self.unknown_usage_policy=='reserve'))).fetchone()
                if conflict:
                    raise ModelBudgetFailure('request_recovery_blocked', '该逻辑请求尝试已记录或用量未知，不能重复发送')
            for scope in ("__global__", self.scope):
                balance = self._balance(scope)
                if balance["calls"] >= balance["call_cap"] or balance["accounted_tokens"] + amount > balance["token_cap"]:
                    raise ModelBudgetFailure('request_budget_exhausted', "模型调用预算不足，发送前已停止")
            request_id = uuid.uuid4().hex
            self.db.execute("INSERT INTO requests(id,scope,reserved,status,request_hash,logical_request_id,attempt_index) VALUES(?,?,?,'reserved',?,?,?)",
                            (request_id, self.scope, amount, digest(body),logical_request_id,attempt_index))
            return request_id

    def settle(self, request_id, usage, *, reason=None, sent=True, complete=True,
               transport_phase=None, output_started=None):
        observed = {k:v for k,v in (usage.items() if isinstance(usage,dict) else [])
                    if k in {'total_tokens','prompt_tokens','completion_tokens'} and type(v) is int and v>=0}
        reported = observed.get('total_tokens')
        total = reported
        if total is None:
            a, b = observed.get("prompt_tokens"), observed.get("completion_tokens")
            if type(a) is int and type(b) is int and a >= 0 and b >= 0:
                total = a + b
        source = 'provider_final' if reported is not None else ('provider_components' if total is not None else 'missing')
        if observed and (not complete or total is None):
            source = 'provider_partial'
        if not complete:
            total = None
        raw = canonical(observed) if observed else None
        with self._lock, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT scope,reserved,used,status,raw_usage_json FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None or row[0] != self.scope:
                raise ProtocolError("模型调用预留不存在")
            if row[3] != "reserved":
                if ((row[3] == "settled" and row[2] == total) or
                        (row[3] == 'unknown' and total is None and row[4] == raw) or
                        (row[3]=='not_sent' and not sent and not observed)):
                    return
                raise ProtocolError("模型调用预留已结算或用量未知")
            if not sent:
                if observed:
                    raise ProtocolError('未发送的请求不能携带服务端用量')
                self.db.execute("UPDATE requests SET used=0,status='not_sent',usage_status='not_sent',usage_reason=? WHERE id=?",
                                (reason or 'cancelled_before_dispatch', request_id))
                self.db.execute("UPDATE requests SET usage_source='not_sent',transport_phase=?,stream_completed=0 WHERE id=?",
                                (transport_phase,request_id))
                return
            self.db.execute('UPDATE requests SET usage_source=?,reported_tokens=?,raw_usage_json=?,transport_phase=?,stream_completed=?,output_started=? WHERE id=?',
                            (source,reported,raw,transport_phase,int(complete),None if output_started is None else int(output_started),request_id))
            if type(total) is not int or total < 0:
                self.db.execute("UPDATE requests SET status='unknown',usage_status=?,usage_reason=? WHERE id=?",
                                ('partial' if observed else 'unknown',reason or 'missing_provider_usage', request_id))
            else:
                status = "settled" if total <= row[1] else "unknown"
                self.db.execute("UPDATE requests SET used=?,status=? WHERE id=?", (total, status, request_id))
                self.db.execute("UPDATE requests SET usage_status='known',usage_reason=? WHERE id=?",
                                ('usage_over_reservation' if status == 'unknown' else None, request_id))
        if type(total) is not int or total < 0:
            if self.unknown_usage_policy == 'stop':
                raise ModelBudgetFailure('usage_unresolved', "提供商缺少有效用量，保留预留并停止该运行")
            return  # Availability mode retains the full bound; never pretends this was free.
        if total > row[1]:
            raise ModelBudgetFailure('usage_accounting_failure', "提供商用量超过请求预留，停止该运行")

    def snapshot(self):
        with self._lock:
            rows = self.db.execute("SELECT id,scope,reserved,used,status,request_hash,created,usage_status,usage_reason,usage_source,transport_phase,logical_request_id,attempt_index,reported_tokens,raw_usage_json,stream_completed,output_started FROM requests ORDER BY rowid").fetchall()
            return {"global": self._balance("__global__"), "scope": self._balance(self.scope),
                    'unknown_usage_policy': self.unknown_usage_policy,
                    "attempts": [dict(zip(("id", "scope", "reserved_tokens", "used_tokens", "status", "request_sha256", "created", 'usage_status', 'usage_reason', 'usage_source','transport_phase','logical_request_id','attempt_index','reported_tokens','raw_usage_json','stream_completed','output_started'), r)) for r in rows]}

    @property
    def tokens_used(self):
        return self._balance(self.scope)["accounted_tokens"]

    def close(self):
        self.db.close()

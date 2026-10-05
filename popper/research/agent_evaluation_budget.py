"""Campaign-wide durable ceiling, additional to existing session reservations.

No automatic retry/refund of dispatched or ambiguous requests. The conversation
adapter must reserve before dispatch and settle with provider-reported usage.
This ledger is infrastructure, not a model execution adapter.
"""
from __future__ import annotations

import json
import sqlite3

from ..core import ProtocolError, canonical, digest
from ..task_context import redact_input


class AgentEvaluationBudget:
    def __init__(self, path, max_requests=None, max_tokens=None):
        if any(x is not None and (type(x) is not int or x <= 0) for x in (max_requests, max_tokens)):
            raise ProtocolError("invalid campaign ceiling")
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS policy(id INTEGER PRIMARY KEY CHECK(id=1),
                max_requests INTEGER NOT NULL, max_tokens INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY, snapshot TEXT NOT NULL,
                sha256 TEXT NOT NULL, reserved INTEGER NOT NULL,
                state TEXT NOT NULL, used INTEGER);
            CREATE TABLE IF NOT EXISTS aggregate_usage(id TEXT PRIMARY KEY,
                receipt TEXT NOT NULL, sha256 TEXT NOT NULL, adjustment INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS aggregate_members(request_id TEXT PRIMARY KEY,
                aggregate_id TEXT NOT NULL REFERENCES aggregate_usage(id));
            CREATE TABLE IF NOT EXISTS policy_amendments(id TEXT PRIMARY KEY,
                receipt TEXT NOT NULL, sha256 TEXT NOT NULL);
        """)
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO policy VALUES(1,?,?)",
                                    (max_requests if max_requests is not None else 60,
                                     max_tokens if max_tokens is not None else 400000))
        limits = self.connection.execute("SELECT * FROM policy").fetchone()
        if ((max_requests is not None and limits["max_requests"] != max_requests)
                or (max_tokens is not None and limits["max_tokens"] != max_tokens)):
            self.close()
            raise ProtocolError("persisted campaign ceiling cannot be silently changed")

    def close(self):
        self.connection.close()

    def policy(self):
        row = self.connection.execute("SELECT max_requests,max_tokens FROM policy WHERE id=1").fetchone()
        return dict(row)

    def amend_policy(self, amendment_id, expected_policy, new_policy, authorization):
        """Explicit administrative authorization; never an Agent tool or reset.

        All existing request snapshots, settlements and aggregate evidence remain
        untouched. Callers supply the human authorization, not model suggestions.
        """
        keys = {"max_requests", "max_tokens"}
        if (not isinstance(amendment_id, str) or not amendment_id
                or not isinstance(expected_policy, dict) or set(expected_policy) != keys
                or not isinstance(new_policy, dict) or set(new_policy) != keys
                or any(type(v) is not int or v <= 0 for v in [*expected_policy.values(), *new_policy.values()])
                or not isinstance(authorization, dict)
                or authorization.get("actor") != "user"
                or not isinstance(authorization.get("text"), str)
                or not authorization["text"].strip() or len(authorization["text"]) > 4000):
            raise ProtocolError("explicit human budget authorization and exact policies required")
        receipt = redact_input({"expected_policy": expected_policy, "new_policy": new_policy,
                                "authorization": authorization})
        body, sha = canonical(receipt), digest(receipt)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            prior = self.connection.execute("SELECT sha256 FROM policy_amendments WHERE id=?", (amendment_id,)).fetchone()
            if prior:
                if prior["sha256"] != sha:
                    raise ProtocolError("budget amendment idempotency conflict")
                return self.policy()
            if self.policy() != expected_policy:
                raise ProtocolError("budget policy version conflict")
            if self.status()["unresolved_requests"]:
                raise ProtocolError("reconcile uncertain requests before budget amendment")
            state = self.status()
            if new_policy["max_requests"] < state["request_count"] or new_policy["max_tokens"] < state["used_tokens"]:
                raise ProtocolError("budget amendment cannot erase existing consumption")
            self.connection.execute("INSERT INTO policy_amendments VALUES(?,?,?)", (amendment_id, body, sha))
            self.connection.execute("UPDATE policy SET max_requests=?,max_tokens=? WHERE id=1",
                (new_policy["max_requests"], new_policy["max_tokens"]))
        return self.policy()

    def reserve(self, request_id, snapshot, reserved_tokens):
        if not isinstance(request_id, str) or not request_id or type(reserved_tokens) is not int or reserved_tokens <= 0:
            raise ProtocolError("invalid request reservation")
        safe = redact_input(snapshot)
        body, sha = canonical(safe), digest(safe)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            prior = self.connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if prior:
                if prior["sha256"] != sha or prior["reserved"] != reserved_tokens:
                    raise ProtocolError("request ID belongs to a different snapshot")
                raise ProtocolError("request already reserved; reconcile it, never redispatch automatically")
            if self.connection.execute("SELECT 1 FROM requests WHERE state NOT IN ('settled','aggregate_reconciled','usage_over_reservation') LIMIT 1").fetchone():
                raise ProtocolError("unresolved request or unavailable usage; campaign stopped")
            limits = self.connection.execute("SELECT * FROM policy").fetchone()
            count, used = self.connection.execute(
                "SELECT count(*),coalesce(sum(used),0) FROM requests").fetchone()
            used += self.connection.execute("SELECT coalesce(sum(adjustment),0) FROM aggregate_usage").fetchone()[0]
            if count >= limits["max_requests"] or used + reserved_tokens > limits["max_tokens"]:
                raise ProtocolError("campaign request/token ceiling exhausted")
            self.connection.execute("INSERT INTO requests VALUES(?,?,?,?,?,NULL)",
                                    (request_id, body, sha, reserved_tokens, "reserved"))
        return {"request_id": request_id, "snapshot_sha256": sha,
                "snapshot": json.loads(body), "reserved_tokens": reserved_tokens}

    def settle(self, request_id, total_tokens):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise ProtocolError("unknown campaign request")
            if self.connection.execute('SELECT 1 FROM aggregate_members WHERE request_id=?', (request_id,)).fetchone():
                if row['state'] == 'settled' and type(total_tokens) is int and row['used'] == total_tokens:
                    return
                raise ProtocolError('request covered by immutable aggregate; individual usage must not double count it')
            if row["state"] in {"settled", "usage_over_reservation"}:
                if type(total_tokens) is not int or row["used"] != total_tokens:
                    raise ProtocolError("usage settlement conflict")
                return
            if type(total_tokens) is not int or total_tokens < 0:
                self.connection.execute("UPDATE requests SET state='usage_unknown' WHERE id=?", (request_id,))
            else:
                state = "settled" if total_tokens <= row["reserved"] else "usage_over_reservation"
                self.connection.execute("UPDATE requests SET state=?, used=? WHERE id=?",
                                        (state, total_tokens, request_id))

    def reconcile_aggregate(self, receipt_id, request_ids, provider_requests, total_tokens, evidence):
        """Administrative evidence reconciliation, never an Agent tool or retry.

        Preserves individual unknown usage. A covered group's charge is known,
        without allocating a dashboard discrepancy to any individual request.
        Caller must verify the provider/date/account scope against real evidence.
        """
        if (not isinstance(receipt_id, str) or not receipt_id or not isinstance(request_ids, list)
                or not request_ids or any(not isinstance(x, str) or not x for x in request_ids)
                or len(set(request_ids)) != len(request_ids)
                or type(provider_requests) is not int or type(total_tokens) is not int or total_tokens < 0
                or not isinstance(evidence, dict) or not evidence.get('source_sha256')
                or evidence.get('scope_verified') is not True):
            raise ProtocolError('aggregate requires verified scope, evidence and distinct request IDs')
        body = redact_input({'request_ids': sorted(request_ids), 'provider_requests': provider_requests,
                             'total_tokens': total_tokens, 'evidence': evidence})
        sha = digest(body)
        with self.connection:
            self.connection.execute('BEGIN IMMEDIATE')
            prior = self.connection.execute('SELECT sha256 FROM aggregate_usage WHERE id=?', (receipt_id,)).fetchone()
            if prior:
                if prior['sha256'] != sha:
                    raise ProtocolError('aggregate reconciliation conflict')
                return self.status()
            rows = []
            for request_id in request_ids:
                row = self.connection.execute('SELECT * FROM requests WHERE id=?', (request_id,)).fetchone()
                if row is None or self.connection.execute(
                        'SELECT 1 FROM aggregate_members WHERE request_id=?', (request_id,)).fetchone():
                    raise ProtocolError('missing request or overlapping aggregate coverage')
                rows.append(row)
            known = [r for r in rows if r['state'] == 'settled']
            unknown = [r for r in rows if r['state'] == 'usage_unknown']
            # A recorded unknown-usage terminal attempt, not an in-flight reservation,
            # may be covered; over-reservation violations cannot be forgiven here.
            if len(unknown) != 1 or len(known) + 1 != len(rows) or provider_requests != len(known):
                raise ProtocolError('aggregate must match known requests and one unreported failed attempt')
            known_tokens = sum(r['used'] for r in known)
            if total_tokens < known_tokens or total_tokens > known_tokens + unknown[0]['reserved']:
                raise ProtocolError('aggregate total contradicts known usage or reservation')
            self.connection.execute('INSERT INTO aggregate_usage VALUES(?,?,?,?)',
                (receipt_id, canonical(body), sha, total_tokens - known_tokens))
            self.connection.executemany('INSERT INTO aggregate_members VALUES(?,?)',
                [(r['id'], receipt_id) for r in rows])
            self.connection.execute("UPDATE requests SET state='aggregate_reconciled' WHERE id=?", (unknown[0]['id'],))
        return self.status()

    def status(self):
        rows = self.connection.execute("SELECT state,used,reserved FROM requests").fetchall()
        adjustment = self.connection.execute('SELECT coalesce(sum(adjustment),0) FROM aggregate_usage').fetchone()[0]
        return {"request_count": len(rows), "used_tokens": sum(r["used"] or 0 for r in rows) + adjustment,
                "unresolved_requests": sum(r["state"] not in {'settled','aggregate_reconciled','usage_over_reservation'} for r in rows),
                "over_reservation_requests": sum(r["state"] == 'usage_over_reservation' for r in rows),
                "unallocated_usage_requests": sum(r['state'] == 'aggregate_reconciled' for r in rows),
                "aggregate_adjustment_tokens": adjustment,
                "remaining_calls": self.connection.execute("SELECT max_requests FROM policy").fetchone()[0] - len(rows)}

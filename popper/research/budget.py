"""预算总账（R0）· 预留-结算，跨所有子实验，按研究族记账。

- 与 research.sqlite 共用同一连接：预留与状态写入可以在同一次事务里提交，
  不再需要跨库写入后的手工补偿。
- 每个写方法用 SAVEPOINT 保证自身原子；被外层事务包住时不会提前提交。
- reserve：原子预留，余额不足拒绝；返回不可变 reservation id。
- settle：按实际消耗结算，未用额度退回；结算只允许一次。
- kind 区分预留用途（dev / confirmation / external），只有开发集预留允许在
  启动对账时回收。
"""
from __future__ import annotations

import contextlib
import sqlite3
import uuid
import math

from ..core import ProtocolError

RESERVATION_KINDS = ("dev", "confirmation", "external")


class BudgetError(ProtocolError):
    pass


class BudgetLedger:
    """预算账本。cap 为族级全局硬上限，预留累计不得超过 cap。"""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    @contextlib.contextmanager
    def _atomic(self):
        """自身原子：嵌套在外层事务里时只开 SAVEPOINT，不提前提交。

        独立调用（没有外层事务）时，SAVEPOINT 只会开启一个事务、RELEASE 并不提交，
        因此这里显式接管最外层事务的提交与回滚，避免「调用成功但没落盘」。
        """
        # 保存点名字必须是合法标识符：连字符会触发 SQL 语法错误，用下划线。
        name = "budget_" + uuid.uuid4().hex
        outermost = not self._conn.in_transaction
        if outermost:
            self._conn.execute("BEGIN IMMEDIATE")
        self._conn.execute(f"SAVEPOINT {name}")
        try:
            yield
        except Exception:
            self._conn.execute(f"ROLLBACK TO {name}")
            self._conn.execute(f"RELEASE {name}")
            if outermost:
                self._conn.rollback()
            raise
        self._conn.execute(f"RELEASE {name}")
        if outermost:
            self._conn.commit()

    def close(self):
        """账本不拥有连接（与研究存储共用），关闭由存储负责。"""

    def open_family(self, family_id, cap):
        """登记研究族并设置全局预算上限（仅一次；重复以已登记为准）。"""
        if (not family_id or isinstance(cap, bool) or not isinstance(cap, (int, float))
                or not math.isfinite(cap) or cap < 0):
            raise BudgetError("cap 必须为非负数值")
        with self._atomic():
            row = self._conn.execute(
                "SELECT cap FROM families WHERE family_id = ?", (family_id,)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO families(family_id, cap) VALUES(?, ?)",
                    (family_id, cap))
                registered_cap = float(cap)
            else:
                registered_cap = row[0]
                if registered_cap != float(cap):
                    raise BudgetError(
                        f"研究族 {family_id} 已登记预算 {registered_cap:.2f}，"
                        f"不能改为 {float(cap):.2f}")
        return {"family_id": family_id, "cap": registered_cap}

    def reserve(self, family_id, item, amount, kind="dev"):
        """原子且幂等地预留：同一 family/item 不会因恢复而重复扣减。"""
        if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                or not math.isfinite(amount) or amount < 0 or not item):
            raise BudgetError("预留金额不能为负")
        if kind not in RESERVATION_KINDS:
            raise BudgetError(f"未知预留用途: {kind!r}")
        reservation_id = uuid.uuid4().hex
        with self._atomic():
            existing = self._conn.execute(
                "SELECT reservation_id, amount, status, spent, kind FROM reservations "
                "WHERE family_id = ? AND item = ?", (family_id, item)).fetchone()
            if existing is not None:
                old_id, old_amount, status, spent, old_kind = existing
                if old_amount != float(amount):
                    raise BudgetError("同一预算项目不能用不同金额重复预留")
                if old_kind != kind:
                    raise BudgetError("同一预算项目不能改变预留用途")
                return {"reservation_id": old_id, "amount": old_amount,
                        "status": status, "spent": spent, "duplicate": True}
            row = self._conn.execute(
                "SELECT cap, reserved, spent FROM families WHERE family_id = ?",
                (family_id,)).fetchone()
            if row is None:
                raise BudgetError(f"研究族未登记: {family_id}")
            cap, reserved, spent = row
            if spent + reserved + amount > cap:
                raise BudgetError(
                    f"预算不足: 已花费 {spent:.2f} + 已预留 {reserved:.2f} + "
                    f"本次 {amount:.2f} > 上限 {cap:.2f}")
            self._conn.execute(
                "UPDATE families SET reserved = reserved + ? WHERE family_id = ?",
                (amount, family_id))
            self._conn.execute(
                "INSERT INTO reservations(reservation_id, family_id, item, amount, kind,"
                " status, created) VALUES(?, ?, ?, ?, ?, 'reserved', datetime('now'))",
                (reservation_id, family_id, item, amount, kind))
        return {"reservation_id": reservation_id, "amount": amount,
                "status": "reserved", "duplicate": False}

    def settle(self, reservation_id, spent):
        """按实际消耗结算，未用额度退回；同一预留只结算一次。"""
        if (isinstance(spent, bool) or not isinstance(spent, (int, float))
                or not math.isfinite(spent) or spent < 0):
            raise BudgetError("结算金额不能为负")
        with self._atomic():
            row = self._conn.execute(
                "SELECT family_id, amount, status, spent FROM reservations WHERE reservation_id = ?",
                (reservation_id,)).fetchone()
            if row is None:
                raise BudgetError(f"预留不存在: {reservation_id}")
            family_id, amount, status, prior_spent = row
            if status != "reserved":
                if prior_spent == float(spent):
                    return {"reservation_id": reservation_id, "spent": prior_spent,
                            "refund": amount - prior_spent, "duplicate": True}
                raise BudgetError(f"预留已结算，不可用不同金额重复: {reservation_id} ({status})")
            if spent > amount:
                raise BudgetError(f"实际消耗 {spent:.2f} 超过预留 {amount:.2f}")
            refund = amount - spent
            self._conn.execute(
                "UPDATE families SET reserved = reserved - ?, spent = spent + ? "
                "WHERE family_id = ?", (amount, spent, family_id))
            self._conn.execute(
                "UPDATE reservations SET status = 'settled', spent = ? "
                "WHERE reservation_id = ?", (spent, reservation_id))
        return {"reservation_id": reservation_id, "spent": spent,
                "refund": refund, "duplicate": False}

    def balance(self, family_id):
        """余额视图：cap / 已预留 / 已结算 / 剩余可用。"""
        row = self._conn.execute(
            "SELECT cap, reserved, spent FROM families WHERE family_id = ?",
            (family_id,)).fetchone()
        if row is None:
            raise BudgetError(f"研究族未登记: {family_id}")
        cap, reserved, spent = row
        return {"family_id": family_id, "cap": cap, "reserved": reserved,
                "spent": spent, "available": cap - reserved - spent}

    def outstanding(self, family_id):
        """该族当前所有未结算预留（对账用，不分用途）。"""
        rows = self._conn.execute(
            "SELECT reservation_id, item, amount, kind FROM reservations "
            "WHERE family_id = ? AND status = 'reserved' ORDER BY created, item",
            (family_id,)).fetchall()
        return [{"reservation_id": r[0], "item": r[1], "amount": r[2], "kind": r[3]}
                for r in rows]

    def reclaim(self, reservation_id):
        """回收一笔悬空预留：按零消耗结算并退回全部额度。"""
        return self.settle(reservation_id, 0.0)

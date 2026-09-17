"""研究库 schema 版本与统一迁移入口。

每个 SQLite 库只在这里定义表结构，并用 `PRAGMA user_version` 记录版本；
建库方（Experiment / ResearchStore / HoldoutService）只调用 migrate_*，
不再自己写 DDL。迁移必须是幂等的「补齐缺失结构」，不覆盖既有数据。

- state.db：核心实验注册状态与运行表（`popper/core.py`）。
- research.sqlite：研究状态（study/事件链）与预算总账同库。
- holdout.sqlite：保留集服务单独持库，保持与研究工作目录的解耦。
"""
from __future__ import annotations

SCHEMA_VERSION = 1

_STATE_TABLES = """
CREATE TABLE IF NOT EXISTS state(
    id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
    payload TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
    id TEXT PRIMARY KEY, config_hash TEXT NOT NULL, split TEXT NOT NULL,
    status TEXT NOT NULL, result TEXT);
CREATE INDEX IF NOT EXISTS idx_runs_config_split_status ON runs(config_hash,split,status);
CREATE INDEX IF NOT EXISTS idx_runs_running ON runs(status) WHERE status='running';
"""

_RESEARCH_TABLES = """
CREATE TABLE IF NOT EXISTS snapshots(
    kind TEXT NOT NULL, entity_id TEXT NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL,
    writer TEXT NOT NULL, updated_seq INTEGER NOT NULL,
    PRIMARY KEY (kind, entity_id));
CREATE TABLE IF NOT EXISTS events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL, entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL, payload TEXT NOT NULL,
    writer TEXT NOT NULL,
    previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS study_versions(
    study_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL CHECK(version >= 1));
CREATE TABLE IF NOT EXISTS store_meta(
    key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

_BUDGET_TABLES = """
CREATE TABLE IF NOT EXISTS families(
    family_id TEXT PRIMARY KEY,
    cap REAL NOT NULL,
    reserved REAL NOT NULL DEFAULT 0,
    spent REAL NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS reservations(
    reservation_id TEXT PRIMARY KEY,
    family_id TEXT NOT NULL,
    item TEXT NOT NULL,
    amount REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'dev',
    status TEXT NOT NULL DEFAULT 'reserved',
    created TEXT NOT NULL,
    spent REAL);
"""

_HOLDOUT_TABLES = """
CREATE TABLE IF NOT EXISTS contracts(
    contract_id TEXT PRIMARY KEY,
    consumption_key TEXT NOT NULL UNIQUE,
    envelope TEXT NOT NULL,
    labels BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS tickets(
    ticket_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    consumption_key TEXT NOT NULL UNIQUE,
    submission TEXT NOT NULL,
    envelope TEXT NOT NULL,
    status TEXT NOT NULL,
    runner_receipt_sha256 TEXT,
    predictions_sha256 TEXT,
    result TEXT);
"""


def _columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_missing_columns(conn, table, columns):
    existing = _columns(conn, table)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def schema_version(conn):
    """当前库的 schema 版本（0 表示从未迁移过）。"""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate_state(conn):
    """核心实验库：注册状态、审计事件链与运行表。

    与 research.sqlite 物理分离是刻意的：实验注册在 `popper experiment init` 时冻结，
    研究循环不得改写它。这里只统一 schema 来源与版本，不合并文件。
    """
    with conn:
        conn.executescript(_STATE_TABLES)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return SCHEMA_VERSION


def migrate_research(conn):
    """研究库：状态、事件链、study 版本、链头，以及（同库的）预算总账。"""
    with conn:
        conn.executescript(_RESEARCH_TABLES)
    migrate_budget(conn)
    return SCHEMA_VERSION


def migrate_budget(conn):
    """预算总账：与 study 同库，因此预留可以与状态写入同事务提交。"""
    with conn:
        conn.executescript(_BUDGET_TABLES)
        # 预算预留需要区分用途：只有开发集预留可以在启动对账时回收。
        _add_missing_columns(conn, "reservations", {"kind": "TEXT NOT NULL DEFAULT 'dev'"})
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_reservation_item "
            "ON reservations(family_id, item)")
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return SCHEMA_VERSION


def migrate_holdout(conn):
    """保留集服务库：合同与一次性票据。"""
    with conn:
        conn.executescript(_HOLDOUT_TABLES)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return SCHEMA_VERSION

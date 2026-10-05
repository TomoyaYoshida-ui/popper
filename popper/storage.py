"""Database ports used by durable Popper services.

The domain stores retain their schemas and state-machine rules.  This module
separates transaction mechanics from those rules so another SQL backend can be
implemented and run through the same conformance suite.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class StorageCapabilities:
    dialect: str
    parameter_style: str
    write_lock: str
    nested_transactions: bool


@runtime_checkable
class TransactionConnection(Protocol):
    capabilities: StorageCapabilities

    def execute(self, statement: str, parameters: Any = ()) -> Any: ...
    def executemany(self, statement: str, parameters: Any) -> Any: ...
    def apply_schema(self, script: str) -> None: ...
    def begin(self, *, write: bool = False) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
    def transaction(self, *, write: bool = False) -> Any: ...
    def close(self) -> None: ...


class SQLiteConnection:
    """DB-API-shaped connection with explicit, nestable unit-of-work scopes."""

    capabilities = StorageCapabilities(
        dialect="sqlite", parameter_style="qmark",
        write_lock="begin_immediate", nested_transactions=True)

    def __init__(self, connection: sqlite3.Connection):
        self.raw = connection
        self._savepoint_counter = 0

    def execute(self, statement, parameters=()):
        return self.raw.execute(statement, parameters)

    def executemany(self, statement, parameters):
        return self.raw.executemany(statement, parameters)

    def apply_schema(self, script):
        # sqlite3.executescript() commits a pending transaction before running.
        # Execute complete statements individually so migrations remain atomic.
        pending = ""
        for line in script.splitlines(keepends=True):
            pending += line
            if sqlite3.complete_statement(pending):
                if pending.strip():
                    self.raw.execute(pending)
                pending = ""
        if pending.strip():
            raise ValueError("incomplete SQL schema statement")

    def executescript(self, script):
        """Compatibility shim; production schema code should use apply_schema."""
        return self.raw.executescript(script)

    def begin(self, *, write=False):
        self.raw.execute("BEGIN IMMEDIATE" if write else "BEGIN")

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    @property
    def in_transaction(self):
        return self.raw.in_transaction

    def __enter__(self):
        """DB-API compatibility for legacy maintenance and test code."""
        return self

    def __exit__(self, error_type, error, traceback):
        if error_type is None:
            self.raw.commit()
        else:
            self.raw.rollback()
        return False

    @contextmanager
    def transaction(self, *, write=False) -> Iterator["SQLiteConnection"]:
        if self.raw.in_transaction:
            self._savepoint_counter += 1
            name = f"popper_sp_{self._savepoint_counter}"
            self.raw.execute(f"SAVEPOINT {name}")
            try:
                yield self
                self.raw.execute(f"RELEASE SAVEPOINT {name}")
            except BaseException:
                self.raw.execute(f"ROLLBACK TO SAVEPOINT {name}")
                self.raw.execute(f"RELEASE SAVEPOINT {name}")
                raise
            return
        self.raw.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield self
            self.raw.commit()
        except BaseException:
            self.raw.rollback()
            raise

    def close(self):
        self.raw.close()


class SQLiteStorageAdapter:
    """Local storage adapter; path configuration is immutable and reusable."""

    capabilities = SQLiteConnection.capabilities

    def __init__(self, path, *, timeout=30.0, foreign_keys=True):
        self.path = Path(path).resolve()
        self.timeout = timeout
        self.foreign_keys = foreign_keys

    def connect(self) -> SQLiteConnection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(str(self.path), timeout=self.timeout)
        raw.row_factory = sqlite3.Row
        if self.foreign_keys:
            raw.execute("PRAGMA foreign_keys=ON")
        return SQLiteConnection(raw)

    def connect_readonly(self) -> SQLiteConnection:
        """Open an existing SQLite database without creating or migrating it."""
        raw = sqlite3.connect(f"{self.path.as_uri()}?mode=ro",
                              uri=True, timeout=self.timeout)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA query_only=ON")
        if self.foreign_keys:
            raw.execute("PRAGMA foreign_keys=ON")
        return SQLiteConnection(raw)

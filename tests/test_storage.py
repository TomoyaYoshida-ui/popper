import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from popper.storage import (SQLiteStorageAdapter, StorageCapabilities,
                            TransactionConnection)
from popper.research.schema import migrate_research


class SQLiteStorageAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "store.sqlite"
        self.adapter = SQLiteStorageAdapter(self.path, timeout=0.05)
        self.connection = self.adapter.connect()
        with self.connection.transaction(write=True):
            self.connection.execute(
                "CREATE TABLE records(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")

    def tearDown(self):
        self.connection.close()
        self.temp.cleanup()

    def test_declares_portable_transaction_contract(self):
        self.assertIsInstance(self.connection, TransactionConnection)
        self.assertEqual(StorageCapabilities(
            dialect="sqlite", parameter_style="qmark",
            write_lock="begin_immediate", nested_transactions=True),
            self.connection.capabilities)

    def test_readonly_connection_rejects_writes_and_never_creates_missing_db(self):
        readonly = self.adapter.connect_readonly()
        try:
            self.assertEqual([], readonly.execute("SELECT * FROM records").fetchall())
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("INSERT INTO records VALUES(1,'forbidden')")
        finally:
            readonly.close()
        missing = SQLiteStorageAdapter(self.path.parent / "absent" / "missing.sqlite")
        with self.assertRaises(sqlite3.OperationalError):
            missing.connect_readonly()
        self.assertFalse(missing.path.parent.exists())

    def test_outer_failure_rolls_back_all_writes(self):
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.connection.transaction(write=True):
                self.connection.execute("INSERT INTO records VALUES(1,'one')")
                raise RuntimeError("abort")
        self.assertEqual([], self.connection.execute("SELECT * FROM records").fetchall())

    def test_nested_failure_rolls_back_to_savepoint_only(self):
        with self.connection.transaction(write=True):
            self.connection.execute("INSERT INTO records VALUES(1,'one')")
            with self.assertRaisesRegex(RuntimeError, "nested"):
                with self.connection.transaction(write=True):
                    self.connection.execute("INSERT INTO records VALUES(2,'two')")
                    raise RuntimeError("nested")
            self.connection.execute("INSERT INTO records VALUES(3,'three')")
        rows = self.connection.execute("SELECT id,value FROM records ORDER BY id").fetchall()
        self.assertEqual([(1, "one"), (3, "three")], [tuple(row) for row in rows])

    def test_immediate_write_transaction_excludes_second_writer(self):
        contender = self.adapter.connect()
        try:
            with self.connection.transaction(write=True):
                self.connection.execute("INSERT INTO records VALUES(1,'one')")
                with self.assertRaises(sqlite3.OperationalError):
                    with contender.transaction(write=True):
                        contender.execute("INSERT INTO records VALUES(2,'two')")
            with contender.transaction(write=True):
                contender.execute("INSERT INTO records VALUES(2,'two')")
        finally:
            contender.close()
        rows = self.connection.execute("SELECT id FROM records ORDER BY id").fetchall()
        self.assertEqual([1, 2], [row[0] for row in rows])

    def test_schema_failure_rolls_back_earlier_statements(self):
        with self.assertRaises(sqlite3.OperationalError):
            with self.connection.transaction(write=True):
                self.connection.apply_schema(
                    "CREATE TABLE migration_probe(id INTEGER);\n"
                    "CREATE TABLE migration_probe(id INTEGER);\n")
        tables = self.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='migration_probe'").fetchall()
        self.assertEqual([], tables)

    def test_research_and_budget_schema_migrate_as_one_unit(self):
        with patch("popper.research.schema.migrate_budget",
                   side_effect=RuntimeError("budget migration failed")):
            with self.assertRaisesRegex(RuntimeError, "budget migration failed"):
                migrate_research(self.connection)
        names = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("snapshots", names)
        self.assertNotIn("families", names)
        self.assertEqual(0, self.connection.execute("PRAGMA user_version").fetchone()[0])
        migrate_research(self.connection)
        names = {row[0] for row in self.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("snapshots", names)
        self.assertIn("families", names)


if __name__ == "__main__":
    unittest.main()

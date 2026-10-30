from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from meditation_server.migration_runner import (
    Migration,
    MigrationError,
    apply_migrations,
    load_migrations,
)
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class FakeExecutor(DatabaseExecutor):
    def __init__(self) -> None:
        self.applied: dict[str, str] = {}
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sql, parameters=None, *, transaction=None):
        self.statements.append(sql.strip())
        if sql.strip().startswith("SELECT version, checksum"):
            return QueryResult(
                rows=tuple(
                    {"version": version, "checksum": checksum}
                    for version, checksum in sorted(self.applied.items())
                )
            )
        if sql.strip().startswith("INSERT INTO wm_schema_migrations"):
            self.applied[str(parameters["version"])] = str(parameters["checksum"])
            return QueryResult(records_updated=1)
        return QueryResult()

    async def execute_batch(self, sql, parameter_sets, *, transaction=None):
        raise AssertionError("Migrations do not use batch execution")

    async def begin_transaction(self):
        return Transaction(self, "fake")

    async def commit(self, transaction):
        self._transaction_value(transaction)
        self.commits += 1

    async def rollback(self, transaction):
        self._transaction_value(transaction)
        self.rollbacks += 1


class MigrationLoadingTests(unittest.TestCase):
    def test_repository_migrations_are_valid(self) -> None:
        directory = Path(__file__).resolve().parents[1] / "migrations"
        migrations = load_migrations(directory)
        self.assertEqual(
            [migration.version for migration in migrations],
            ["0001", "0002", "0003", "0004", "0005", "0006", "0007"],
        )
        self.assertGreater(len(migrations[0].statements), 20)
        self.assertGreater(len(migrations[1].statements), 5)
        self.assertEqual(len(migrations[2].statements), 3)
        self.assertEqual(len(migrations[4].statements), 4)
        self.assertEqual(len(migrations[5].statements), 2)

    def test_loads_ordered_statements_and_checksum(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "0002_second.sql").write_text("SELECT 2;", encoding="utf-8")
            (path / "0001_first.sql").write_text(
                "SELECT 1;\n-- migrate:split\nSELECT 10;\n", encoding="utf-8"
            )

            migrations = load_migrations(path)

        self.assertEqual([migration.version for migration in migrations], ["0001", "0002"])
        self.assertEqual(migrations[0].statements, ("SELECT 1;", "SELECT 10;"))
        self.assertEqual(len(migrations[0].checksum), 64)

    def test_rejects_bad_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "initial.sql").write_text("SELECT 1;", encoding="utf-8")
            with self.assertRaisesRegex(MigrationError, "filename"):
                load_migrations(path)


class MigrationApplicationTests(unittest.IsolatedAsyncioTestCase):
    async def test_applies_once_and_is_idempotent(self) -> None:
        executor = FakeExecutor()
        migrations = (
            Migration("0001", "0001_first.sql", "a" * 64, ("SELECT 1", "SELECT 2")),
        )

        first = await apply_migrations(executor, migrations)
        second = await apply_migrations(executor, migrations)

        self.assertEqual(first, ("0001",))
        self.assertEqual(second, ())
        self.assertEqual(executor.statements.count("SELECT 1"), 1)
        self.assertEqual(executor.commits, 2)

    async def test_rejects_changed_applied_migration_and_rolls_back(self) -> None:
        executor = FakeExecutor()
        executor.applied["0001"] = "a" * 64
        migrations = (Migration("0001", "0001_first.sql", "b" * 64, ("SELECT 1",)),)

        with self.assertRaisesRegex(MigrationError, "modified"):
            await apply_migrations(executor, migrations)

        self.assertEqual(executor.rollbacks, 1)

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re

from meditation_server.repositories.executor import DatabaseExecutor, Transaction


_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4,})_[a-z0-9_]+\.sql$")
_STATEMENT_BOUNDARY = "-- migrate:split"
_ADVISORY_LOCK_KEY = 8_965_137_421_011_024_001


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    checksum: str
    statements: tuple[str, ...]


class MigrationError(RuntimeError):
    """Raised when migration history is invalid or an applied file changed."""


def load_migrations(directory: Path) -> tuple[Migration, ...]:
    migrations: list[Migration] = []
    seen_versions: set[str] = set()
    for path in sorted(directory.glob("*.sql")):
        match = _MIGRATION_NAME.fullmatch(path.name)
        if match is None:
            raise MigrationError(
                f"Migration filename must match NNNN_description.sql: {path.name}"
            )
        version = match.group("version")
        if version in seen_versions:
            raise MigrationError(f"Duplicate migration version: {version}")
        seen_versions.add(version)
        content = path.read_text(encoding="utf-8")
        statements = tuple(
            statement.strip()
            for statement in content.split(_STATEMENT_BOUNDARY)
            if statement.strip()
        )
        if not statements:
            raise MigrationError(f"Migration contains no SQL: {path.name}")
        migrations.append(
            Migration(
                version=version,
                name=path.name,
                checksum=sha256(content.encode("utf-8")).hexdigest(),
                statements=statements,
            )
        )
    if not migrations:
        raise MigrationError(f"No migrations found in {directory}")
    return tuple(migrations)


async def apply_migrations(
    executor: DatabaseExecutor,
    migrations: tuple[Migration, ...],
) -> tuple[str, ...]:
    """Apply pending files once, atomically, under a PostgreSQL advisory lock."""

    applied_now: list[str] = []
    async with executor.transaction() as transaction:
        await executor.execute(
            "SELECT pg_advisory_xact_lock(:lock_key)",
            {"lock_key": _ADVISORY_LOCK_KEY},
            transaction=transaction,
        )
        await _ensure_history_table(executor, transaction)
        result = await executor.execute(
            "SELECT version, checksum FROM wm_schema_migrations ORDER BY version",
            transaction=transaction,
        )
        applied = {str(row["version"]): str(row["checksum"]) for row in result.rows}

        known_versions = {migration.version for migration in migrations}
        unknown = sorted(set(applied) - known_versions)
        if unknown:
            raise MigrationError(
                "Database contains migrations missing from this build: "
                + ", ".join(unknown)
            )

        for migration in migrations:
            existing_checksum = applied.get(migration.version)
            if existing_checksum is not None:
                if existing_checksum != migration.checksum:
                    raise MigrationError(
                        f"Applied migration {migration.name} has been modified"
                    )
                continue
            for statement in migration.statements:
                await executor.execute(statement, transaction=transaction)
            await executor.execute(
                """
                INSERT INTO wm_schema_migrations (version, name, checksum)
                VALUES (:version, :name, :checksum)
                """,
                {
                    "version": migration.version,
                    "name": migration.name,
                    "checksum": migration.checksum,
                },
                transaction=transaction,
            )
            applied_now.append(migration.version)
    return tuple(applied_now)


async def _ensure_history_table(
    executor: DatabaseExecutor,
    transaction: Transaction,
) -> None:
    await executor.execute(
        """
        CREATE TABLE IF NOT EXISTS wm_schema_migrations (
            version text PRIMARY KEY,
            name text NOT NULL UNIQUE,
            checksum text NOT NULL CHECK (length(checksum) = 64),
            applied_at timestamptz NOT NULL DEFAULT now()
        )
        """,
        transaction=transaction,
    )

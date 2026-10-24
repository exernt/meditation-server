from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from .executor import DatabaseExecutor, Parameters, QueryResult, Transaction


_NAMED_PARAMETER = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")
_UPDATED_COUNT = re.compile(r"(?:^|\s)(\d+)$")


class AsyncpgExecutor(DatabaseExecutor):
    """Local PostgreSQL executor using the same named SQL as the Data API."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @classmethod
    async def connect(cls, database_url: str, **pool_options: Any) -> "AsyncpgExecutor":
        try:
            import asyncpg
        except ImportError as exc:  # pragma: no cover - depends on local environment
            raise RuntimeError("asyncpg is required for the local PostgreSQL executor") from exc
        pool = await asyncpg.create_pool(database_url, **pool_options)
        return cls(pool)

    async def close(self) -> None:
        await self._pool.close()

    async def execute(
        self,
        sql: str,
        parameters: Parameters | None = None,
        *,
        transaction: Transaction | None = None,
    ) -> QueryResult:
        compiled_sql, arguments = _compile_named_parameters(sql, parameters or {})
        if transaction is None:
            async with self._pool.acquire() as connection:
                return await _execute_on_connection(connection, compiled_sql, arguments)
        connection, _ = self._local_transaction(transaction)
        return await _execute_on_connection(connection, compiled_sql, arguments)

    async def execute_batch(
        self,
        sql: str,
        parameter_sets: Sequence[Parameters],
        *,
        transaction: Transaction | None = None,
    ) -> None:
        if not parameter_sets:
            return
        compiled_sql, first_arguments = _compile_named_parameters(sql, parameter_sets[0])
        parameter_names = _parameter_names(sql)
        all_arguments = [first_arguments]
        all_arguments.extend(
            tuple(values[name] for name in parameter_names)
            for values in parameter_sets[1:]
        )
        if transaction is None:
            async with self._pool.acquire() as connection:
                await connection.executemany(compiled_sql, all_arguments)
            return
        connection, _ = self._local_transaction(transaction)
        await connection.executemany(compiled_sql, all_arguments)

    async def begin_transaction(self) -> Transaction:
        connection = await self._pool.acquire()
        transaction = connection.transaction()
        try:
            await transaction.start()
        except BaseException:
            await self._pool.release(connection)
            raise
        return Transaction(self, (connection, transaction))

    async def commit(self, transaction: Transaction) -> None:
        connection, local_transaction = self._local_transaction(transaction)
        try:
            await local_transaction.commit()
        finally:
            await self._pool.release(connection)

    async def rollback(self, transaction: Transaction) -> None:
        connection, local_transaction = self._local_transaction(transaction)
        try:
            await local_transaction.rollback()
        finally:
            await self._pool.release(connection)

    def _local_transaction(self, transaction: Transaction) -> tuple[Any, Any]:
        value = self._transaction_value(transaction)
        if not isinstance(value, tuple) or len(value) != 2:
            raise TypeError("Invalid asyncpg transaction handle")
        return value


async def _execute_on_connection(
    connection: Any,
    sql: str,
    arguments: tuple[Any, ...],
) -> QueryResult:
    statement = await connection.prepare(sql)
    records = await statement.fetch(*arguments)
    status = statement.get_statusmsg() or ""
    match = _UPDATED_COUNT.search(status)
    return QueryResult(
        rows=tuple(dict(record) for record in records),
        records_updated=int(match.group(1)) if match else 0,
    )


def _parameter_names(sql: str) -> tuple[str, ...]:
    names: list[str] = []
    for match in _NAMED_PARAMETER.finditer(sql):
        if match.group(1) not in names:
            names.append(match.group(1))
    return tuple(names)


def _compile_named_parameters(
    sql: str,
    parameters: Mapping[str, Any],
) -> tuple[str, tuple[Any, ...]]:
    names = _parameter_names(sql)
    missing = [name for name in names if name not in parameters]
    extra = [name for name in parameters if name not in names]
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if extra:
            details.append(f"unused: {', '.join(extra)}")
        raise ValueError("SQL parameter mismatch (" + "; ".join(details) + ")")
    positions = {name: index for index, name in enumerate(names, start=1)}
    compiled = _NAMED_PARAMETER.sub(lambda match: f"${positions[match.group(1)]}", sql)
    return compiled, tuple(parameters[name] for name in names)

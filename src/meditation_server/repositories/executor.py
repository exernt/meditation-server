from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any


Parameters = Mapping[str, Any]


@dataclass(frozen=True)
class QueryResult:
    """Transport-neutral result for a single PostgreSQL statement."""

    rows: tuple[Mapping[str, Any], ...] = ()
    records_updated: int = 0

    def one(self) -> Mapping[str, Any]:
        if len(self.rows) != 1:
            raise ValueError(f"Expected exactly one row, received {len(self.rows)}")
        return self.rows[0]


@dataclass(frozen=True)
class Transaction:
    """Opaque transaction handle that may only be used by its creating executor."""

    _owner: object
    _value: object


class DatabaseExecutor(ABC):
    """Small async boundary implemented by local PostgreSQL and the RDS Data API."""

    @abstractmethod
    async def execute(
        self,
        sql: str,
        parameters: Parameters | None = None,
        *,
        transaction: Transaction | None = None,
    ) -> QueryResult:
        """Execute one parameterised PostgreSQL statement."""

    @abstractmethod
    async def execute_batch(
        self,
        sql: str,
        parameter_sets: Sequence[Parameters],
        *,
        transaction: Transaction | None = None,
    ) -> None:
        """Execute one DML statement for several parameter sets."""

    @abstractmethod
    async def begin_transaction(self) -> Transaction:
        """Begin a transaction and return an executor-owned handle."""

    @abstractmethod
    async def commit(self, transaction: Transaction) -> None:
        """Commit a transaction created by this executor."""

    @abstractmethod
    async def rollback(self, transaction: Transaction) -> None:
        """Roll back a transaction created by this executor."""

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Transaction]:
        handle = await self.begin_transaction()
        try:
            yield handle
        except BaseException:
            await self.rollback(handle)
            raise
        else:
            await self.commit(handle)

    def _transaction_value(self, transaction: Transaction) -> object:
        if transaction._owner is not self:
            raise ValueError("Transaction belongs to a different database executor")
        return transaction._value

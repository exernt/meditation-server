from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from meditation_server.api import auth
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class ScriptedExecutor(DatabaseExecutor):
    def __init__(self, responder):
        self.responder = responder
        self.commits = 0
        self.rollbacks = 0
        self.calls: list[tuple[str, dict, Transaction | None]] = []

    async def execute(self, sql, parameters=None, *, transaction=None):
        normalized = " ".join(sql.split())
        values = parameters or {}
        self.calls.append((normalized, values, transaction))
        return self.responder(normalized, values)

    async def execute_batch(self, sql, parameter_sets, *, transaction=None):
        raise AssertionError("batch execution is not expected")

    async def begin_transaction(self):
        return Transaction(self, "test-transaction")

    async def commit(self, transaction):
        self._transaction_value(transaction)
        self.commits += 1

    async def rollback(self, transaction):
        self._transaction_value(transaction)
        self.rollbacks += 1


def test_list_pending_researchers_returns_android_response_shape() -> None:
    user_id = uuid4()
    requested_at = datetime(2026, 10, 24, tzinfo=UTC)

    def respond(sql, _parameters):
        assert "profile.verified_at IS NULL" in sql
        assert "users.email_verified = true" in sql
        assert "users.disabled_at IS NULL" in sql
        return QueryResult(rows=({
            "user_id": user_id,
            "email": "researcher@example.com",
            "institution": "University of Melbourne",
            "requested_at": requested_at,
        },))

    database = ScriptedExecutor(respond)
    response = asyncio.run(auth.list_pending_researchers(database))

    assert response == {"researchers": [{
        "user_id": user_id,
        "email": "researcher@example.com",
        "institution": "University of Melbourne",
        "requested_at": requested_at,
    }]}


def test_reject_pending_researcher_restores_participant_and_revokes_sessions() -> None:
    user_id = uuid4()

    def respond(sql, _parameters):
        if sql.startswith("DELETE FROM researcher_profiles"):
            return QueryResult(rows=({"user_id": user_id},), records_updated=1)
        return QueryResult(records_updated=1)

    database = ScriptedExecutor(respond)
    response = asyncio.run(auth.reject_researcher(user_id, database))

    statements = [sql for sql, _parameters, _transaction in database.calls]
    assert response == {"message": "Researcher request rejected"}
    assert any("SET role='participant'::user_role" in sql for sql in statements)
    assert any("UPDATE refresh_tokens SET revoked_at=now()" in sql for sql in statements)
    assert any("INSERT INTO security_events" in sql for sql in statements)
    assert database.commits == 1
    assert database.rollbacks == 0
    assert all(transaction is not None for _sql, _parameters, transaction in database.calls)


def test_reject_missing_or_already_verified_request_returns_404_and_rolls_back() -> None:
    database = ScriptedExecutor(lambda _sql, _parameters: QueryResult())

    with pytest.raises(HTTPException, match="No pending researcher request") as raised:
        asyncio.run(auth.reject_researcher(uuid4(), database))

    assert raised.value.status_code == 404
    assert database.commits == 0
    assert database.rollbacks == 1

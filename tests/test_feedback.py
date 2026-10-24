from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException

from meditation_server.api import feedback
from meditation_server.api.dependencies import CurrentUser, get_admin_or_verified_researcher
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class RecordingExecutor(DatabaseExecutor):
    def __init__(self, results: list[QueryResult]):
        self.results = iter(results)
        self.statements: list[str] = []

    async def execute(self, sql, parameters=None, *, transaction=None):
        self.statements.append(" ".join(sql.split()))
        return next(self.results)

    async def execute_batch(self, sql, parameter_sets, *, transaction=None):
        raise AssertionError("batch execution is not expected")

    async def begin_transaction(self):
        return Transaction(self, "test-transaction")

    async def commit(self, transaction):
        self._transaction_value(transaction)

    async def rollback(self, transaction):
        self._transaction_value(transaction)


def make_user(role: str) -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        email=f"{role}@example.com",
        role=role,
        session_version=0,
        email_verified=True,
    )


def test_list_general_feedback_returns_non_study_feedback_in_client_shape() -> None:
    created_at = datetime(2026, 10, 24, tzinfo=UTC)
    item = {
        "id": uuid4(),
        "user_id": uuid4(),
        "study_id": None,
        "category": "general",
        "rating": 5,
        "comment": "Helpful app",
        "created_at": created_at,
    }
    database = RecordingExecutor([QueryResult(rows=(item,))])

    response = asyncio.run(feedback.list_general_feedback(make_user("admin"), database))

    assert response == {"feedback": [item]}
    assert "WHERE study_id IS NULL" in database.statements[0]
    assert "ORDER BY created_at DESC" in database.statements[0]


def test_admin_can_view_general_feedback_without_researcher_profile_lookup() -> None:
    database = RecordingExecutor([])
    admin = make_user("admin")

    result = asyncio.run(get_admin_or_verified_researcher(admin, database))

    assert result is admin
    assert database.statements == []


def test_verified_researcher_can_view_general_feedback() -> None:
    database = RecordingExecutor([QueryResult(rows=({"verified_at": datetime.now(UTC)},))])
    researcher = make_user("researcher")

    result = asyncio.run(get_admin_or_verified_researcher(researcher, database))

    assert result is researcher
    assert "SELECT verified_at FROM researcher_profiles" in database.statements[0]


def test_participant_cannot_view_general_feedback() -> None:
    database = RecordingExecutor([])

    with pytest.raises(HTTPException, match="Only researchers") as raised:
        asyncio.run(get_admin_or_verified_researcher(make_user("participant"), database))

    assert raised.value.status_code == 403

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from meditation_server.api import auth
from meditation_server.api.dependencies import CurrentUser
from meditation_server.api.models import RegisterRequest, ResearcherRequest, VerificationConfirmRequest
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class RecordingExecutor(DatabaseExecutor):
    def __init__(self, responder=None):
        self.responder = responder or (lambda _sql, _parameters: QueryResult())
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

    async def rollback(self, transaction):
        self._transaction_value(transaction)


def test_researcher_registration_defers_profile_until_email_verification(monkeypatch) -> None:
    monkeypatch.setattr(auth, "hash_password", AsyncMock(return_value="password-hash"))
    monkeypatch.setattr(auth, "send_verification", AsyncMock())
    database = RecordingExecutor()

    response = asyncio.run(auth.register(
        RegisterRequest(
            email="researcher@example.com",
            password="correct horse battery staple",
            researcher_institution="University of Melbourne",
        ),
        database,
    ))

    statements = [sql for sql, _parameters, _transaction in database.calls]
    user_insert = next(call for call in database.calls if call[0].startswith("INSERT INTO users"))
    assert response == {"message": "Registration successful; verify your email before signing in"}
    assert user_insert[1]["role"] == "researcher"
    assert user_insert[1]["pending_researcher_institution"] == "University of Melbourne"
    assert not any(sql.startswith("INSERT INTO researcher_profiles") for sql in statements)


def test_email_verification_creates_deferred_researcher_profile() -> None:
    token_id = uuid4()
    user_id = uuid4()

    def respond(sql, _parameters):
        if sql.startswith("SELECT id, user_id, expires_at, used_at"):
            return QueryResult(rows=({
                "id": token_id,
                "user_id": user_id,
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
                "used_at": None,
            },))
        if sql.startswith("UPDATE users SET email_verified = true"):
            return QueryResult(rows=({
                "role": "researcher",
                "pending_researcher_institution": "University of Melbourne",
            },))
        return QueryResult(records_updated=1)

    database = RecordingExecutor(respond)
    response = asyncio.run(auth.confirm_verification(
        VerificationConfirmRequest(token="verification-token"),
        database,
    ))

    profile_insert = next(
        call for call in database.calls
        if call[0].startswith("INSERT INTO researcher_profiles")
    )
    assert response == {"message": "Email verified"}
    assert profile_insert[1] == {
        "id": user_id,
        "institution": "University of Melbourne",
    }
    assert any(
        "SET pending_researcher_institution = NULL" in sql
        for sql, _parameters, _transaction in database.calls
    )


def test_participant_email_verification_does_not_create_researcher_profile() -> None:
    token_id = uuid4()
    user_id = uuid4()

    def respond(sql, _parameters):
        if sql.startswith("SELECT id, user_id, expires_at, used_at"):
            return QueryResult(rows=({
                "id": token_id,
                "user_id": user_id,
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
                "used_at": None,
            },))
        if sql.startswith("UPDATE users SET email_verified = true"):
            return QueryResult(rows=({
                "role": "participant",
                "pending_researcher_institution": None,
            },))
        return QueryResult(records_updated=1)

    database = RecordingExecutor(respond)
    asyncio.run(auth.confirm_verification(
        VerificationConfirmRequest(token="verification-token"),
        database,
    ))

    assert not any(
        sql.startswith("INSERT INTO researcher_profiles")
        for sql, _parameters, _transaction in database.calls
    )


def test_existing_account_cannot_request_researcher_access_before_email_verification() -> None:
    user = CurrentUser(
        id=uuid4(),
        email="unverified@example.com",
        role="participant",
        session_version=0,
        email_verified=False,
    )
    database = RecordingExecutor()

    with pytest.raises(HTTPException, match="Email verification is required") as raised:
        asyncio.run(auth.register_researcher(
            ResearcherRequest(institution="University of Melbourne"),
            user,
            database,
        ))

    assert raised.value.status_code == 403
    assert database.calls == []

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from meditation_server.api import auth
from meditation_server.api.models import RegisterRequest
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class RecordingExecutor(DatabaseExecutor):
    def __init__(self):
        self.calls: list[tuple[str, dict, Transaction | None]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sql, parameters=None, *, transaction=None):
        normalized = " ".join(sql.split())
        values = dict(parameters or {})
        self.calls.append((normalized, values, transaction))
        return QueryResult()

    async def execute_batch(self, sql, parameter_sets, *, transaction=None):
        raise AssertionError("batch execution is not expected")

    async def begin_transaction(self):
        return Transaction(self, "privacy-policy-test")

    async def commit(self, transaction):
        self._transaction_value(transaction)
        self.commits += 1

    async def rollback(self, transaction):
        self._transaction_value(transaction)
        self.rollbacks += 1


def registration_request(**overrides) -> RegisterRequest:
    values = {
        "email": "person@example.com",
        "password": "correct horse battery staple",
        "agrees_to_privacy_policy": True,
        "privacy_policy_version": auth.CURRENT_PRIVACY_POLICY_VERSION,
    }
    values.update(overrides)
    return RegisterRequest(**values)


def test_registration_requires_privacy_policy_fields() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(
            email="person@example.com",
            password="correct horse battery staple",
        )


def test_registration_rejects_non_positive_privacy_policy_version() -> None:
    with pytest.raises(ValidationError):
        registration_request(privacy_policy_version=0)


def test_registration_rejects_missing_agreement_before_database_work() -> None:
    database = RecordingExecutor()

    with pytest.raises(HTTPException) as raised:
        asyncio.run(auth.register(
            registration_request(agrees_to_privacy_policy=False),
            database,
        ))

    assert raised.value.status_code == 400
    assert raised.value.detail == "You must agree to the privacy policy to register"
    assert database.calls == []
    assert database.commits == 0


def test_registration_rejects_outdated_policy_version_before_database_work() -> None:
    database = RecordingExecutor()

    with pytest.raises(HTTPException) as raised:
        asyncio.run(auth.register(
            registration_request(
                privacy_policy_version=auth.CURRENT_PRIVACY_POLICY_VERSION + 1,
            ),
            database,
        ))

    assert raised.value.status_code == 409
    assert "review it" in raised.value.detail
    assert database.calls == []
    assert database.commits == 0


def test_registration_records_policy_acceptance_and_version(monkeypatch) -> None:
    monkeypatch.setattr(auth, "hash_password", AsyncMock(return_value="password-hash"))
    monkeypatch.setattr(auth, "send_verification", AsyncMock())
    database = RecordingExecutor()

    response = asyncio.run(auth.register(registration_request(), database))

    user_insert = next(
        call for call in database.calls if call[0].startswith("INSERT INTO users")
    )
    parameters = user_insert[1]
    accepted_at = parameters["privacy_policy_accepted_at"]
    assert response == {
        "message": "Registration successful; verify your email before signing in"
    }
    assert parameters["privacy_policy_version"] == auth.CURRENT_PRIVACY_POLICY_VERSION
    assert accepted_at.tzinfo is not None
    assert database.commits == 1
    assert database.rollbacks == 0
    assert user_insert[2] is not None

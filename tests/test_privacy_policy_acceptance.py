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
        return QueryResult(records_updated=1)

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


def request(**overrides) -> RegisterRequest:
    values = {
        "email": "person@example.com",
        "password": "correct horse battery staple",
        "agrees_to_privacy_policy": True,
        "privacy_policy_version": auth.CURRENT_PRIVACY_POLICY_VERSION,
    }
    return RegisterRequest(**{**values, **overrides})


def test_registration_requires_the_privacy_policy_fields() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest(email="person@example.com", password="correct horse battery staple")


def test_registration_rejects_a_non_positive_policy_version() -> None:
    with pytest.raises(ValidationError):
        request(privacy_policy_version=0)


def test_registration_without_agreement_is_refused_before_touching_the_database() -> None:
    database = RecordingExecutor()

    with pytest.raises(HTTPException) as raised:
        asyncio.run(auth.register(request(agrees_to_privacy_policy=False), database))

    assert raised.value.status_code == 400
    assert raised.value.detail == "You must agree to the privacy policy to register"
    assert database.calls == []
    assert database.commits == 0


def test_registration_with_an_out_of_date_policy_version_is_refused() -> None:
    database = RecordingExecutor()

    with pytest.raises(HTTPException) as raised:
        asyncio.run(auth.register(
            request(privacy_policy_version=auth.CURRENT_PRIVACY_POLICY_VERSION + 1),
            database,
        ))

    assert raised.value.status_code == 409
    assert "review it" in raised.value.detail
    assert database.calls == []
    assert database.commits == 0


def test_registration_stores_when_and_to_which_version_the_user_agreed(monkeypatch) -> None:
    monkeypatch.setattr(auth, "hash_password", AsyncMock(return_value="password-hash"))
    monkeypatch.setattr(auth, "send_verification", AsyncMock())
    database = RecordingExecutor()

    response = asyncio.run(auth.register(request(), database))

    sql, parameters, transaction = next(
        call for call in database.calls if call[0].startswith("INSERT INTO users")
    )
    assert response == {
        "message": "Registration successful; verify your email before signing in"
    }
    assert "privacy_policy_accepted_at" in sql
    assert "now()" in sql
    assert "privacy_policy_accepted_at" not in parameters
    assert parameters["privacy_policy_version"] == auth.CURRENT_PRIVACY_POLICY_VERSION
    assert transaction is not None
    assert database.commits == 1
    assert database.rollbacks == 0

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from meditation_server.api import auth
from meditation_server.api.dependencies import CurrentUser
from meditation_server.api.models import ProfileUpdateRequest, RegisterRequest
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class RecordingExecutor(DatabaseExecutor):
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict, Transaction | None]] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sql, parameters=None, *, transaction=None):
        normalized = " ".join(sql.split())
        values = dict(parameters or {})
        self.calls.append((normalized, values, transaction))
        return self.responder(normalized, values)

    async def execute_batch(self, sql, parameter_sets, *, transaction=None):
        raise AssertionError("batch execution is not expected")

    async def begin_transaction(self):
        return Transaction(self, "profile-test")

    async def commit(self, transaction):
        self._transaction_value(transaction)
        self.commits += 1

    async def rollback(self, transaction):
        self._transaction_value(transaction)
        self.rollbacks += 1


def current_user(*, role: str = "participant") -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        email="person@example.com",
        role=role,
        session_version=3,
        email_verified=True,
    )


@pytest.mark.parametrize("age", [17, 121])
def test_profile_rejects_age_outside_study_range(age: int) -> None:
    with pytest.raises(ValueError):
        ProfileUpdateRequest(age=age, gender="Other")
    with pytest.raises(ValueError):
        RegisterRequest(
            email="person@example.com",
            password="valid-password",
            agrees_to_privacy_policy=True,
            privacy_policy_version=auth.CURRENT_PRIVACY_POLICY_VERSION,
            age=age,
        )


def test_update_profile_normalizes_gender_and_persists_both_fields() -> None:
    user = current_user()

    def respond(sql, parameters):
        assert sql.startswith("UPDATE users SET age=:age, gender=:gender")
        return QueryResult(rows=({"age": parameters["age"], "gender": parameters["gender"]},))

    database = RecordingExecutor(respond)
    response = asyncio.run(auth.update_me(
        ProfileUpdateRequest(age=34, gender="  Non-binary  "), user, database
    ))

    assert response == {"age": 34, "gender": "Non-binary"}


def test_get_profile_includes_demographics() -> None:
    user = current_user(role="researcher")
    verified_at = datetime(2026, 10, 29, tzinfo=UTC)
    database = RecordingExecutor(lambda _sql, _parameters: QueryResult(rows=({
        "age": 42,
        "gender": "Female",
        "institution": "University of Melbourne",
        "verified_at": verified_at,
    },)))

    response = asyncio.run(auth.me(user, database))

    assert response["age"] == 42
    assert response["gender"] == "Female"
    assert response["institution"] == "University of Melbourne"
    assert response["verified_at"] == verified_at


def test_delete_account_revokes_sessions_then_deletes_user_transactionally() -> None:
    user = current_user()
    database = RecordingExecutor(lambda _sql, _parameters: QueryResult(records_updated=1))

    response = asyncio.run(auth.delete_me(user, database))

    statements = [sql for sql, _parameters, _transaction in database.calls]
    assert response == {"message": "Account deleted"}
    version_index = next(i for i, sql in enumerate(statements) if sql.startswith("UPDATE users SET session_version"))
    refresh_index = next(i for i, sql in enumerate(statements) if sql.startswith("UPDATE refresh_tokens"))
    event_index = next(i for i, sql in enumerate(statements) if sql.startswith("INSERT INTO security_events"))
    delete_index = next(i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM users"))
    assert version_index < refresh_index < event_index < delete_index
    assert not any("questionnaire_responses" in sql for sql in statements)
    assert database.commits == 1
    assert database.rollbacks == 0
    assert all(transaction is not None for _sql, _parameters, transaction in database.calls)

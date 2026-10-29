from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from meditation_server.api import auth
from meditation_server.api.models import LoginRequest, TokenRequest
from meditation_server.auth.security import decode_access_token
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class ScriptedExecutor(DatabaseExecutor):
    def __init__(self, responder):
        self.responder = responder
        self.commits = 0
        self.rollbacks = 0
        self.statements: list[str] = []

    async def execute(self, sql, parameters=None, *, transaction=None):
        normalized = " ".join(sql.split())
        self.statements.append(normalized)
        return self.responder(normalized, parameters or {})

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


def test_unknown_user_failure_commits_throttle_and_audit(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_RATE_LIMIT_PEPPER", "test-pepper")
    monkeypatch.setattr(auth, "verify_unknown_user_password", AsyncMock())

    def respond(sql, _parameters):
        if "FROM login_throttles" in sql:
            return QueryResult()
        if "FROM users WHERE email" in sql:
            return QueryResult()
        if "INSERT INTO login_throttles" in sql:
            return QueryResult(rows=({"failed_attempts": 1},))
        return QueryResult()

    database = ScriptedExecutor(respond)
    request = Request({"type": "http", "method": "POST", "path": "/auth/login", "headers": [], "client": ("203.0.113.10", 1234)})

    try:
        asyncio.run(auth.login(request, LoginRequest(email="missing@example.com", password="not-the-password"), database))
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("login should fail")

    assert database.commits == 1
    assert database.rollbacks == 0
    assert any("INSERT INTO security_events" in sql for sql in database.statements)


def test_successful_login_replaces_existing_session(monkeypatch) -> None:
    monkeypatch.setenv("AUTH_RATE_LIMIT_PEPPER", "test-pepper")
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("JWT_KEY_ID", "current-test")
    monkeypatch.setattr(auth, "verify_password", AsyncMock(return_value=True))
    monkeypatch.setattr(auth, "password_needs_rehash", lambda _password_hash: False)
    user_id = uuid4()

    def respond(sql, _parameters):
        if "FROM login_throttles" in sql:
            return QueryResult()
        if "FROM users WHERE email" in sql:
            return QueryResult(rows=({
                "id": user_id,
                "email": "person@example.com",
                "password_hash": "password-hash",
                "session_version": 4,
                "role": "participant",
                "email_verified": True,
                "disabled_at": None,
            },))
        if "UPDATE users SET session_version=session_version+1" in sql:
            return QueryResult(rows=({"session_version": 5},))
        return QueryResult()

    database = ScriptedExecutor(respond)
    request = Request({"type": "http", "method": "POST", "path": "/auth/login", "headers": [], "client": ("203.0.113.10", 1234)})

    response = asyncio.run(auth.login(
        request,
        LoginRequest(email="person@example.com", password="correct-password"),
        database,
    ))

    assert decode_access_token(response["access_token"]) == (user_id, 5)
    assert database.commits == 1
    assert database.rollbacks == 0
    statements = database.statements
    version_index = next(i for i, sql in enumerate(statements) if "UPDATE users SET session_version=session_version+1" in sql)
    revoke_index = next(i for i, sql in enumerate(statements) if "UPDATE refresh_tokens SET revoked_at=now()" in sql)
    insert_index = next(i for i, sql in enumerate(statements) if "INSERT INTO refresh_tokens" in sql)
    assert version_index < revoke_index < insert_index
    assert any("login_succeeded" in str(sql) or "INSERT INTO security_events" in sql for sql in statements)


def test_refresh_replay_commits_family_revocation_before_401() -> None:
    user_id = uuid4()
    token_id = uuid4()
    family_id = uuid4()

    def respond(sql, _parameters):
        if "SELECT user_id FROM refresh_tokens" in sql:
            return QueryResult(rows=({"user_id": user_id},))
        if "FROM users WHERE id" in sql:
            return QueryResult(rows=({"id": user_id, "role": "participant", "session_version": 0, "disabled_at": None},))
        if "SELECT id,user_id,family_id,expires_at,revoked_at FROM refresh_tokens" in sql:
            return QueryResult(rows=({
                "id": token_id,
                "user_id": user_id,
                "family_id": family_id,
                "expires_at": datetime.now(UTC) + timedelta(days=1),
                "revoked_at": datetime.now(UTC),
            },))
        return QueryResult()

    database = ScriptedExecutor(respond)
    try:
        asyncio.run(auth.refresh(TokenRequest(refresh_token="replayed-token"), database))
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("refresh should fail")

    assert database.commits == 1
    assert database.rollbacks == 0
    assert any("UPDATE refresh_tokens SET revoked_at=now() WHERE family_id" in sql for sql in database.statements)
    assert any("refresh_token_replay" in str(sql) or "INSERT INTO security_events" in sql for sql in database.statements)

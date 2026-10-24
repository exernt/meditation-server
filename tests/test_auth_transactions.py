from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from meditation_server.api import auth
from meditation_server.api.models import LoginRequest, TokenRequest
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

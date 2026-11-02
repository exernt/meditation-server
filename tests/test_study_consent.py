from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from meditation_server.api.dependencies import CurrentUser
from meditation_server.api.models import EditConsentFormRequest, JoinStudyRequest
from meditation_server.api.studies import (
    PLACEHOLDER_CONSENT_TEXT,
    edit_consent_form,
    join_study,
    publish,
)
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class ScriptedExecutor(DatabaseExecutor):
    def __init__(self, responder):
        self.responder = responder
        self.statements: list[str] = []
        self.parameters: list[dict] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sql, parameters=None, *, transaction=None):
        normalized = " ".join(sql.split())
        normalized_parameters = parameters or {}
        self.statements.append(normalized)
        self.parameters.append(normalized_parameters)
        return self.responder(normalized, normalized_parameters)

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


def user(role: str = "participant") -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        email=f"{role}@example.com",
        role=role,
        session_version=0,
        email_verified=True,
    )


def study_row(study_id, status: str) -> dict:
    return {
        "id": study_id,
        "name": "Walking study",
        "duration": 14,
        "description": "",
        "status": status,
        "control_script_generation_parameters": "",
        "experimental_script_generation_parameters": "",
        "control_tts_voice": "male",
        "experimental_tts_voice": "male",
    }


def join_responder(study_id, participant, *, consent_version: int, withdrawn_at=None):
    registered_at = datetime.now(UTC)

    def respond(sql, parameters):
        if sql.startswith("SELECT id,name,duration"):
            return QueryResult(rows=(study_row(study_id, "recruiting"),))
        if sql.startswith("SELECT version FROM consent_form"):
            return QueryResult(rows=({"version": consent_version},))
        if sql.startswith("SELECT id FROM users"):
            return QueryResult(rows=({"id": participant.id},))
        if sql.startswith("SELECT sr.study_id"):
            return QueryResult()
        if sql.startswith("SELECT withdrawn_at FROM study_registrations"):
            return QueryResult(rows=({"withdrawn_at": withdrawn_at},)) if withdrawn_at is not None else QueryResult()
        if sql.startswith("SELECT count(*) FILTER"):
            return QueryResult(rows=({"control_count": 0, "experimental_count": 0},))
        if sql.startswith("INSERT INTO study_registrations") or sql.startswith("UPDATE study_registrations"):
            return QueryResult(rows=({
                "user_id": participant.id,
                "study_id": study_id,
                "registered_at": registered_at,
                "consented_at": registered_at,
                "withdrawn_at": None,
            },))
        raise AssertionError(f"Unexpected SQL: {sql}")

    return respond


def test_consent_edit_request_normalizes_and_validates_text() -> None:
    request = EditConsentFormRequest(body_text="  I agree to take part.\n")
    assert request.body_text == "I agree to take part."

    with pytest.raises(ValidationError):
        EditConsentFormRequest(body_text="   ")
    with pytest.raises(ValidationError):
        EditConsentFormRequest(body_text="x" * 20_001)
    with pytest.raises(ValidationError):
        EditConsentFormRequest(body_text="Terms", version=2)


def test_join_request_rejects_non_positive_consent_version() -> None:
    with pytest.raises(ValidationError):
        JoinStudyRequest(agrees_to_consent=True, consent_version=0)


def test_join_records_the_version_the_participant_reviewed() -> None:
    participant = user()
    study_id = uuid4()
    database = ScriptedExecutor(join_responder(
        study_id, participant, consent_version=3,
    ))

    asyncio.run(join_study(
        study_id,
        JoinStudyRequest(agrees_to_consent=True, consent_version=3),
        participant,
        database,
    ))

    insert_index = next(
        index for index, sql in enumerate(database.statements)
        if sql.startswith("INSERT INTO study_registrations")
    )
    assert database.parameters[insert_index]["consent_version"] == 3
    assert database.commits == 1


def test_legacy_join_records_the_current_consent_version() -> None:
    participant = user()
    study_id = uuid4()
    database = ScriptedExecutor(join_responder(
        study_id, participant, consent_version=4,
    ))

    asyncio.run(join_study(
        study_id,
        JoinStudyRequest(agrees_to_consent=True),
        participant,
        database,
    ))

    insert_index = next(
        index for index, sql in enumerate(database.statements)
        if sql.startswith("INSERT INTO study_registrations")
    )
    assert database.parameters[insert_index]["consent_version"] == 4


def test_rejoin_records_the_new_consent_version() -> None:
    participant = user()
    study_id = uuid4()
    database = ScriptedExecutor(join_responder(
        study_id,
        participant,
        consent_version=2,
        withdrawn_at=datetime(2026, 10, 1, tzinfo=UTC),
    ))

    asyncio.run(join_study(
        study_id,
        JoinStudyRequest(agrees_to_consent=True, consent_version=2),
        participant,
        database,
    ))

    update_index = next(
        index for index, sql in enumerate(database.statements)
        if sql.startswith("UPDATE study_registrations")
    )
    assert database.parameters[update_index]["consent_version"] == 2


def test_join_rejects_stale_consent_before_registration_changes() -> None:
    participant = user()
    study_id = uuid4()

    def respond(sql, _parameters):
        if sql.startswith("SELECT id,name,duration"):
            return QueryResult(rows=(study_row(study_id, "recruiting"),))
        if sql.startswith("SELECT version FROM consent_form"):
            return QueryResult(rows=({"version": 4},))
        raise AssertionError(f"Unexpected SQL after stale consent check: {sql}")

    database = ScriptedExecutor(respond)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(join_study(
            study_id,
            JoinStudyRequest(agrees_to_consent=True, consent_version=3),
            participant,
            database,
        ))

    assert raised.value.status_code == 409
    assert "review it again" in raised.value.detail
    assert database.commits == 0
    assert database.rollbacks == 1
    assert not any("study_registrations" in sql for sql in database.statements)


def test_editing_draft_consent_increments_version() -> None:
    study_id = uuid4()

    def respond(sql, parameters):
        if sql.startswith("SELECT id,name,duration"):
            return QueryResult(rows=(study_row(study_id, "draft"),))
        if sql.startswith("SELECT study_id,body_text,version FROM consent_form"):
            return QueryResult(rows=({
                "study_id": study_id,
                "body_text": "Existing terms",
                "version": 1,
            },))
        if sql.startswith("UPDATE consent_form"):
            assert parameters["body"] == "I agree to take part."
            return QueryResult(rows=({
                "study_id": study_id,
                "body_text": parameters["body"],
                "version": 2,
            },))
        raise AssertionError(f"Unexpected SQL: {sql}")

    database = ScriptedExecutor(respond)
    response = asyncio.run(edit_consent_form(
        study_id,
        EditConsentFormRequest(body_text="  I agree to take part.  "),
        user("researcher"),
        database,
    ))

    assert response["version"] == 2
    assert response["body_text"] == "I agree to take part."
    assert database.commits == 1


def test_saving_identical_consent_is_a_no_op() -> None:
    study_id = uuid4()

    def respond(sql, _parameters):
        if sql.startswith("SELECT id,name,duration"):
            return QueryResult(rows=(study_row(study_id, "draft"),))
        if sql.startswith("UPDATE consent_form"):
            return QueryResult()
        if sql.startswith("SELECT study_id,body_text,version FROM consent_form"):
            return QueryResult(rows=({
                "study_id": study_id,
                "body_text": "Existing terms",
                "version": 5,
            },))
        raise AssertionError(f"Unexpected SQL: {sql}")

    response = asyncio.run(edit_consent_form(
        study_id,
        EditConsentFormRequest(body_text="Existing terms"),
        user("researcher"),
        ScriptedExecutor(respond),
    ))

    assert response["version"] == 5


def test_consent_cannot_be_edited_after_publication() -> None:
    study_id = uuid4()
    database = ScriptedExecutor(lambda sql, _parameters: QueryResult(
        rows=(study_row(study_id, "recruiting"),)
    ) if sql.startswith("SELECT id,name,duration") else QueryResult())

    with pytest.raises(HTTPException) as raised:
        asyncio.run(edit_consent_form(
            study_id,
            EditConsentFormRequest(body_text="New terms"),
            user("researcher"),
            database,
        ))

    assert raised.value.status_code == 409
    assert database.rollbacks == 1
    assert not any(sql.startswith("UPDATE consent_form") for sql in database.statements)


def test_publish_rejects_the_placeholder_consent_form() -> None:
    study_id = uuid4()

    def respond(sql, _parameters):
        if sql.startswith("SELECT id,name,duration"):
            return QueryResult(rows=(study_row(study_id, "draft"),))
        if sql.startswith("SELECT body_text FROM consent_form"):
            return QueryResult(rows=({"body_text": PLACEHOLDER_CONSENT_TEXT},))
        raise AssertionError(f"Unexpected SQL: {sql}")

    database = ScriptedExecutor(respond)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(publish(study_id, user("researcher"), database))

    assert raised.value.status_code == 409
    assert raised.value.detail == "Write the consent form before publishing this study"
    assert database.commits == 0
    assert database.rollbacks == 1
    assert not any(sql.startswith("UPDATE study SET") for sql in database.statements)


def test_publish_succeeds_with_written_consent() -> None:
    study_id = uuid4()

    def respond(sql, _parameters):
        if sql.startswith("SELECT id,name,duration"):
            return QueryResult(rows=(study_row(study_id, "draft"),))
        if sql.startswith("SELECT body_text FROM consent_form"):
            return QueryResult(rows=({"body_text": "I agree to take part."},))
        if sql.startswith("UPDATE study SET"):
            return QueryResult(rows=(study_row(study_id, "recruiting"),))
        raise AssertionError(f"Unexpected SQL: {sql}")

    database = ScriptedExecutor(respond)
    response = asyncio.run(publish(study_id, user("researcher"), database))

    assert response["status"] == "recruiting"
    assert database.commits == 1
    assert database.rollbacks == 0

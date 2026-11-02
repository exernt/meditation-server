from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from meditation_server.api.dependencies import CurrentUser
from meditation_server.api.models import (
    CreateStudyRequest,
    EditConsentFormRequest,
    JoinStudyRequest,
)
from meditation_server.api.studies import (
    PLACEHOLDER_CONSENT_TEXT,
    create_study,
    edit_consent_form,
    join_study,
    publish,
)
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class ScriptedExecutor(DatabaseExecutor):
    def __init__(self, responder):
        self.responder = responder
        self.calls: list[tuple[str, dict]] = []
        self.commits = 0
        self.rollbacks = 0

    @property
    def statements(self) -> list[str]:
        return [sql for sql, _parameters in self.calls]

    def parameters_of(self, prefix: str) -> dict:
        return next(parameters for sql, parameters in self.calls if sql.startswith(prefix))

    async def execute(self, sql, parameters=None, *, transaction=None):
        normalized = " ".join(sql.split())
        self.calls.append((normalized, parameters or {}))
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


def user(role: str = "researcher") -> CurrentUser:
    return CurrentUser(
        id=uuid4(),
        email=f"{role}@example.com",
        role=role,
        session_version=0,
        email_verified=True,
    )


def study_row(status: str) -> dict:
    return {
        "id": uuid4(),
        "name": "Morning walks",
        "duration": 14,
        "description": "",
        "status": status,
        "control_script_generation_parameters": "",
        "experimental_script_generation_parameters": "",
        "control_tts_voice": "male",
        "experimental_tts_voice": "male",
    }


def run(coroutine):
    return asyncio.run(coroutine)


# --- request validation -------------------------------------------------------------------

def test_consent_request_trims_surrounding_whitespace() -> None:
    assert EditConsentFormRequest(body_text="  I agree to take part.\n").body_text == "I agree to take part."


def test_consent_request_rejects_blank_text() -> None:
    with pytest.raises(ValidationError):
        EditConsentFormRequest(body_text="   ")


def test_consent_request_enforces_the_length_limit() -> None:
    assert len(EditConsentFormRequest(body_text="x" * 20_000).body_text) == 20_000
    with pytest.raises(ValidationError):
        EditConsentFormRequest(body_text="x" * 20_001)


def test_consent_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        EditConsentFormRequest(body_text="Terms", version=9)


def test_join_request_rejects_a_non_positive_consent_version() -> None:
    with pytest.raises(ValidationError):
        JoinStudyRequest(agrees_to_consent=True, consent_version=0)


# --- editing -------------------------------------------------------------------------------

def edit_responder(status: str, *, text: str = PLACEHOLDER_CONSENT_TEXT, version: int = 1, form_exists: bool = True):
    study = study_row(status)

    def respond(sql, parameters):
        if "FROM study WHERE id" in sql:
            return QueryResult(rows=(study,))
        if sql.startswith("SELECT study_id,body_text,version FROM consent_form"):
            return QueryResult(rows=({"study_id": study["id"], "body_text": text, "version": version},) if form_exists else ())
        if sql.startswith("UPDATE consent_form"):
            return QueryResult(rows=({"study_id": study["id"], "body_text": parameters["body"], "version": version + 1},))
        raise AssertionError(f"Unexpected SQL: {sql}")

    return respond


def test_editing_updates_the_text_and_bumps_the_version() -> None:
    database = ScriptedExecutor(edit_responder("draft"))

    response = run(edit_consent_form(
        uuid4(), EditConsentFormRequest(body_text="You agree to walk daily."), user(), database,
    ))

    assert response["body_text"] == "You agree to walk daily."
    assert response["version"] == 2
    update = " ".join(database.statements[-1].split())
    assert "version=version+1" in update
    assert database.commits == 1


def test_saving_identical_text_keeps_the_version() -> None:
    database = ScriptedExecutor(edit_responder("draft", text="Same wording", version=4))

    response = run(edit_consent_form(
        uuid4(), EditConsentFormRequest(body_text="Same wording"), user(), database,
    ))

    assert response["version"] == 4
    assert not any(sql.startswith("UPDATE consent_form") for sql in database.statements)


@pytest.mark.parametrize("status", ["recruiting", "active", "closed"])
def test_editing_is_refused_once_the_study_is_published(status: str) -> None:
    database = ScriptedExecutor(edit_responder(status))

    with pytest.raises(HTTPException) as raised:
        run(edit_consent_form(uuid4(), EditConsentFormRequest(body_text="New wording"), user(), database))

    assert raised.value.status_code == 409
    assert not any(sql.startswith("UPDATE consent_form") for sql in database.statements)
    assert database.commits == 0
    assert database.rollbacks == 1


def test_editing_a_missing_consent_form_returns_404() -> None:
    database = ScriptedExecutor(edit_responder("draft", form_exists=False))

    with pytest.raises(HTTPException) as raised:
        run(edit_consent_form(uuid4(), EditConsentFormRequest(body_text="New wording"), user(), database))

    assert raised.value.status_code == 404
    assert database.commits == 0


# --- publishing ----------------------------------------------------------------------------

def publish_responder(body_text: str | None):
    study = study_row("draft")

    def respond(sql, _parameters):
        if "FROM study WHERE id" in sql:
            return QueryResult(rows=(study,))
        if sql.startswith("SELECT body_text FROM consent_form"):
            return QueryResult(rows=({"body_text": body_text},) if body_text is not None else ())
        if sql.startswith("UPDATE study SET status"):
            return QueryResult(rows=({**study, "status": "recruiting"},))
        raise AssertionError(f"Unexpected SQL: {sql}")

    return respond


def test_publishing_is_refused_while_the_consent_form_is_the_placeholder() -> None:
    database = ScriptedExecutor(publish_responder(PLACEHOLDER_CONSENT_TEXT))

    with pytest.raises(HTTPException) as raised:
        run(publish(uuid4(), user(), database))

    assert raised.value.status_code == 409
    assert "consent form" in raised.value.detail
    assert not any(sql.startswith("UPDATE study SET status") for sql in database.statements)
    assert database.commits == 0


def test_publishing_is_refused_without_a_consent_form() -> None:
    database = ScriptedExecutor(publish_responder(None))

    with pytest.raises(HTTPException) as raised:
        run(publish(uuid4(), user(), database))

    assert raised.value.status_code == 409


def test_publishing_succeeds_once_the_consent_form_is_written() -> None:
    database = ScriptedExecutor(publish_responder("You agree to walk daily."))

    response = run(publish(uuid4(), user(), database))

    assert response["status"] == "recruiting"
    assert database.commits == 1


def test_new_studies_start_with_the_placeholder_the_publish_guard_checks() -> None:
    def respond(sql, _parameters):
        if sql.startswith("INSERT INTO study ("):
            return QueryResult(rows=(study_row("draft"),))
        return QueryResult(records_updated=1)

    database = ScriptedExecutor(respond)

    run(create_study(CreateStudyRequest(name="Morning walks", duration=14), user(), database))

    assert database.parameters_of("INSERT INTO consent_form")["body"] == PLACEHOLDER_CONSENT_TEXT


# --- joining -------------------------------------------------------------------------------

def join_responder(*, form_version: int | None = 3, status: str = "recruiting", withdrawn_registration: bool = False):
    study = study_row(status)
    registration = {
        "user_id": uuid4(), "study_id": study["id"], "registered_at": None, "consented_at": None, "withdrawn_at": None,
    }

    def respond(sql, _parameters):
        if "FROM study WHERE id" in sql:
            return QueryResult(rows=(study,))
        if sql.startswith("SELECT version FROM consent_form"):
            return QueryResult(rows=({"version": form_version},) if form_version is not None else ())
        if sql.startswith("SELECT id FROM users"):
            return QueryResult(rows=())
        if "AND sr.withdrawn_at IS NULL" in sql:
            return QueryResult(rows=())
        if sql.startswith("SELECT withdrawn_at FROM study_registrations"):
            return QueryResult(rows=({"withdrawn_at": object()},) if withdrawn_registration else ())
        if sql.startswith("SELECT count(*)"):
            return QueryResult(rows=({"control_count": 0, "experimental_count": 0},))
        if sql.startswith(("INSERT INTO study_registrations", "UPDATE study_registrations")):
            return QueryResult(rows=(registration,))
        raise AssertionError(f"Unexpected SQL: {sql}")

    return respond


def join(payload: JoinStudyRequest, database: ScriptedExecutor):
    return run(join_study(uuid4(), payload, user("participant"), database))


def test_joining_records_the_consent_version_the_participant_saw() -> None:
    database = ScriptedExecutor(join_responder(form_version=3))

    join(JoinStudyRequest(agrees_to_consent=True, consent_version=3), database)

    assert database.parameters_of("INSERT INTO study_registrations")["consent_version"] == 3
    assert "consent_version" in next(s for s in database.statements if s.startswith("INSERT INTO study_registrations"))
    assert database.commits == 1


def test_joining_with_a_stale_consent_version_is_refused() -> None:
    database = ScriptedExecutor(join_responder(form_version=4))

    with pytest.raises(HTTPException) as raised:
        join(JoinStudyRequest(agrees_to_consent=True, consent_version=3), database)

    assert raised.value.status_code == 409
    assert not any(s.startswith(("INSERT INTO study_registrations", "UPDATE study_registrations")) for s in database.statements)
    assert database.commits == 0


def test_older_clients_get_the_current_version_recorded() -> None:
    database = ScriptedExecutor(join_responder(form_version=2))

    join(JoinStudyRequest(agrees_to_consent=True), database)

    assert database.parameters_of("INSERT INTO study_registrations")["consent_version"] == 2


def test_joining_a_study_without_a_consent_form_returns_404() -> None:
    database = ScriptedExecutor(join_responder(form_version=None))

    with pytest.raises(HTTPException) as raised:
        join(JoinStudyRequest(agrees_to_consent=True, consent_version=1), database)

    assert raised.value.status_code == 404
    assert database.commits == 0


def test_rejoining_records_the_new_consent_version() -> None:
    database = ScriptedExecutor(join_responder(form_version=2, withdrawn_registration=True))

    join(JoinStudyRequest(agrees_to_consent=True, consent_version=2), database)

    assert "consent_version=:consent_version" in next(s for s in database.statements if s.startswith("UPDATE study_registrations"))
    assert database.parameters_of("UPDATE study_registrations")["consent_version"] == 2

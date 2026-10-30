from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException

from meditation_server.api.dependencies import CurrentUser
from meditation_server.api.models import EditStudyRequest, IssueQuestionnaireRequest
from meditation_server.api.studies import activate, current_script_parameters, edit_study
from meditation_server.api.study_questionnaires import issue
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction


class ScriptedExecutor(DatabaseExecutor):
    def __init__(self, responder):
        self.responder = responder
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

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


def researcher():
    return CurrentUser(
        id=uuid4(),
        email="researcher@example.com",
        role="researcher",
        session_version=0,
        email_verified=True,
    )


def questionnaire_definition():
    return {
        "title": "Weekly check-in",
        "questions": [{
            "id": "q1",
            "prompt": "How are you feeling?",
            "type": "text",
            "required": True,
        }],
    }


def test_questionnaire_cannot_be_issued_while_study_is_recruiting() -> None:
    owner = researcher()
    questionnaire_id = uuid4()

    def respond(sql, _parameters):
        if "FROM questionnaires WHERE id" in sql:
            return QueryResult(rows=({
                "id": questionnaire_id,
                "title": "Weekly check-in",
                "researcher_id": owner.id,
                "status": "draft",
                "definition": questionnaire_definition(),
            },))
        if "FROM study WHERE id" in sql:
            return QueryResult(rows=({"status": "recruiting"},))
        raise AssertionError(f"Unexpected SQL: {sql}")

    database = ScriptedExecutor(respond)

    try:
        asyncio.run(issue(
            uuid4(),
            IssueQuestionnaireRequest(questionnaire_id=questionnaire_id),
            owner,
            database,
        ))
    except HTTPException as exc:
        assert exc.status_code == 409
        assert exc.detail == "Questionnaires can only be issued to active studies"
    else:
        raise AssertionError("issuing to a recruiting study should fail")

    assert database.commits == 0
    assert database.rollbacks == 1
    assert not any("UPDATE questionnaires SET status='published'" in sql for sql in database.statements)
    assert not any("INSERT INTO study_questionnaires" in sql for sql in database.statements)


def test_publishing_and_issuing_draft_questionnaire_commit_atomically() -> None:
    owner = researcher()
    questionnaire_id = uuid4()
    study_id = uuid4()
    issuance_id = uuid4()
    issued_at = datetime.now(UTC)

    def respond(sql, _parameters):
        if "FROM questionnaires WHERE id" in sql:
            return QueryResult(rows=({
                "id": questionnaire_id,
                "title": "Weekly check-in",
                "researcher_id": owner.id,
                "status": "draft",
                "definition": questionnaire_definition(),
            },))
        if "FROM study WHERE id" in sql:
            return QueryResult(rows=({"status": "active"},))
        if "SELECT COALESCE(max(round_number),0)" in sql:
            return QueryResult(rows=({"round": 0},))
        if "UPDATE questionnaires SET status='published'" in sql:
            return QueryResult(records_updated=1)
        if "INSERT INTO study_questionnaires" in sql:
            return QueryResult(rows=({
                "id": issuance_id,
                "study_id": study_id,
                "questionnaire_id": questionnaire_id,
                "round_number": 1,
                "issued_at": issued_at,
                "revoked_at": None,
            },))
        raise AssertionError(f"Unexpected SQL: {sql}")

    database = ScriptedExecutor(respond)
    response = asyncio.run(issue(
        study_id,
        IssueQuestionnaireRequest(questionnaire_id=questionnaire_id),
        owner,
        database,
    ))

    assert response["id"] == issuance_id
    assert response["round_number"] == 1
    assert database.commits == 1
    assert database.rollbacks == 0
    publish_index = next(
        index for index, sql in enumerate(database.statements)
        if "UPDATE questionnaires SET status='published'" in sql
    )
    issuance_index = next(
        index for index, sql in enumerate(database.statements)
        if "INSERT INTO study_questionnaires" in sql
    )
    assert publish_index < issuance_index


def test_failed_issuance_rolls_back_draft_publication() -> None:
    owner = researcher()
    questionnaire_id = uuid4()

    def respond(sql, _parameters):
        if "FROM questionnaires WHERE id" in sql:
            return QueryResult(rows=({
                "id": questionnaire_id,
                "title": "Weekly check-in",
                "researcher_id": owner.id,
                "status": "draft",
                "definition": questionnaire_definition(),
            },))
        if "FROM study WHERE id" in sql:
            return QueryResult(rows=({"status": "active"},))
        if "SELECT COALESCE(max(round_number),0)" in sql:
            return QueryResult(rows=({"round": 0},))
        if "UPDATE questionnaires SET status='published'" in sql:
            return QueryResult(records_updated=1)
        if "INSERT INTO study_questionnaires" in sql:
            raise RuntimeError("simulated insert failure")
        raise AssertionError(f"Unexpected SQL: {sql}")

    database = ScriptedExecutor(respond)
    try:
        asyncio.run(issue(
            uuid4(),
            IssueQuestionnaireRequest(questionnaire_id=questionnaire_id),
            owner,
            database,
        ))
    except RuntimeError as exc:
        assert str(exc) == "simulated insert failure"
    else:
        raise AssertionError("issuance failure should propagate")

    assert database.commits == 0
    assert database.rollbacks == 1
    assert any("UPDATE questionnaires SET status='published'" in sql for sql in database.statements)


def test_current_script_parameters_ignore_recruiting_enrolments() -> None:
    participant = CurrentUser(
        id=uuid4(),
        email="participant@example.com",
        role="participant",
        session_version=0,
        email_verified=True,
    )

    database = ScriptedExecutor(lambda _sql, _parameters: QueryResult())
    response = asyncio.run(current_script_parameters(participant, database))

    assert response == {
        "participating": False,
        "study_id": None,
        "study_status": None,
        "study_group": None,
        "script_generation_parameters": "",
        "tts_voice": "male",
    }
    assert len(database.statements) == 1
    assert "s.status='active'::study_status" in database.statements[0]
    assert "'recruiting'::study_status" not in database.statements[0]


def test_current_script_parameters_return_active_study_group_configuration() -> None:
    participant = CurrentUser(
        id=uuid4(),
        email="participant@example.com",
        role="participant",
        session_version=0,
        email_verified=True,
    )
    study_id = uuid4()

    database = ScriptedExecutor(lambda _sql, _parameters: QueryResult(rows=({
        "study_id": study_id,
        "study_status": "active",
        "study_group": "experimental",
        "script_generation_parameters": "Use a gratitude practice.",
        "tts_voice": "female",
    },)))

    response = asyncio.run(current_script_parameters(participant, database))

    assert response == {
        "participating": True,
        "study_id": study_id,
        "study_status": "active",
        "study_group": "experimental",
        "script_generation_parameters": "Use a gratitude practice.",
        "tts_voice": "female",
    }
    assert "CASE sr.study_group" in database.statements[0]
    assert "s.control_script_generation_parameters" in database.statements[0]
    assert "s.experimental_script_generation_parameters" in database.statements[0]
    assert "s.control_tts_voice" in database.statements[0]
    assert "s.experimental_tts_voice" in database.statements[0]


def test_researcher_can_edit_one_groups_script_parameters() -> None:
    owner = researcher()
    study_id = uuid4()

    def respond(sql, parameters):
        assert "UPDATE study SET experimental_script_generation_parameters=:value" in sql
        assert parameters["value"] == "Use a gratitude practice."
        return QueryResult(rows=({
            "id": study_id,
            "name": "Walking study",
            "duration": 14,
            "description": "",
            "status": "draft",
            "control_script_generation_parameters": "Use a body scan.",
            "experimental_script_generation_parameters": parameters["value"],
        },))

    response = asyncio.run(edit_study(
        study_id,
        EditStudyRequest(
            column="experimental_script_generation_parameters",
            value="  Use a gratitude practice.  ",
        ),
        owner,
        ScriptedExecutor(respond),
    ))

    assert response["control_script_generation_parameters"] == "Use a body scan."
    assert response["experimental_script_generation_parameters"] == "Use a gratitude practice."


def test_researcher_can_select_one_groups_tts_voice() -> None:
    owner = researcher()
    study_id = uuid4()

    def respond(sql, parameters):
        assert "UPDATE study SET control_tts_voice=:value::study_tts_voice" in sql
        assert parameters["value"] == "female"
        return QueryResult(rows=({
            "id": study_id,
            "name": "Walking study",
            "duration": 14,
            "description": "",
            "status": "draft",
            "control_script_generation_parameters": "",
            "experimental_script_generation_parameters": "",
            "control_tts_voice": parameters["value"],
            "experimental_tts_voice": "male",
        },))

    response = asyncio.run(edit_study(
        study_id,
        EditStudyRequest(column="control_tts_voice", value=" Female "),
        owner,
        ScriptedExecutor(respond),
    ))

    assert response["control_tts_voice"] == "female"
    assert response["experimental_tts_voice"] == "male"


def test_activating_study_records_countdown_start_time() -> None:
    owner = researcher()
    study_id = uuid4()

    def respond(sql, _parameters):
        if sql.startswith("SELECT id,name,duration"):
            return QueryResult(rows=({
                "id": study_id,
                "name": "Walking study",
                "duration": 14,
                "description": "",
                "status": "recruiting",
                "control_script_generation_parameters": "Use a body scan.",
                "experimental_script_generation_parameters": "Use a gratitude practice.",
            },))
        if sql.startswith("UPDATE study SET"):
            return QueryResult(rows=({
                "id": study_id,
                "name": "Walking study",
                "duration": 14,
                "description": "",
                "status": "active",
                "control_script_generation_parameters": "Use a body scan.",
                "experimental_script_generation_parameters": "Use a gratitude practice.",
            },))
        raise AssertionError(f"Unexpected SQL: {sql}")

    database = ScriptedExecutor(respond)
    response = asyncio.run(activate(study_id, owner, database))

    assert response["status"] == "active"
    assert database.commits == 1
    assert database.rollbacks == 0
    update = next(sql for sql in database.statements if sql.startswith("UPDATE study SET"))
    assert "status=:target::study_status,activated_at=now()" in update

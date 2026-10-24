from __future__ import annotations

from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException

from meditation_server.api.dependencies import DatabaseDependency, VerifiedResearcherDependency
from meditation_server.api.models import QuestionnaireRequest
from meditation_server.domain.questionnaires import validate_questionnaire


router = APIRouter(prefix="/questionnaires", tags=["questionnaires"])


async def owned(db, questionnaire_id: UUID, researcher_id: UUID):
    result = await db.execute(
        """SELECT id,title,researcher_id,status::text AS status,definition,created_on
           FROM questionnaires WHERE id=:id::uuid AND researcher_id=:researcher::uuid""",
        {"id": questionnaire_id, "researcher": researcher_id},
    )
    if not result.rows:
        raise HTTPException(status_code=404, detail="Questionnaire not found")
    return result.rows[0]


def normalized(payload: QuestionnaireRequest):
    try:
        validate_questionnaire(payload.definition)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    definition = dict(payload.definition)
    definition["title"] = str(definition["title"]).strip()
    return definition


@router.post("", status_code=201)
async def create(payload: QuestionnaireRequest, researcher: VerifiedResearcherDependency, db: DatabaseDependency):
    definition = normalized(payload)
    result = await db.execute(
        """INSERT INTO questionnaires (id,title,researcher_id,definition)
           VALUES (:id::uuid,:title,:researcher::uuid,:definition)
           RETURNING id,title,researcher_id,status::text AS status,definition,created_on""",
        {"id": uuid4(), "title": definition["title"], "researcher": researcher.id, "definition": definition},
    )
    return dict(result.rows[0])


@router.get("")
async def list_all(researcher: VerifiedResearcherDependency, db: DatabaseDependency):
    result = await db.execute(
        """SELECT id,title,researcher_id,status::text AS status,definition,created_on
           FROM questionnaires WHERE researcher_id=:researcher::uuid ORDER BY created_on DESC""",
        {"researcher": researcher.id},
    )
    return {"questionnaires": [dict(row) for row in result.rows]}


@router.get("/{questionnaire_id}")
async def get_one(questionnaire_id: UUID, researcher: VerifiedResearcherDependency, db: DatabaseDependency):
    return dict(await owned(db, questionnaire_id, researcher.id))


@router.put("/{questionnaire_id}")
async def replace(questionnaire_id: UUID, payload: QuestionnaireRequest, researcher: VerifiedResearcherDependency, db: DatabaseDependency):
    current = await owned(db, questionnaire_id, researcher.id)
    if current["status"] != "draft":
        raise HTTPException(status_code=409, detail="Only draft questionnaires can be edited")
    definition = normalized(payload)
    result = await db.execute(
        """UPDATE questionnaires SET title=:title,definition=:definition WHERE id=:id::uuid
           RETURNING id,title,researcher_id,status::text AS status,definition,created_on""",
        {"title": definition["title"], "definition": definition, "id": questionnaire_id},
    )
    return dict(result.rows[0])


@router.post("/{questionnaire_id}/publish")
async def publish(questionnaire_id: UUID, researcher: VerifiedResearcherDependency, db: DatabaseDependency):
    current = await owned(db, questionnaire_id, researcher.id)
    if current["status"] == "archived":
        raise HTTPException(status_code=409, detail="An archived questionnaire cannot be published")
    try:
        validate_questionnaire(current["definition"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await db.execute(
        """UPDATE questionnaires SET status='published'::questionnaire_status WHERE id=:id::uuid
           RETURNING id,title,researcher_id,status::text AS status,definition,created_on""",
        {"id": questionnaire_id},
    )
    return dict(result.rows[0])

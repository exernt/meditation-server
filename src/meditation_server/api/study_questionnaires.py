from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException

from meditation_server.api.dependencies import CurrentUserDependency, DatabaseDependency, StudyOwnerDependency
from meditation_server.api.models import IssueQuestionnaireRequest, SubmitAnswersRequest
from meditation_server.domain.questionnaires import validate_response


router = APIRouter(prefix="/studies/{study_id}/questionnaires", tags=["study-questionnaires"])


async def issuance(db, study_id: UUID, issuance_id: UUID, *, transaction=None):
    result = await db.execute(
        """SELECT sq.id,sq.study_id,sq.questionnaire_id,sq.round_number,sq.issued_at,sq.revoked_at,
                  q.title,q.status::text AS questionnaire_status,q.definition
           FROM study_questionnaires sq JOIN questionnaires q ON q.id=sq.questionnaire_id
           WHERE sq.id=:id::uuid AND sq.study_id=:study::uuid""",
        {"id": issuance_id, "study": study_id}, transaction=transaction,
    )
    if not result.rows:
        raise HTTPException(status_code=404, detail="No such questionnaire issuance for this study")
    return result.rows[0]


async def access(db, study_id: UUID, user_id: UUID) -> str:
    result = await db.execute(
        """SELECT
             EXISTS(SELECT 1 FROM study_registrations WHERE study_id=:study::uuid AND user_id=:user::uuid AND withdrawn_at IS NULL) AS participant,
             EXISTS(SELECT 1 FROM study_created_by WHERE study_id=:study::uuid AND researcher_id=:user::uuid) AS owner""",
        {"study": study_id, "user": user_id},
    )
    row = result.rows[0]
    if row["participant"]:
        return "participant"
    if row["owner"]:
        return "owner"
    raise HTTPException(status_code=403, detail="You are not an active participant in or owner of this study")


def summary(row, has_responded=None):
    return {
        "id": row["id"], "study_id": row["study_id"], "questionnaire_id": row["questionnaire_id"],
        "round_number": row["round_number"], "title": row["title"], "issued_at": row["issued_at"],
        "revoked_at": row["revoked_at"], "has_responded": has_responded,
    }


@router.post("", status_code=201)
async def issue(study_id: UUID, payload: IssueQuestionnaireRequest, owner: StudyOwnerDependency, db: DatabaseDependency):
    async with db.transaction() as tx:
        question = await db.execute(
            "SELECT id,title,researcher_id,status::text AS status FROM questionnaires WHERE id=:id::uuid",
            {"id": payload.questionnaire_id}, transaction=tx,
        )
        if not question.rows:
            raise HTTPException(status_code=404, detail="Questionnaire not found")
        q = question.rows[0]
        if q["researcher_id"] != owner.id:
            raise HTTPException(status_code=403, detail="You do not own this questionnaire")
        if q["status"] != "published":
            raise HTTPException(status_code=409, detail="Only published questionnaires can be issued")
        study = await db.execute("SELECT status::text AS status FROM study WHERE id=:id::uuid FOR UPDATE", {"id": study_id}, transaction=tx)
        if study.rows[0]["status"] not in {"recruiting", "active"}:
            raise HTTPException(status_code=409, detail="Questionnaires can only be issued to recruiting or active studies")
        current = await db.execute(
            "SELECT COALESCE(max(round_number),0) AS round FROM study_questionnaires WHERE study_id=:study::uuid AND questionnaire_id=:questionnaire::uuid",
            {"study": study_id, "questionnaire": payload.questionnaire_id}, transaction=tx,
        )
        result = await db.execute(
            """INSERT INTO study_questionnaires (id,study_id,questionnaire_id,round_number)
               VALUES (:id::uuid,:study::uuid,:questionnaire::uuid,:round)
               RETURNING id,study_id,questionnaire_id,round_number,issued_at,revoked_at""",
            {"id": uuid4(), "study": study_id, "questionnaire": payload.questionnaire_id, "round": current.rows[0]["round"] + 1}, transaction=tx,
        )
    return summary({**dict(result.rows[0]), "title": q["title"]})


@router.get("")
async def list_issued(study_id: UUID, user: CurrentUserDependency, db: DatabaseDependency):
    mode = await access(db, study_id, user.id)
    participant_filter = " AND sq.revoked_at IS NULL AND q.status='published'::questionnaire_status AND s.status IN ('recruiting'::study_status,'active'::study_status)" if mode == "participant" else ""
    result = await db.execute(
        """SELECT sq.id,sq.study_id,sq.questionnaire_id,sq.round_number,sq.issued_at,sq.revoked_at,q.title,
                  EXISTS(SELECT 1 FROM questionnaire_responses qr WHERE qr.study_questionnaire_id=sq.id AND qr.respondent_id=:user::uuid) AS has_responded
           FROM study_questionnaires sq JOIN questionnaires q ON q.id=sq.questionnaire_id JOIN study s ON s.id=sq.study_id
           WHERE sq.study_id=:study::uuid""" + participant_filter + " ORDER BY sq.issued_at DESC",
        {"user": user.id, "study": study_id},
    )
    return {"issued_questionnaires": [summary(row, row["has_responded"]) for row in result.rows]}


@router.get("/{study_questionnaire_id}")
async def get_issued(study_id: UUID, study_questionnaire_id: UUID, user: CurrentUserDependency, db: DatabaseDependency):
    mode = await access(db, study_id, user.id)
    row = await issuance(db, study_id, study_questionnaire_id)
    study = await db.execute("SELECT status::text AS status FROM study WHERE id=:id::uuid", {"id": study_id})
    if mode == "participant" and (row["revoked_at"] is not None or row["questionnaire_status"] != "published" or study.rows[0]["status"] not in {"recruiting", "active"}):
        raise HTTPException(status_code=404, detail="No open questionnaire round with this id")
    responded = await db.execute("SELECT id FROM questionnaire_responses WHERE study_questionnaire_id=:id::uuid AND respondent_id=:user::uuid", {"id": study_questionnaire_id, "user": user.id})
    return {**summary(row, bool(responded.rows)), "definition": row["definition"]}


@router.post("/{study_questionnaire_id}/revoke")
async def revoke(study_id: UUID, study_questionnaire_id: UUID, _owner: StudyOwnerDependency, db: DatabaseDependency):
    row = await issuance(db, study_id, study_questionnaire_id)
    if row["revoked_at"] is not None:
        raise HTTPException(status_code=409, detail="This questionnaire is already revoked for this study")
    result = await db.execute("UPDATE study_questionnaires SET revoked_at=now() WHERE id=:id::uuid RETURNING revoked_at", {"id": study_questionnaire_id})
    return summary({**dict(row), "revoked_at": result.rows[0]["revoked_at"]})


@router.post("/{study_questionnaire_id}/responses", status_code=201)
async def submit_response(study_id: UUID, study_questionnaire_id: UUID, payload: SubmitAnswersRequest, user: CurrentUserDependency, db: DatabaseDependency):
    row = await issuance(db, study_id, study_questionnaire_id)
    registration = await db.execute(
        """SELECT withdrawn_at,respondent_code FROM study_registrations
           WHERE user_id=:user::uuid AND study_id=:study::uuid""",
        {"user": user.id, "study": study_id},
    )
    if not registration.rows or registration.rows[0]["withdrawn_at"] is not None:
        raise HTTPException(status_code=403, detail="Only active participants in this study can submit questionnaire responses")
    if row["revoked_at"] is not None:
        raise HTTPException(status_code=409, detail="This questionnaire is no longer active for this study")
    study = await db.execute("SELECT status::text AS status FROM study WHERE id=:id::uuid", {"id": study_id})
    if study.rows[0]["status"] not in {"recruiting", "active"}:
        raise HTTPException(status_code=409, detail="This study is not currently accepting questionnaire responses")
    if row["questionnaire_status"] != "published":
        raise HTTPException(status_code=409, detail="This questionnaire is not published")
    existing = await db.execute("SELECT id FROM questionnaire_responses WHERE study_questionnaire_id=:id::uuid AND respondent_id=:user::uuid", {"id": study_questionnaire_id, "user": user.id})
    if existing.rows:
        raise HTTPException(status_code=409, detail="You have already responded to this questionnaire round")
    try:
        validate_response(row["definition"], payload.answers)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    result = await db.execute(
        """INSERT INTO questionnaire_responses
               (id,study_questionnaire_id,respondent_id,respondent_code,answers,submitted_on)
           VALUES (:id::uuid,:issuance::uuid,:user::uuid,:respondent_code,:answers,:now)
           RETURNING id,study_questionnaire_id,respondent_id,answers,submitted_on""",
        {
            "id": uuid4(),
            "issuance": study_questionnaire_id,
            "user": user.id,
            "respondent_code": registration.rows[0]["respondent_code"],
            "answers": payload.answers,
            "now": datetime.now(UTC),
        },
    )
    return dict(result.rows[0])


@router.get("/{study_questionnaire_id}/responses")
async def responses(study_id: UUID, study_questionnaire_id: UUID, _owner: StudyOwnerDependency, db: DatabaseDependency):
    await issuance(db, study_id, study_questionnaire_id)
    result = await db.execute(
        "SELECT id,study_questionnaire_id,respondent_id,answers,submitted_on FROM questionnaire_responses WHERE study_questionnaire_id=:id::uuid",
        {"id": study_questionnaire_id},
    )
    return {"responses": [dict(row) for row in result.rows]}

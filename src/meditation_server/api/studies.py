from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query

from meditation_server.api.dependencies import (
    CurrentUserDependency,
    DatabaseDependency,
    StudyOwnerDependency,
    VerifiedResearcherDependency,
)
from meditation_server.api.models import CreateStudyRequest, EditStudyRequest, JoinStudyRequest, SwapStudyGroupRequest


router = APIRouter(prefix="/studies", tags=["studies"])
VALID_STATUSES = {"draft", "recruiting", "active", "closed"}


def study_response(row):
    return {
        "id": row["id"], "name": row["name"], "duration": row["duration"],
        "description": row.get("description") or "", "status": row["status"],
        "script_generation_parameters": row.get("script_generation_parameters") or "",
    }


async def get_study_row(db, study_id: UUID, *, transaction=None, lock=False):
    suffix = " FOR UPDATE" if lock else ""
    result = await db.execute(
        """SELECT id,name,duration,description,status::text AS status,script_generation_parameters
           FROM study WHERE id=:id::uuid""" + suffix,
        {"id": study_id}, transaction=transaction,
    )
    if not result.rows:
        raise HTTPException(status_code=404, detail="Study not found")
    return result.rows[0]


@router.post("", status_code=201)
async def create_study(payload: CreateStudyRequest, researcher: VerifiedResearcherDependency, db: DatabaseDependency):
    study_id = uuid4()
    async with db.transaction() as tx:
        result = await db.execute(
            """INSERT INTO study (id,name,duration) VALUES (:id::uuid,:name,:duration)
               RETURNING id,name,duration,description,status::text AS status,script_generation_parameters""",
            {"id": study_id, "name": payload.name, "duration": payload.duration}, transaction=tx,
        )
        await db.execute("INSERT INTO study_created_by (researcher_id,study_id) VALUES (:user::uuid,:study::uuid)", {"user": researcher.id, "study": study_id}, transaction=tx)
        await db.execute("INSERT INTO consent_form (study_id,body_text,version) VALUES (:study::uuid,:body,1)", {"study": study_id, "body": "Placeholder consent text — to be finalized."}, transaction=tx)
    return study_response(result.rows[0])


@router.get("")
async def list_studies(
    user: CurrentUserDependency,
    db: DatabaseDependency,
    status_filter: str | None = Query(default=None, alias="status"),
    owner: str | None = Query(default=None),
):
    if status_filter is not None and status_filter not in VALID_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid study status")
    if owner == "me" and user.role != "researcher":
        raise HTTPException(status_code=403, detail="Only researchers can filter studies by ownership")
    clauses, parameters = [], {}
    join = ""
    if owner == "me":
        join = " JOIN study_created_by scb ON scb.study_id=s.id"
        clauses.append("scb.researcher_id=:owner::uuid")
        parameters["owner"] = user.id
    if status_filter:
        clauses.append("s.status=:status::study_status")
        parameters["status"] = status_filter
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    result = await db.execute(
        "SELECT s.id,s.name,s.duration,s.description,s.status::text AS status,s.script_generation_parameters FROM study s" + join + where,
        parameters,
    )
    return {"studies": [study_response(row) for row in result.rows]}


@router.get("/mine")
async def my_studies(user: CurrentUserDependency, db: DatabaseDependency):
    result = await db.execute(
        """SELECT s.id AS study_id,s.name,s.duration,s.description,s.status::text AS status,
                  sr.study_group::text AS study_group,sr.registered_at,sr.consented_at,sr.withdrawn_at
           FROM study_registrations sr JOIN study s ON s.id=sr.study_id
           WHERE sr.user_id=:user::uuid""",
        {"user": user.id},
    )
    studies = []
    for row in result.rows:
        open_items = []
        if row["withdrawn_at"] is None and row["status"] in {"recruiting", "active"}:
            open_result = await db.execute(
                """SELECT sq.id,sq.study_id,sq.questionnaire_id,sq.round_number,q.title,sq.issued_at,sq.revoked_at,
                          EXISTS(SELECT 1 FROM questionnaire_responses qr WHERE qr.study_questionnaire_id=sq.id AND qr.respondent_id=:user::uuid) AS has_responded
                   FROM study_questionnaires sq JOIN questionnaires q ON q.id=sq.questionnaire_id
                   WHERE sq.study_id=:study::uuid AND sq.revoked_at IS NULL AND q.status='published'::questionnaire_status
                   ORDER BY sq.issued_at DESC""",
                {"user": user.id, "study": row["study_id"]},
            )
            open_items = [dict(item) for item in open_result.rows]
        studies.append({**dict(row), "description": row["description"] or "", "open_questionnaires": open_items})
    return {"studies": studies}


@router.get("/current-script-parameters")
async def current_script_parameters(user: CurrentUserDependency, db: DatabaseDependency):
    result = await db.execute(
        """SELECT s.id AS study_id,s.status::text AS study_status,s.script_generation_parameters
           FROM study_registrations sr JOIN study s ON s.id=sr.study_id
           WHERE sr.user_id=:user::uuid AND sr.withdrawn_at IS NULL
             AND s.status IN ('recruiting'::study_status,'active'::study_status)
           ORDER BY sr.registered_at DESC LIMIT 1""",
        {"user": user.id},
    )
    if not result.rows:
        return {"participating": False, "study_id": None, "study_status": None, "script_generation_parameters": ""}
    row = result.rows[0]
    return {"participating": True, **dict(row), "script_generation_parameters": row["script_generation_parameters"] or ""}


@router.get("/{study_id}")
async def get_study(study_id: UUID, _user: CurrentUserDependency, db: DatabaseDependency):
    return study_response(await get_study_row(db, study_id))


@router.post("/{study_id}/join", status_code=201)
async def join_study(study_id: UUID, payload: JoinStudyRequest, user: CurrentUserDependency, db: DatabaseDependency):
    if not payload.agrees_to_consent:
        raise HTTPException(status_code=400, detail="You must agree to the consent form to join this study")
    async with db.transaction() as tx:
        study = await get_study_row(db, study_id, transaction=tx, lock=True)
        if study["status"] not in {"recruiting", "active"}:
            raise HTTPException(status_code=409, detail="This study is not currently accepting participants")
        await db.execute("SELECT id FROM users WHERE id=:user::uuid FOR UPDATE", {"user": user.id}, transaction=tx)
        current = await db.execute(
            """SELECT sr.study_id FROM study_registrations sr JOIN study s ON s.id=sr.study_id
               WHERE sr.user_id=:user::uuid AND sr.study_id<>:study::uuid AND sr.withdrawn_at IS NULL
                 AND s.status IN ('recruiting'::study_status,'active'::study_status) LIMIT 1""",
            {"user": user.id, "study": study_id}, transaction=tx,
        )
        if current.rows:
            raise HTTPException(status_code=409, detail="You are already participating in another study")
        existing = await db.execute("SELECT withdrawn_at FROM study_registrations WHERE user_id=:user::uuid AND study_id=:study::uuid FOR UPDATE", {"user": user.id, "study": study_id}, transaction=tx)
        if existing.rows and existing.rows[0]["withdrawn_at"] is None:
            raise HTTPException(status_code=409, detail="User is already registered for this study")
        now = datetime.now(UTC)
        if existing.rows:
            result = await db.execute(
                """UPDATE study_registrations SET registered_at=:now,consented_at=:now,withdrawn_at=NULL
                   WHERE user_id=:user::uuid AND study_id=:study::uuid
                   RETURNING user_id,study_id,registered_at,consented_at,withdrawn_at""",
                {"now": now, "user": user.id, "study": study_id}, transaction=tx,
            )
        else:
            counts = await db.execute(
                """SELECT count(*) FILTER (WHERE study_group='control'::study_group) AS control_count,
                          count(*) FILTER (WHERE study_group='experimental'::study_group) AS experimental_count
                   FROM study_registrations WHERE study_id=:study::uuid""",
                {"study": study_id}, transaction=tx,
            )
            count = counts.rows[0]
            group = "control" if count["control_count"] <= count["experimental_count"] else "experimental"
            result = await db.execute(
                """INSERT INTO study_registrations (user_id,study_id,registered_at,consented_at,study_group)
                   VALUES (:user::uuid,:study::uuid,:now,:now,:group::study_group)
                   RETURNING user_id,study_id,registered_at,consented_at,withdrawn_at""",
                {"user": user.id, "study": study_id, "now": now, "group": group}, transaction=tx,
            )
    return dict(result.rows[0])


@router.post("/{study_id}/getstatus")
async def study_status(study_id: UUID, _owner: StudyOwnerDependency, db: DatabaseDependency):
    study = await get_study_row(db, study_id)
    people = await db.execute(
        """SELECT sr.study_group::text AS study_group,u.id AS user_id,u.gender,u.age
           FROM study_registrations sr JOIN users u ON u.id=sr.user_id
           WHERE sr.study_id=:study::uuid AND sr.withdrawn_at IS NULL ORDER BY sr.registered_at,u.id""",
        {"study": study_id},
    )
    groups = {"control": [], "experimental": []}
    for row in people.rows:
        groups[row["study_group"]].append({"user_id": row["user_id"], "gender": row["gender"], "age": row["age"]})
    return {"name": study["name"], "duration": study["duration"], "groups": groups}


@router.post("/{study_id}/swapgroup")
async def swap_group(study_id: UUID, payload: SwapStudyGroupRequest, _owner: StudyOwnerDependency, db: DatabaseDependency):
    group = payload.target_group.strip().casefold()
    if group not in {"control", "experimental"}:
        raise HTTPException(status_code=422, detail="Target group must be one of: control, experimental")
    result = await db.execute(
        """UPDATE study_registrations SET study_group=:group::study_group
           WHERE user_id=:user::uuid AND study_id=:study::uuid RETURNING user_id""",
        {"group": group, "user": payload.user_id, "study": study_id},
    )
    if not result.rows:
        raise HTTPException(status_code=404, detail="User is not registered for this study")
    return {"study_id": study_id, "user_id": payload.user_id, "group": group}


@router.post("/{study_id}/edit")
async def edit_study(study_id: UUID, payload: EditStudyRequest, _owner: StudyOwnerDependency, db: DatabaseDependency):
    existing_columns = {"id", "name", "duration", "description", "script_generation_parameters", "status"}
    editable = {"name": str, "duration": int, "description": str, "script_generation_parameters": str}
    if payload.column not in existing_columns:
        raise HTTPException(status_code=400, detail=f"Study column '{payload.column}' does not exist")
    expected = editable.get(payload.column)
    if expected is None:
        raise HTTPException(status_code=400, detail=f"Study column '{payload.column}' cannot be edited")
    if type(payload.value) is not expected:
        raise HTTPException(status_code=422, detail=f"Study column '{payload.column}' requires a {expected.__name__} value")
    value = payload.value
    if payload.column == "name":
        value = value.strip()
        if not value or len(value) > 255:
            raise HTTPException(status_code=422, detail="Study name must contain between 1 and 255 characters")
    elif payload.column in {"description", "script_generation_parameters"}:
        value = value.strip()
        if len(value) > 10000:
            label = "description" if payload.column == "description" else "script generation parameters"
            raise HTTPException(status_code=422, detail=f"Study {label} cannot exceed 10000 characters")
    elif value <= 0:
        raise HTTPException(status_code=422, detail="Study duration must be greater than zero")
    # Column names come exclusively from the allow-list above.
    result = await db.execute(
        f"""UPDATE study SET {payload.column}=:value WHERE id=:id::uuid
            RETURNING id,name,duration,description,status::text AS status,script_generation_parameters""",
        {"value": value, "id": study_id},
    )
    return study_response(result.rows[0])


async def transition(db, study_id: UUID, current: set[str], target: str):
    async with db.transaction() as tx:
        row = await get_study_row(db, study_id, transaction=tx, lock=True)
        if row["status"] not in current:
            raise HTTPException(status_code=409, detail=f"Cannot move study from '{row['status']}' to '{target}'")
        result = await db.execute(
            """UPDATE study SET status=:target::study_status WHERE id=:id::uuid
               RETURNING id,name,duration,description,status::text AS status,script_generation_parameters""",
            {"target": target, "id": study_id}, transaction=tx,
        )
    return study_response(result.rows[0])


@router.post("/{study_id}/publish")
async def publish(study_id: UUID, _owner: StudyOwnerDependency, db: DatabaseDependency):
    return await transition(db, study_id, {"draft"}, "recruiting")


@router.post("/{study_id}/activate")
async def activate(study_id: UUID, _owner: StudyOwnerDependency, db: DatabaseDependency):
    return await transition(db, study_id, {"recruiting"}, "active")


@router.post("/{study_id}/close")
async def close(study_id: UUID, _owner: StudyOwnerDependency, db: DatabaseDependency):
    return await transition(db, study_id, {"recruiting", "active"}, "closed")


@router.get("/{study_id}/consent-form")
async def consent(study_id: UUID, _user: CurrentUserDependency, db: DatabaseDependency):
    result = await db.execute("SELECT study_id,body_text,version FROM consent_form WHERE study_id=:id::uuid", {"id": study_id})
    if not result.rows:
        raise HTTPException(status_code=404, detail="Consent form not found")
    return dict(result.rows[0])


@router.post("/{study_id}/leave")
async def leave(study_id: UUID, user: CurrentUserDependency, db: DatabaseDependency):
    result = await db.execute(
        """UPDATE study_registrations SET withdrawn_at=now()
           WHERE user_id=:user::uuid AND study_id=:study::uuid AND withdrawn_at IS NULL
           RETURNING user_id,study_id,registered_at,consented_at,withdrawn_at""",
        {"user": user.id, "study": study_id},
    )
    if result.rows:
        return dict(result.rows[0])
    found = await db.execute("SELECT withdrawn_at FROM study_registrations WHERE user_id=:user::uuid AND study_id=:study::uuid", {"user": user.id, "study": study_id})
    if not found.rows:
        raise HTTPException(status_code=404, detail="You are not registered for this study")
    raise HTTPException(status_code=409, detail="You have already left this study")


@router.get("/{study_id}/feedback")
async def feedback(study_id: UUID, _owner: StudyOwnerDependency, db: DatabaseDependency):
    result = await db.execute(
        "SELECT id,user_id,study_id,category::text AS category,rating,comment,created_at FROM feedback WHERE study_id=:study::uuid",
        {"study": study_id},
    )
    return {"feedback": [dict(row) for row in result.rows]}

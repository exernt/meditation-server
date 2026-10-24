from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException

from meditation_server.api.dependencies import CurrentUserDependency, DatabaseDependency
from meditation_server.api.models import FeedbackRequest


router = APIRouter(prefix="/feedback", tags=["feedback"])


@router.post("", status_code=201)
async def submit(payload: FeedbackRequest, user: CurrentUserDependency, db: DatabaseDependency):
    if payload.study_id is not None:
        found = await db.execute("SELECT id FROM study WHERE id=:id::uuid", {"id": payload.study_id})
        if not found.rows:
            raise HTTPException(status_code=404, detail="Study not found")
    result = await db.execute(
        """INSERT INTO feedback (id,user_id,study_id,category,rating,comment)
           VALUES (:id::uuid,:user::uuid,:study::uuid,:category::feedback_category,:rating,:comment)
           RETURNING id,user_id,study_id,category::text AS category,rating,comment,created_at""",
        {"id": uuid4(), "user": user.id, "study": payload.study_id, "category": payload.category, "rating": payload.rating, "comment": payload.comment},
    )
    return dict(result.rows[0])

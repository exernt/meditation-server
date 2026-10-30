from __future__ import annotations

import logging
import os
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError

from meditation_server.api.auth import (
    _assert_login_allowed,
    _record_event,
    _record_login_failure,
)
from meditation_server.api.dependencies import (
    DatabaseDependency,
    StudyOwnerDependency,
    VerifiedResearcherDependency,
)
from meditation_server.api.models import LoginRequest
from meditation_server.application.response_exports import (
    publish_due_export_outbox,
    request_response_export,
)
from meditation_server.auth.security import (
    hash_password,
    login_subjects,
    password_needs_rehash,
    verify_password,
    verify_unknown_user_password,
)
from meditation_server.integrations.email import send_response_export_ready
from meditation_server.integrations.response_export_storage import response_export_download_url


logger = logging.getLogger(__name__)
router = APIRouter(tags=["response-exports"])


def _export_response(row: dict, *, cached: bool) -> dict:
    return {
        "id": row["id"],
        "scope": row["scope"],
        "study_id": row["study_id"],
        "status": row["status"],
        "filename": row["filename"],
        "created_at": row["created_at"],
        "generated_at": row["generated_at"],
        "expires_at": row["expires_at"],
        "cached": cached,
    }


@router.post("/response-exports", status_code=status.HTTP_202_ACCEPTED)
async def request_all_studies_export(
    researcher: VerifiedResearcherDependency,
    db: DatabaseDependency,
):
    return await _request_export(db, researcher_id=researcher.id, researcher_email=researcher.email)


@router.post("/studies/{study_id}/response-export", status_code=status.HTTP_202_ACCEPTED)
async def request_study_export(
    study_id: UUID,
    owner: StudyOwnerDependency,
    db: DatabaseDependency,
):
    return await _request_export(
        db,
        researcher_id=owner.id,
        researcher_email=owner.email,
        study_id=study_id,
    )


async def _request_export(
    db,
    *,
    researcher_id: UUID,
    researcher_email: str,
    study_id: UUID | None = None,
) -> dict:
    result = await request_response_export(
        db,
        researcher_id=researcher_id,
        scope="study" if study_id is not None else "all_studies",
        study_id=study_id,
    )
    export_id = result.export["id"]
    if result.cached:
        try:
            await send_response_export_ready(researcher_email, str(export_id))
        except Exception:
            logger.exception("Failed to resend cached response export email")
    else:
        publish = await publish_due_export_outbox(
            db,
            queue_url=os.getenv("RESPONSE_EXPORT_QUEUE_URL", ""),
            export_id=export_id,
        )
        if publish.failed:
            logger.warning("Response export was queued but immediate outbox publication failed")
    return _export_response(result.export, cached=result.cached)


@router.get("/response-exports/{export_id}")
async def get_response_export(
    export_id: UUID,
    researcher: VerifiedResearcherDependency,
    db: DatabaseDependency,
):
    result = await db.execute(
        """SELECT id,scope::text AS scope,study_id,status::text AS status,filename,
                  created_at,generated_at,expires_at
             FROM response_exports
            WHERE id=:export_id::uuid AND researcher_id=:researcher_id::uuid""",
        {"export_id": export_id, "researcher_id": researcher.id},
    )
    if not result.rows:
        raise HTTPException(status_code=404, detail="Response export not found")
    return _export_response(dict(result.rows[0]), cached=False)


@router.get("/response-exports/{export_id}/download", response_class=HTMLResponse)
async def response_export_login_page(export_id: UUID):
    return _login_page(export_id)


@router.post("/response-exports/{export_id}/download", response_model=None)
async def response_export_login(
    export_id: UUID,
    request: Request,
    db: DatabaseDependency,
):
    try:
        payload = await _login_payload(request)
        user = await _authenticate_export_researcher(
            db,
            payload=payload,
            source_ip=request.client.host if request.client else "unknown",
        )
        export = await db.execute(
            """SELECT export.s3_key,export.filename
                 FROM response_exports AS export
                WHERE export.id=:export_id::uuid
                  AND export.researcher_id=:researcher_id::uuid
                  AND export.status='complete'::response_export_status
                  AND export.expires_at > now()
                  AND (
                      export.scope='all_studies'::response_export_scope
                      OR EXISTS (
                          SELECT 1 FROM study_created_by AS ownership
                           WHERE ownership.study_id=export.study_id
                             AND ownership.researcher_id=export.researcher_id
                      )
                  )""",
            {"export_id": export_id, "researcher_id": user["id"]},
        )
        if not export.rows:
            raise ValueError("Export is unavailable")
        row = export.rows[0]
        url = response_export_download_url(key=row["s3_key"], filename=row["filename"])
        await _record_event(db, "response_export_downloaded", user_id=user["id"])
        return RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    except HTTPException as exc:
        return _login_page(
            export_id,
            failed=True,
            status_code=exc.status_code if exc.status_code == 429 else 401,
            headers=exc.headers,
        )
    except (ValidationError, ValueError, UnicodeDecodeError):
        return _login_page(export_id, failed=True, status_code=401)


async def _login_payload(request: Request) -> LoginRequest:
    body = await request.body()
    if len(body) > 16_384:
        raise ValueError("Login form is too large")
    values = parse_qs(body.decode("utf-8"), keep_blank_values=True, max_num_fields=2)
    return LoginRequest.model_validate(
        {
            "email": (values.get("email") or [""])[0],
            "password": (values.get("password") or [""])[0],
        }
    )


async def _authenticate_export_researcher(db, *, payload: LoginRequest, source_ip: str):
    subjects = login_subjects(str(payload.email), source_ip)
    transaction = await db.begin_transaction()
    try:
        try:
            await _assert_login_allowed(db, subjects, transaction)
        except HTTPException:
            await db.rollback(transaction)
            raise
        found = await db.execute(
            """SELECT users.id,users.password_hash,users.role::text AS role,
                      users.email_verified,users.disabled_at,profile.verified_at
                 FROM users
                 LEFT JOIN researcher_profiles AS profile ON profile.user_id=users.id
                WHERE users.email=:email
                FOR UPDATE OF users""",
            {"email": str(payload.email)},
            transaction=transaction,
        )
        if not found.rows:
            await verify_unknown_user_password(payload.password)
            await _record_login_failure(db, subjects, None, transaction)
            await db.commit(transaction)
            raise HTTPException(status_code=401, detail="Login failed")
        user = found.rows[0]
        valid_password = await verify_password(payload.password, user["password_hash"])
        permitted = (
            valid_password
            and user["disabled_at"] is None
            and user["email_verified"]
            and user["role"] == "researcher"
            and user["verified_at"] is not None
        )
        if not permitted:
            await _record_login_failure(db, subjects, user["id"], transaction)
            await db.commit(transaction)
            raise HTTPException(status_code=401, detail="Login failed")
        if password_needs_rehash(user["password_hash"]):
            await db.execute(
                "UPDATE users SET password_hash=:password_hash WHERE id=:id::uuid",
                {
                    "password_hash": await hash_password(payload.password),
                    "id": user["id"],
                },
                transaction=transaction,
            )
        await db.execute(
            "DELETE FROM login_throttles WHERE subject_hash=:account",
            {"account": subjects[0]},
            transaction=transaction,
        )
        await _record_event(
            db,
            "response_export_login_succeeded",
            user_id=user["id"],
            subject_hash=subjects[0],
            transaction=transaction,
        )
        await db.commit(transaction)
        return user
    except HTTPException:
        raise
    except BaseException:
        await db.rollback(transaction)
        raise


def _login_page(
    export_id: UUID,
    *,
    failed: bool = False,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    failure = '<p style="color:red">Login failed.</p>' if failed else ""
    body = f"""<!doctype html><html><head><meta charset="utf-8"><title>Download response export</title></head>
<body>{failure}<form method="post">
<label>Email <input type="email" name="email" required autocomplete="username"></label><br>
<label>Password <input type="password" name="password" required autocomplete="current-password"></label><br>
<button type="submit">Log In</button></form></body></html>"""
    response_headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; form-action 'self'; style-src 'unsafe-inline'",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
    }
    if headers:
        response_headers.update(headers)
    return HTMLResponse(body, status_code=status_code, headers=response_headers)

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse

from meditation_server import settings
from meditation_server.api.dependencies import (
    CurrentUserDependency,
    DatabaseDependency,
    require_role,
)
from meditation_server.api.models import (
    EmailRequest,
    LoginRequest,
    RegisterRequest,
    ResearcherRequest,
    ResetConfirmRequest,
    TokenRequest,
    VerificationConfirmRequest,
)
from meditation_server.auth.security import (
    create_access_token,
    generate_token,
    hash_password,
    hash_token,
    login_subjects,
    password_needs_rehash,
    refresh_expiry,
    reset_expiry,
    verification_expiry,
    verify_password,
    verify_unknown_user_password,
)
from meditation_server.integrations.email import send_password_reset, send_verification


router = APIRouter(prefix="/auth", tags=["authentication"])


def _message(message: str) -> dict[str, str]:
    return {"message": message}


async def _record_event(db, event_type: str, *, user_id=None, subject_hash=None, transaction=None) -> None:
    await db.execute(
        """INSERT INTO security_events (user_id, event_type, subject_hash)
           VALUES (:user_id::uuid, :event_type, :subject_hash)""",
        {"user_id": user_id, "event_type": event_type, "subject_hash": subject_hash},
        transaction=transaction,
    )


async def _assert_login_allowed(db, subjects: tuple[str, str], transaction) -> None:
    rows = await db.execute(
        """SELECT subject_hash, blocked_until FROM login_throttles
           WHERE subject_hash IN (:account, :ip) FOR UPDATE""",
        {"account": subjects[0], "ip": subjects[1]},
        transaction=transaction,
    )
    now = datetime.now(UTC)
    for row in rows.rows:
        if row["blocked_until"] is not None and row["blocked_until"] > now:
            retry_after = int((row["blocked_until"] - now).total_seconds()) + 1
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again later.",
                headers={"Retry-After": str(max(1, retry_after))},
            )


async def _record_login_failure(db, subjects, user_id, transaction) -> None:
    now = datetime.now(UTC)
    keys = subjects if user_id is not None else subjects[1:]
    for key in keys:
        result = await db.execute(
            """INSERT INTO login_throttles (subject_hash, failed_attempts, updated_on)
               VALUES (:key, 1, :now)
               ON CONFLICT (subject_hash) DO UPDATE SET
                 failed_attempts = CASE
                   WHEN login_throttles.updated_on < :window_start THEN 1
                   ELSE login_throttles.failed_attempts + 1 END,
                 updated_on = :now
               RETURNING failed_attempts""",
            {"key": key, "now": now, "window_start": now - timedelta(minutes=15)},
            transaction=transaction,
        )
        attempts = result.rows[0]["failed_attempts"]
        delay = 3600 if attempts >= 12 else 600 if attempts >= 8 else 60 if attempts >= 5 else 0
        await db.execute(
            "UPDATE login_throttles SET blocked_until = :blocked WHERE subject_hash = :key",
            {"blocked": now + timedelta(seconds=delay) if delay else None, "key": key},
            transaction=transaction,
        )
    await _record_event(db, "login_failed", user_id=user_id, subject_hash=subjects[0], transaction=transaction)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, db: DatabaseDependency):
    existing = await db.execute("SELECT id FROM users WHERE email = :email", {"email": str(payload.email)})
    if existing.rows:
        raise HTTPException(status_code=409, detail="Email is already registered")
    user_id = uuid4()
    raw_token = generate_token()
    role = "researcher" if payload.researcher_institution else "participant"
    async with db.transaction() as tx:
        await db.execute(
            """INSERT INTO users (id, email, password_hash, gender, age, role)
               VALUES (:id::uuid, :email, :password_hash, :gender, :age, :role::user_role)""",
            {
                "id": user_id,
                "email": str(payload.email),
                "password_hash": await hash_password(payload.password),
                "gender": payload.gender,
                "age": payload.age,
                "role": role,
            },
            transaction=tx,
        )
        if payload.researcher_institution:
            await db.execute(
                "INSERT INTO researcher_profiles (user_id, institution) VALUES (:id::uuid, :institution)",
                {"id": user_id, "institution": payload.researcher_institution},
                transaction=tx,
            )
        await db.execute(
            """INSERT INTO email_verification_tokens (user_id, token_hash, expires_at)
               VALUES (:id::uuid, :token_hash, :expires_at)""",
            {"id": user_id, "token_hash": hash_token(raw_token), "expires_at": verification_expiry()},
            transaction=tx,
        )
    try:
        await send_verification(str(payload.email), raw_token)
    except Exception:
        pass
    return _message("Registration successful; verify your email before signing in")


@router.get("/email-verification/open", response_class=HTMLResponse)
async def verification_page():
    return HTMLResponse(
        """<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Verify email - Walking Meditation</title></head>
<body><p id="status">Verifying your email address…</p><script>
const token=new URLSearchParams(window.location.hash.slice(1)).get("token");const n=document.getElementById("status");
if(!token){n.textContent="This verification link is incomplete.";}else{fetch("confirm",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token})}).then(r=>{n.textContent=r.ok?"Email verified. You can return to Walking Meditation and sign in.":"This verification link is invalid or has expired.";history.replaceState(null,"",window.location.pathname);}).catch(()=>n.textContent="Verification is temporarily unavailable.");}</script></body></html>""",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; connect-src 'self'",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/email-verification/confirm")
async def confirm_verification(payload: VerificationConfirmRequest, db: DatabaseDependency):
    async with db.transaction() as tx:
        result = await db.execute(
            """SELECT id, user_id, expires_at, used_at FROM email_verification_tokens
               WHERE token_hash = :token_hash FOR UPDATE""",
            {"token_hash": hash_token(payload.token)}, transaction=tx,
        )
        if not result.rows or result.rows[0]["used_at"] is not None or result.rows[0]["expires_at"] < datetime.now(UTC):
            raise HTTPException(status_code=400, detail="Verification token is invalid or has expired")
        row = result.rows[0]
        await db.execute("UPDATE users SET email_verified = true WHERE id = :id::uuid", {"id": row["user_id"]}, transaction=tx)
        await db.execute("UPDATE email_verification_tokens SET used_at = now() WHERE id = :id::uuid", {"id": row["id"]}, transaction=tx)
        await _record_event(db, "email_verified", user_id=row["user_id"], transaction=tx)
    return _message("Email verified")


@router.post("/email-verification/resend")
async def resend_verification(payload: EmailRequest, db: DatabaseDependency):
    user = await db.execute("SELECT id, email_verified FROM users WHERE email = :email", {"email": str(payload.email)})
    raw_token = None
    if user.rows and not user.rows[0]["email_verified"]:
        recent = await db.execute(
            """SELECT id FROM email_verification_tokens
               WHERE user_id = :id::uuid AND created_on >= :since LIMIT 1""",
            {"id": user.rows[0]["id"], "since": datetime.now(UTC) - timedelta(seconds=settings.request_cooldown_seconds())},
        )
        if not recent.rows:
            raw_token = generate_token()
            async with db.transaction() as tx:
                await db.execute("UPDATE email_verification_tokens SET used_at=now() WHERE user_id=:id::uuid AND used_at IS NULL", {"id": user.rows[0]["id"]}, transaction=tx)
                await db.execute(
                    "INSERT INTO email_verification_tokens (user_id, token_hash, expires_at) VALUES (:id::uuid,:hash,:expires)",
                    {"id": user.rows[0]["id"], "hash": hash_token(raw_token), "expires": verification_expiry()}, transaction=tx,
                )
    if raw_token:
        try:
            await send_verification(str(payload.email), raw_token)
        except Exception:
            pass
    return _message("If that account needs verification, a new link has been sent.")


@router.post("/password-reset/request")
async def request_reset(payload: EmailRequest, db: DatabaseDependency):
    generic = _message("If that email is registered, a reset link has been sent.")
    user = await db.execute("SELECT id FROM users WHERE email = :email", {"email": str(payload.email)})
    if not user.rows:
        return generic
    user_id = user.rows[0]["id"]
    recent = await db.execute(
        "SELECT id FROM password_reset_tokens WHERE user_id=:id::uuid AND created_on>=:since LIMIT 1",
        {"id": user_id, "since": datetime.now(UTC) - timedelta(seconds=settings.request_cooldown_seconds())},
    )
    if recent.rows:
        return generic
    raw_token = generate_token()
    async with db.transaction() as tx:
        await db.execute("UPDATE password_reset_tokens SET used_at=now() WHERE user_id=:id::uuid AND used_at IS NULL", {"id": user_id}, transaction=tx)
        await db.execute(
            "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES (:id::uuid,:hash,:expires)",
            {"id": user_id, "hash": hash_token(raw_token), "expires": reset_expiry()}, transaction=tx,
        )
    try:
        await send_password_reset(str(payload.email), raw_token)
    except Exception:
        await db.execute("UPDATE password_reset_tokens SET used_at=now() WHERE token_hash=:hash", {"hash": hash_token(raw_token)})
    return generic


@router.post("/password-reset/confirm")
async def confirm_reset(payload: ResetConfirmRequest, db: DatabaseDependency):
    async with db.transaction() as tx:
        token = await db.execute(
            "SELECT id,user_id,expires_at,used_at FROM password_reset_tokens WHERE token_hash=:hash FOR UPDATE",
            {"hash": hash_token(payload.token)}, transaction=tx,
        )
        if not token.rows or token.rows[0]["used_at"] is not None or token.rows[0]["expires_at"] < datetime.now(UTC):
            raise HTTPException(status_code=400, detail="Reset token is invalid or has expired")
        row = token.rows[0]
        await db.execute("SELECT id FROM users WHERE id=:id::uuid FOR UPDATE", {"id": row["user_id"]}, transaction=tx)
        await db.execute(
            "UPDATE users SET password_hash=:password_hash, session_version=session_version+1 WHERE id=:id::uuid",
            {"password_hash": await hash_password(payload.new_password), "id": row["user_id"]}, transaction=tx,
        )
        await db.execute("UPDATE password_reset_tokens SET used_at=now() WHERE id=:id::uuid", {"id": row["id"]}, transaction=tx)
        await db.execute("UPDATE refresh_tokens SET revoked_at=now() WHERE user_id=:id::uuid AND revoked_at IS NULL", {"id": row["user_id"]}, transaction=tx)
        await _record_event(db, "password_reset", user_id=row["user_id"], transaction=tx)
    return _message("Password has been reset")


@router.post("/login")
async def login(request: Request, payload: LoginRequest, db: DatabaseDependency):
    source_ip = request.client.host if request.client else "unknown"
    subjects = login_subjects(str(payload.email), source_ip)
    tx = await db.begin_transaction()
    try:
        try:
            await _assert_login_allowed(db, subjects, tx)
        except HTTPException:
            await db.rollback(tx)
            raise
        found = await db.execute(
            """SELECT id,email,password_hash,session_version,role::text AS role,email_verified,disabled_at
               FROM users WHERE email=:email FOR UPDATE""",
            {"email": str(payload.email)}, transaction=tx,
        )
        if not found.rows:
            await verify_unknown_user_password(payload.password)
            await _record_login_failure(db, subjects, None, tx)
            await db.commit(tx)
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user = found.rows[0]
        if not await verify_password(payload.password, user["password_hash"]) or user["disabled_at"] is not None:
            await _record_login_failure(db, subjects, user["id"], tx)
            await db.commit(tx)
            raise HTTPException(status_code=401, detail="Invalid email or password")
        if not user["email_verified"]:
            await db.rollback(tx)
            raise HTTPException(status_code=403, detail="Email verification is required")
        if password_needs_rehash(user["password_hash"]):
            await db.execute("UPDATE users SET password_hash=:hash WHERE id=:id::uuid", {"hash": await hash_password(payload.password), "id": user["id"]}, transaction=tx)
        session = await db.execute(
            """UPDATE users SET session_version=session_version+1
               WHERE id=:id::uuid RETURNING session_version""",
            {"id": user["id"]},
            transaction=tx,
        )
        session_version = session.rows[0]["session_version"]
        await db.execute(
            """UPDATE refresh_tokens SET revoked_at=now()
               WHERE user_id=:id::uuid AND revoked_at IS NULL""",
            {"id": user["id"]},
            transaction=tx,
        )
        raw_refresh = generate_token()
        await db.execute(
            """INSERT INTO refresh_tokens (user_id,family_id,token_hash,expires_at)
               VALUES (:id::uuid,:family::uuid,:hash,:expires)""",
            {"id": user["id"], "family": uuid4(), "hash": hash_token(raw_refresh), "expires": refresh_expiry()}, transaction=tx,
        )
        await db.execute("DELETE FROM login_throttles WHERE subject_hash=:account", {"account": subjects[0]}, transaction=tx)
        await _record_event(db, "login_succeeded", user_id=user["id"], subject_hash=subjects[0], transaction=tx)
        await db.commit(tx)
    except HTTPException:
        # Credential failures and unverified-email branches close their own
        # transaction so throttle/audit writes are not accidentally rolled back.
        raise
    except BaseException:
        await db.rollback(tx)
        raise
    return {"access_token": create_access_token(user["id"], session_version, admin=user["role"] == "admin"), "refresh_token": raw_refresh, "token_type": "bearer"}


@router.post("/logout")
async def logout(payload: TokenRequest, db: DatabaseDependency):
    row = await db.execute("SELECT family_id FROM refresh_tokens WHERE token_hash=:hash", {"hash": hash_token(payload.refresh_token)})
    if row.rows:
        await db.execute("UPDATE refresh_tokens SET revoked_at=now() WHERE family_id=:family::uuid AND revoked_at IS NULL", {"family": row.rows[0]["family_id"]})
    return _message("Logged out")


@router.post("/refresh")
async def refresh(payload: TokenRequest, db: DatabaseDependency):
    invalid = HTTPException(status_code=401, detail="Refresh token is invalid, expired, or has been revoked")
    token_hash = hash_token(payload.refresh_token)
    first = await db.execute("SELECT user_id FROM refresh_tokens WHERE token_hash=:hash", {"hash": token_hash})
    if not first.rows:
        raise invalid
    tx = await db.begin_transaction()
    try:
        user_result = await db.execute("SELECT id,role::text AS role,session_version,disabled_at FROM users WHERE id=:id::uuid FOR UPDATE", {"id": first.rows[0]["user_id"]}, transaction=tx)
        if not user_result.rows or user_result.rows[0]["disabled_at"] is not None:
            await db.rollback(tx)
            raise invalid
        token_result = await db.execute("SELECT id,user_id,family_id,expires_at,revoked_at FROM refresh_tokens WHERE token_hash=:hash FOR UPDATE", {"hash": token_hash}, transaction=tx)
        if not token_result.rows:
            await db.rollback(tx)
            raise invalid
        old = token_result.rows[0]
        if old["revoked_at"] is not None:
            await db.execute("UPDATE refresh_tokens SET revoked_at=now() WHERE family_id=:family::uuid AND revoked_at IS NULL", {"family": old["family_id"]}, transaction=tx)
            await _record_event(db, "refresh_token_replay", user_id=old["user_id"], transaction=tx)
            await db.commit(tx)
            raise invalid
        if old["expires_at"] < datetime.now(UTC):
            await db.rollback(tx)
            raise invalid
        await db.execute("UPDATE refresh_tokens SET revoked_at=now() WHERE id=:id::uuid", {"id": old["id"]}, transaction=tx)
        raw_refresh = generate_token()
        await db.execute(
            """INSERT INTO refresh_tokens (user_id,family_id,parent_id,token_hash,expires_at)
               VALUES (:user::uuid,:family::uuid,:parent::uuid,:hash,:expires)""",
            {"user": old["user_id"], "family": old["family_id"], "parent": old["id"], "hash": hash_token(raw_refresh), "expires": refresh_expiry()}, transaction=tx,
        )
        await db.commit(tx)
    except HTTPException:
        raise
    except BaseException:
        await db.rollback(tx)
        raise
    user = user_result.rows[0]
    return {"access_token": create_access_token(user["id"], user["session_version"], admin=user["role"] == "admin"), "refresh_token": raw_refresh, "token_type": "bearer"}


@router.post("/logout-all")
async def logout_all(user: CurrentUserDependency, db: DatabaseDependency):
    async with db.transaction() as tx:
        await db.execute("UPDATE users SET session_version=session_version+1 WHERE id=:id::uuid", {"id": user.id}, transaction=tx)
        await db.execute("UPDATE refresh_tokens SET revoked_at=now() WHERE user_id=:id::uuid AND revoked_at IS NULL", {"id": user.id}, transaction=tx)
        await _record_event(db, "all_sessions_revoked", user_id=user.id, transaction=tx)
    return _message("All devices have been logged out")


@router.post("/admin/users/{user_id}/disable", dependencies=[Depends(require_role("admin"))])
async def disable_user(user_id: UUID, db: DatabaseDependency):
    async with db.transaction() as tx:
        result = await db.execute("UPDATE users SET disabled_at=now(),session_version=session_version+1 WHERE id=:id::uuid RETURNING id", {"id": user_id}, transaction=tx)
        if not result.rows:
            raise HTTPException(status_code=404, detail="User not found")
        await db.execute("UPDATE refresh_tokens SET revoked_at=now() WHERE user_id=:id::uuid AND revoked_at IS NULL", {"id": user_id}, transaction=tx)
        await _record_event(db, "account_disabled", user_id=user_id, transaction=tx)
    return _message("Account disabled")


@router.post("/register-researcher", status_code=201)
async def register_researcher(payload: ResearcherRequest, user: CurrentUserDependency, db: DatabaseDependency):
    found = await db.execute("SELECT user_id FROM researcher_profiles WHERE user_id=:id::uuid", {"id": user.id})
    if found.rows:
        raise HTTPException(status_code=409, detail="Researcher profile already exists for this account")
    async with db.transaction() as tx:
        await db.execute("UPDATE users SET role='researcher'::user_role WHERE id=:id::uuid", {"id": user.id}, transaction=tx)
        await db.execute("INSERT INTO researcher_profiles (user_id,institution) VALUES (:id::uuid,:institution)", {"id": user.id, "institution": payload.institution}, transaction=tx)
        await _record_event(db, "researcher_registered", user_id=user.id, transaction=tx)
    return {"institution": payload.institution, "verified_at": None, "role": "researcher"}


@router.get("/admin/researchers/pending", dependencies=[Depends(require_role("admin"))])
async def list_pending_researchers(db: DatabaseDependency):
    result = await db.execute(
        """SELECT profile.user_id, users.email, profile.institution,
                  profile.created_on AS requested_at
           FROM researcher_profiles AS profile
           JOIN users ON users.id = profile.user_id
           WHERE profile.verified_at IS NULL
             AND users.role = 'researcher'::user_role
             AND users.disabled_at IS NULL
           ORDER BY profile.created_on ASC, profile.user_id ASC"""
    )
    return {"researchers": [dict(row) for row in result.rows]}


@router.post("/admin/researchers/{user_id}/verify", dependencies=[Depends(require_role("admin"))])
async def verify_researcher(user_id: UUID, db: DatabaseDependency):
    async with db.transaction() as tx:
        result = await db.execute("UPDATE researcher_profiles SET verified_at=now() WHERE user_id=:id::uuid RETURNING institution,verified_at", {"id": user_id}, transaction=tx)
        if not result.rows:
            raise HTTPException(status_code=404, detail="No researcher profile found for that user")
        await _record_event(db, "researcher_verified", user_id=user_id, transaction=tx)
    return {**result.rows[0], "role": "researcher"}


@router.post("/admin/researchers/{user_id}/reject", dependencies=[Depends(require_role("admin"))])
async def reject_researcher(user_id: UUID, db: DatabaseDependency):
    async with db.transaction() as tx:
        result = await db.execute(
            """DELETE FROM researcher_profiles
               WHERE user_id=:id::uuid AND verified_at IS NULL
               RETURNING user_id""",
            {"id": user_id},
            transaction=tx,
        )
        if not result.rows:
            raise HTTPException(status_code=404, detail="No pending researcher request found for that user")
        await db.execute(
            """UPDATE users
               SET role='participant'::user_role, session_version=session_version+1
               WHERE id=:id::uuid AND role='researcher'::user_role""",
            {"id": user_id},
            transaction=tx,
        )
        await db.execute(
            """UPDATE refresh_tokens SET revoked_at=now()
               WHERE user_id=:id::uuid AND revoked_at IS NULL""",
            {"id": user_id},
            transaction=tx,
        )
        await _record_event(db, "researcher_rejected", user_id=user_id, transaction=tx)
    return _message("Researcher request rejected")


@router.get("/me")
async def me(user: CurrentUserDependency, db: DatabaseDependency):
    institution = verified_at = None
    if user.role == "researcher":
        profile = await db.execute("SELECT institution,verified_at FROM researcher_profiles WHERE user_id=:id::uuid", {"id": user.id})
        if profile.rows:
            institution, verified_at = profile.rows[0]["institution"], profile.rows[0]["verified_at"]
    return {"user_id": user.id, "email": user.email, "role": user.role, "institution": institution, "verified_at": verified_at}

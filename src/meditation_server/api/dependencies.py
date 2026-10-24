from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from meditation_server.auth.security import decode_access_token
from meditation_server.repositories.asyncpg import AsyncpgExecutor
from meditation_server.repositories.executor import DatabaseExecutor
from meditation_server.repositories.factory import create_data_api_executor_from_env


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    email: str
    role: str
    session_version: int
    email_verified: bool


async def create_executor() -> DatabaseExecutor:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        return await AsyncpgExecutor.connect(database_url, min_size=1, max_size=5)
    return create_data_api_executor_from_env()


def get_database(request: Request) -> DatabaseExecutor:
    database = getattr(request.app.state, "database", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is not configured")
    return database


DatabaseDependency = Annotated[DatabaseExecutor, Depends(get_database)]
_bearer = HTTPBearer()


def invalid_access_token() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired access token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    database: DatabaseDependency,
) -> CurrentUser:
    try:
        user_id, session_version = decode_access_token(credentials.credentials)
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise invalid_access_token()
    result = await database.execute(
        """SELECT id, email, role::text AS role, session_version, email_verified, disabled_at
           FROM users WHERE id = :user_id::uuid""",
        {"user_id": user_id},
    )
    if not result.rows:
        raise invalid_access_token()
    row = result.rows[0]
    if row["disabled_at"] is not None or row["session_version"] != session_version:
        raise invalid_access_token()
    return CurrentUser(
        id=row["id"],
        email=row["email"],
        role=row["role"],
        session_version=row["session_version"],
        email_verified=row["email_verified"],
    )


CurrentUserDependency = Annotated[CurrentUser, Depends(get_current_user)]


async def get_verified_researcher(
    user: CurrentUserDependency,
    database: DatabaseDependency,
) -> CurrentUser:
    if user.role != "researcher":
        raise HTTPException(status_code=403, detail="Only researchers can perform this action")
    result = await database.execute(
        "SELECT verified_at FROM researcher_profiles WHERE user_id = :user_id::uuid",
        {"user_id": user.id},
    )
    if not result.rows or result.rows[0]["verified_at"] is None:
        raise HTTPException(status_code=403, detail="Researcher account has not been verified")
    return user


VerifiedResearcherDependency = Annotated[CurrentUser, Depends(get_verified_researcher)]


def require_role(*roles: str):
    async def check(user: CurrentUserDependency) -> CurrentUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have permission to perform this action")
        return user
    return check


async def require_study_owner(
    study_id: UUID,
    researcher: VerifiedResearcherDependency,
    database: DatabaseDependency,
) -> CurrentUser:
    result = await database.execute(
        "SELECT researcher_id FROM study_created_by WHERE study_id = :study_id::uuid",
        {"study_id": study_id},
    )
    if not result.rows:
        exists = await database.execute("SELECT id FROM study WHERE id = :study_id::uuid", {"study_id": study_id})
        if not exists.rows:
            raise HTTPException(status_code=404, detail="Study not found")
        raise HTTPException(status_code=403, detail="You do not own this study")
    if result.rows[0]["researcher_id"] != researcher.id:
        raise HTTPException(status_code=403, detail="You do not own this study")
    return researcher


StudyOwnerDependency = Annotated[CurrentUser, Depends(require_study_owner)]

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from meditation_server import settings


_password_hasher = PasswordHasher(type=Type.ID)
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$OtVhfVFfAMPJRiZ/zzwprg$"
    "ZPSsWOmF9o78NbCvC2wz8JMkHulpvHuXPn+M+Q6INMo"
)


async def hash_password(password: str) -> str:
    return await asyncio.to_thread(_password_hasher.hash, password)


async def verify_password(password: str, password_hash: str) -> bool:
    try:
        return await asyncio.to_thread(_password_hasher.verify, password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


async def verify_unknown_user_password(password: str) -> None:
    await verify_password(password, _DUMMY_PASSWORD_HASH)


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def create_access_token(user_id: UUID, session_version: int, *, admin: bool = False) -> str:
    now = datetime.now(UTC)
    kid, key, _, _ = settings.jwt_keys()
    ttl = settings.admin_access_token_ttl_minutes() if admin else settings.access_token_ttl_minutes()
    return jwt.encode(
        {
            "sub": str(user_id),
            "session_version": session_version,
            "iss": settings.jwt_issuer(),
            "aud": settings.jwt_audience(),
            "iat": now,
            "exp": now + timedelta(minutes=ttl),
        },
        key,
        algorithm=settings.jwt_algorithm(),
        headers={"kid": kid},
    )


def decode_access_token(token: str) -> tuple[UUID, int]:
    kid, current, previous_kid, previous = settings.jwt_keys()
    token_kid = jwt.get_unverified_header(token).get("kid")
    if token_kid == kid:
        key = current
    elif token_kid and token_kid == previous_kid and previous:
        key = previous
    else:
        raise jwt.InvalidTokenError("Unknown signing key")
    payload = jwt.decode(
        token,
        key,
        algorithms=[settings.jwt_algorithm()],
        issuer=settings.jwt_issuer(),
        audience=settings.jwt_audience(),
        options={"require": ["sub", "iss", "aud", "iat", "exp", "session_version"]},
    )
    return UUID(payload["sub"]), int(payload["session_version"])


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def refresh_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=settings.refresh_token_ttl_days())


def reset_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=30)


def verification_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(hours=24)


def login_subjects(email: str, source_ip: str) -> tuple[str, str]:
    pepper = settings.auth_rate_limit_pepper().encode()
    def digest(kind: str, value: str) -> str:
        normalized = value.strip().casefold()
        return hmac.new(pepper, f"{kind}:{normalized}".encode(), hashlib.sha256).hexdigest()
    return digest("account", email), digest("ip", source_ip or "unknown")

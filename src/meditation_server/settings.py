from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be greater than zero")
    return value


def access_token_ttl_minutes() -> int:
    return _positive_int("ACCESS_TOKEN_TTL_MINUTES", 15)


def admin_access_token_ttl_minutes() -> int:
    return _positive_int("ADMIN_ACCESS_TOKEN_TTL_MINUTES", 10)


def refresh_token_ttl_days() -> int:
    return _positive_int("REFRESH_TOKEN_TTL_DAYS", 30)


def request_cooldown_seconds() -> int:
    return _positive_int("PASSWORD_RESET_REQUEST_COOLDOWN_SECONDS", 60)


def jwt_algorithm() -> str:
    return os.getenv("JWT_ALGORITHM", "HS256").strip()


def jwt_issuer() -> str:
    return os.getenv("JWT_ISSUER", "walking-meditation").strip()


def jwt_audience() -> str:
    return os.getenv("JWT_AUDIENCE", "walking-meditation-api").strip()


@lru_cache(maxsize=8)
def _read_secret(secret_arn: str) -> Any:
    if not secret_arn:
        raise ValueError("Secret ARN is not configured")
    import boto3
    from botocore.config import Config

    value = boto3.client(
        "secretsmanager",
        config=Config(
            connect_timeout=3,
            read_timeout=10,
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    ).get_secret_value(SecretId=secret_arn)
    secret = value.get("SecretString")
    if secret is None:
        raise ValueError("Binary secrets are not supported")
    try:
        return json.loads(secret)
    except json.JSONDecodeError:
        return secret


def jwt_keys() -> tuple[str, str, str | None, str | None]:
    inline = os.getenv("JWT_SECRET_KEY", "").strip()
    if inline:
        return (
            os.getenv("JWT_KEY_ID", "current").strip(),
            inline,
            os.getenv("JWT_PREVIOUS_KEY_ID", "").strip() or None,
            os.getenv("JWT_PREVIOUS_SECRET_KEY", "").strip() or None,
        )
    value = _read_secret(os.getenv("JWT_SECRET_ARN", ""))
    if not isinstance(value, dict) or not value.get("kid") or not value.get("current"):
        raise ValueError("JWT signing secret must contain kid and current")
    return value["kid"], value["current"], value.get("previous_kid"), value.get("previous")


def auth_rate_limit_pepper() -> str:
    inline = os.getenv("AUTH_RATE_LIMIT_PEPPER", "").strip()
    if inline:
        return inline
    value = _read_secret(os.getenv("AUTH_RATE_LIMIT_SECRET_ARN", ""))
    if not isinstance(value, str) or not value:
        raise ValueError("Authentication rate-limit secret must be a string")
    return value


def email_verification_link_base() -> str:
    value = os.getenv(
        "EMAIL_VERIFICATION_LINK_BASE",
        "http://localhost:8000/auth/email-verification/open",
    ).strip()
    parsed = urlsplit(value)
    local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if not (local or (parsed.scheme == "https" and parsed.netloc)):
        raise ValueError("EMAIL_VERIFICATION_LINK_BASE must be local HTTP or HTTPS")
    if not parsed.path.endswith("/auth/email-verification/open") or parsed.query or parsed.fragment:
        raise ValueError("EMAIL_VERIFICATION_LINK_BASE must end in /auth/email-verification/open")
    return value


def password_reset_link_base() -> str:
    return os.getenv("PASSWORD_RESET_LINK_BASE", "walkingmeditation://password-reset").strip()


def response_export_link_base() -> str:
    value = os.getenv(
        "RESPONSE_EXPORT_LINK_BASE",
        "http://localhost:8000/response-exports",
    ).strip()
    parsed = urlsplit(value)
    local = parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}
    if not (local or (parsed.scheme == "https" and parsed.netloc)):
        raise ValueError("RESPONSE_EXPORT_LINK_BASE must be local HTTP or HTTPS")
    if not parsed.path.endswith("/response-exports") or parsed.query or parsed.fragment:
        raise ValueError("RESPONSE_EXPORT_LINK_BASE must end in /response-exports")
    return value.rstrip("/")


def response_export_bucket() -> str:
    value = os.getenv("RESPONSE_EXPORT_BUCKET", "").strip()
    if not value:
        raise ValueError("RESPONSE_EXPORT_BUCKET is not configured")
    return value


def response_export_presigned_ttl_seconds() -> int:
    return _positive_int("RESPONSE_EXPORT_PRESIGNED_TTL_SECONDS", 300)


def android_package_name() -> str:
    return os.getenv("ANDROID_APP_PACKAGE_NAME", "com.example.walking_meditation").strip()


def android_fingerprints() -> list[str]:
    return [item.strip() for item in os.getenv("ANDROID_APP_SHA256_CERT_FINGERPRINTS", "").split(",") if item.strip()]


def prompt_root() -> Path:
    return Path(__file__).resolve().parent / "prompts"


def external_secret(arn_env: str, *preferred_keys: str) -> str:
    inline = os.getenv(arn_env.removesuffix("_SECRET_ARN") + "_API_KEY", "").strip()
    if inline:
        return inline
    value = _read_secret(os.getenv(arn_env, ""))
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in preferred_keys:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    raise ValueError(f"{arn_env} does not contain a usable API key")


def _non_negative_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name, "true" if default else "false").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be a boolean")


def google_maps_api_key() -> str:
    return external_secret("GOOGLE_MAPS_SECRET_ARN", "api_key", "key", "GOOGLE_MAPS_API_KEY")


def openai_api_key() -> str:
    return external_secret("OPENAI_SECRET_ARN", "api_key", "key", "OPENAI_API_KEY")


def openai_model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-6-luna").strip()


def openai_timeout_seconds() -> float:
    return _positive_float("OPENAI_TIMEOUT_SECONDS", 60.0)


def openai_max_retries() -> int:
    return _non_negative_int("OPENAI_MAX_RETRIES", 2)


def nearby_context_enabled() -> bool:
    return _boolean("ENABLE_NEARBY_CONTEXT", False)


def script_fallback_enabled() -> bool:
    return _boolean("SCRIPT_FALLBACK_ENABLED", True)


def generation_archive_bucket() -> str | None:
    """Return the optional bucket used to archive generation results."""
    value = os.getenv("GENERATION_ARCHIVE_BUCKET", "").strip()
    return value or None

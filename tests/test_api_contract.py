from __future__ import annotations

import os
import asyncio
from uuid import uuid4

import jwt
import pytest
from fastapi.routing import iter_route_contexts

from meditation_server.api.app import app
from meditation_server.api.auth import CURRENT_PRIVACY_POLICY_VERSION
from meditation_server.api.models import FeedbackRequest, RegisterRequest
from meditation_server.auth.security import create_access_token, decode_access_token
from meditation_server.api.models import LatLng, RouteStepInfo, ScriptWithoutParkRequest
from meditation_server.integrations.scripts import without_park


EXPECTED_MIGRATED_ROUTES = {
    ("GET", "/"),
    ("GET", "/health/database"),
    ("GET", "/.well-known/assetlinks.json"),
    ("GET", "/auth/password-reset/open"),
    ("GET", "/study/join"),
    ("POST", "/route/generate-path"),
    ("POST", "/tts"),
    ("POST", "/v1/generation-jobs"),
    ("GET", "/v1/generation-jobs/{job_id}"),
    ("GET", "/v1/generation-jobs/{job_id}/result"),
    ("POST", "/auth/register"),
    ("GET", "/auth/email-verification/open"),
    ("POST", "/auth/email-verification/confirm"),
    ("POST", "/auth/email-verification/resend"),
    ("POST", "/auth/password-reset/request"),
    ("POST", "/auth/password-reset/confirm"),
    ("POST", "/auth/login"),
    ("POST", "/auth/logout"),
    ("POST", "/auth/refresh"),
    ("POST", "/auth/logout-all"),
    ("POST", "/auth/admin/users/{user_id}/disable"),
    ("POST", "/auth/register-researcher"),
    ("GET", "/auth/admin/researchers/pending"),
    ("POST", "/auth/admin/researchers/{user_id}/verify"),
    ("POST", "/auth/admin/researchers/{user_id}/reject"),
    ("GET", "/auth/me"),
    ("PUT", "/auth/me"),
    ("DELETE", "/auth/me"),
    ("POST", "/studies"),
    ("POST", "/studies/{study_id}/join"),
    ("GET", "/studies"),
    ("GET", "/studies/mine"),
    ("GET", "/studies/current-script-parameters"),
    ("GET", "/studies/{study_id}"),
    ("POST", "/studies/{study_id}/getstatus"),
    ("POST", "/studies/{study_id}/swapgroup"),
    ("POST", "/studies/{study_id}/edit"),
    ("POST", "/studies/{study_id}/publish"),
    ("POST", "/studies/{study_id}/activate"),
    ("POST", "/studies/{study_id}/close"),
    ("GET", "/studies/{study_id}/consent-form"),
    ("POST", "/studies/{study_id}/consent-form/edit"),
    ("POST", "/studies/{study_id}/leave"),
    ("GET", "/studies/{study_id}/feedback"),
    ("POST", "/questionnaires"),
    ("GET", "/questionnaires"),
    ("GET", "/questionnaires/{questionnaire_id}"),
    ("PUT", "/questionnaires/{questionnaire_id}"),
    ("POST", "/studies/{study_id}/questionnaires"),
    ("GET", "/studies/{study_id}/questionnaires"),
    ("GET", "/studies/{study_id}/questionnaires/{study_questionnaire_id}"),
    ("POST", "/studies/{study_id}/questionnaires/{study_questionnaire_id}/revoke"),
    ("POST", "/studies/{study_id}/questionnaires/{study_questionnaire_id}/responses"),
    ("GET", "/studies/{study_id}/questionnaires/{study_questionnaire_id}/responses"),
    ("POST", "/response-exports"),
    ("POST", "/studies/{study_id}/response-export"),
    ("GET", "/response-exports/{export_id}"),
    ("GET", "/response-exports/{export_id}/download"),
    ("POST", "/response-exports/{export_id}/download"),
    ("POST", "/feedback"),
    ("GET", "/feedback"),
}


def test_migrated_fastapi_route_contract_is_complete() -> None:
    actual = {
        (method, route.path)
        for route in iter_route_contexts(app.routes)
        for method in (route.methods or set())
        if method not in {"HEAD", "OPTIONS"}
    }
    target_only = {
        ("GET", "/health"),
        ("GET", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("GET", "/openapi.json"),
        ("GET", "/redoc"),
    }
    assert actual - target_only == EXPECTED_MIGRATED_ROUTES


def test_legacy_created_status_codes_are_preserved() -> None:
    created = {
        (method, route.path)
        for route in iter_route_contexts(app.routes)
        for method in (route.methods or set())
        if getattr(route, "status_code", None) == 201
    }
    assert created == {
        ("POST", "/auth/register"),
        ("POST", "/auth/register-researcher"),
        ("POST", "/feedback"),
        ("POST", "/questionnaires"),
        ("POST", "/studies"),
        ("POST", "/studies/{study_id}/join"),
        ("POST", "/studies/{study_id}/questionnaires"),
        ("POST", "/studies/{study_id}/questionnaires/{study_questionnaire_id}/responses"),
    }


def test_static_study_routes_precede_uuid_route() -> None:
    paths = [route.path for route in iter_route_contexts(app.routes)]
    assert paths.index("/studies/mine") < paths.index("/studies/{study_id}")
    assert paths.index("/studies/current-script-parameters") < paths.index("/studies/{study_id}")


def test_registration_normalizes_legacy_fields() -> None:
    request = RegisterRequest(
        email="  PERSON@Example.COM ",
        password="correct horse battery staple",
        gender="  non-binary  ",
        researcher_institution="  University  ",
        agrees_to_privacy_policy=True,
        privacy_policy_version=CURRENT_PRIVACY_POLICY_VERSION,
    )
    assert str(request.email) == "person@example.com"
    assert request.gender == "non-binary"
    assert request.researcher_institution == "University"


def test_feedback_requires_rating_or_comment() -> None:
    with pytest.raises(ValueError, match="Feedback must include"):
        FeedbackRequest(category="general")


def test_access_token_validates_issuer_audience_kid_and_session_version(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("JWT_KEY_ID", "current-test")
    monkeypatch.setenv("JWT_ISSUER", "walking-meditation-test")
    monkeypatch.setenv("JWT_AUDIENCE", "walking-meditation-api-test")
    user_id = uuid4()
    token = create_access_token(user_id, 7)
    assert decode_access_token(token) == (user_id, 7)
    header = jwt.get_unverified_header(token)
    assert header["kid"] == "current-test"


def test_access_token_rejects_unknown_key(monkeypatch) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", "a" * 64)
    monkeypatch.setenv("JWT_KEY_ID", "current-test")
    token = jwt.encode({"sub": str(uuid4())}, "a" * 64, algorithm="HS256", headers={"kid": "unknown"})
    with pytest.raises(jwt.InvalidTokenError, match="Unknown signing key"):
        decode_access_token(token)


def test_script_fallback_keeps_legacy_response_shape() -> None:
    response = asyncio.run(without_park(ScriptWithoutParkRequest(
        source=LatLng(lat=-37.81, lon=144.96),
        destination=LatLng(lat=-37.80, lon=144.97),
        total_walking_time=600,
        context="general wellbeing",
        route_steps=[RouteStepInfo(
            index=1,
            instruction="Walk along Test Street",
            start=LatLng(lat=-37.81, lon=144.96),
            end=LatLng(lat=-37.80, lon=144.97),
            duration_s=600,
            distance_m=800,
            polyline="encoded",
        )],
    )))
    assert response["generation_cost_usd"] == 0.0
    assert response["usage"]["model"] == "fallback"
    assert response["script"]["fallback"] is True
    assert response["script"]["sections"][0]["route_segments"][0]["id"] == "source_to_destination_1"

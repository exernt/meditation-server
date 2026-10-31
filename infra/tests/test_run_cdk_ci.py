from __future__ import annotations

import pytest

from scripts.run_cdk_ci import build_cdk_command, build_context_arguments


REQUIRED_ENVIRONMENT = {
    "STAGE": "prod",
    "DATABASE_CLUSTER_ARN": (
        "arn:aws:rds:ap-southeast-2:111111111111:cluster:wm-prod-aurora-postgres"
    ),
    "WORKER_MAX_CONCURRENCY": "2",
    "MONTHLY_BUDGET_USD": "20",
    "BUDGET_ALERT_EMAIL": "alerts@example.invalid",
    "OPERATIONAL_ALERT_EMAIL": "operations@example.invalid",
    "GOOGLE_MAPS_SECRET_ARN": (
        "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:maps-AbC123"
    ),
    "OPENAI_SECRET_ARN": (
        "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:openai-XyZ789"
    ),
    "PASSWORD_RESET_FROM_EMAIL": "no-reply@example.invalid",
    "PASSWORD_RESET_LINK_BASE": (
        "https://api.example.invalid/prod/auth/password-reset/open"
    ),
    "EMAIL_VERIFICATION_LINK_BASE": (
        "https://api.example.invalid/prod/auth/email-verification/open"
    ),
    "RESPONSE_EXPORT_LINK_BASE": "https://api.example.invalid/prod/response-exports",
}


def test_missing_required_environment_variable_is_reported_by_name() -> None:
    environment = dict(REQUIRED_ENVIRONMENT)
    del environment["PASSWORD_RESET_LINK_BASE"]

    with pytest.raises(ValueError, match="PASSWORD_RESET_LINK_BASE"):
        build_context_arguments(environment)


def test_context_values_are_passed_as_distinct_process_arguments() -> None:
    environment = {
        **REQUIRED_ENVIRONMENT,
        "API_RESERVED_CONCURRENCY": "10",
        "TTS_PROVIDER": "polly",
        "WAF_MODE": "count",
    }

    command = build_cdk_command(("synth", "--strict"), environment)

    assert command[1:3] == ["cdk", "synth"]
    assert command[-1] == "--strict"
    assert (
        "passwordResetLinkBase="
        "https://api.example.invalid/prod/auth/password-reset/open"
    ) in command
    assert "apiReservedConcurrency=10" in command
    assert "wafMode=count" in command


def test_blank_optional_context_values_are_omitted() -> None:
    arguments = build_context_arguments(REQUIRED_ENVIRONMENT)

    assert not any("apiReservedConcurrency=" in argument for argument in arguments)
    assert not any("ttsProvider=" in argument for argument in arguments)
    assert not any("wafMode=" in argument for argument in arguments)

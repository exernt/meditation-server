import aws_cdk as cdk
import pytest

from infra.config import EnvironmentConfig


def _app(**values: object) -> cdk.App:
    return cdk.App(context={
        "stage": "dev",
        "githubRepository": "example/meditation-server",
        "databaseClusterArn": (
            "arn:aws:rds:ap-southeast-2:111111111111:cluster:wm-dev-aurora-postgres"
        ),
        "workerMaxConcurrency": 2,
        "monthlyBudgetUsd": 20,
        "budgetAlertEmail": "ops@example.com",
        "googleMapsSecretArn": "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:google",
        "openAiSecretArn": "arn:aws:secretsmanager:ap-southeast-2:111111111111:secret:openai",
        "passwordResetFromEmail": "no-reply@example.com",
        "emailVerificationLinkBase": (
            "https://api.example.com/auth/email-verification/open"
        ),
        **values,
    })


def test_restricts_stage() -> None:
    with pytest.raises(ValueError, match="dev or prod"):
        EnvironmentConfig.from_app(_app(stage="staging"))


def test_rejects_cluster_in_wrong_region() -> None:
    with pytest.raises(ValueError, match="configured Region"):
        EnvironmentConfig.from_app(
            _app(
                databaseClusterArn=(
                    "arn:aws:rds:ap-southeast-4:111111111111:cluster:wm-dev-aurora-postgres"
                )
            )
        )


def test_polly_is_default_optional_provider() -> None:
    config = EnvironmentConfig.from_app(_app())
    assert config.tts_provider == "polly"
    assert config.region == "ap-southeast-2"
    assert config.api_reserved_concurrency is None


def test_accepts_optional_api_reserved_concurrency() -> None:
    config = EnvironmentConfig.from_app(_app(apiReservedConcurrency=5))
    assert config.api_reserved_concurrency == 5


def test_rejects_zero_api_reserved_concurrency() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        EnvironmentConfig.from_app(_app(apiReservedConcurrency=0))


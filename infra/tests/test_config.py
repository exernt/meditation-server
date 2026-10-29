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
        "passwordResetLinkBase": (
            "https://api.example.com/auth/password-reset/open"
        ),
        "emailVerificationLinkBase": (
            "https://api.example.com/auth/email-verification/open"
        ),
        "responseExportLinkBase": "https://api.example.com/response-exports",
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


def test_accepts_api_gateway_stage_in_email_verification_link() -> None:
    link = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/auth/email-verification/open"
    )

    config = EnvironmentConfig.from_app(_app(emailVerificationLinkBase=link))

    assert config.email_verification_link_base == link


def test_accepts_api_gateway_stage_in_password_reset_link() -> None:
    link = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/auth/password-reset/open"
    )

    config = EnvironmentConfig.from_app(_app(passwordResetLinkBase=link))

    assert config.password_reset_link_base == link


def test_accepts_api_gateway_stage_in_response_export_link() -> None:
    link = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/response-exports"
    )

    config = EnvironmentConfig.from_app(_app(responseExportLinkBase=link))

    assert config.response_export_link_base == link


@pytest.mark.parametrize(
    "link",
    [
        "http://api.example.com/auth/password-reset/open",
        "https://api.example.com/auth/password-reset/open?token=placeholder",
        "https://api.example.com/auth/password-reset",
    ],
)
def test_rejects_invalid_password_reset_link(link: str) -> None:
    with pytest.raises(ValueError, match="passwordResetLinkBase"):
        EnvironmentConfig.from_app(_app(passwordResetLinkBase=link))


def test_rejects_zero_api_reserved_concurrency() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        EnvironmentConfig.from_app(_app(apiReservedConcurrency=0))


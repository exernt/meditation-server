import boto3

from meditation_server import settings


def test_accepts_api_gateway_stage_in_email_verification_link(monkeypatch) -> None:
    link = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/auth/email-verification/open"
    )
    monkeypatch.setenv("EMAIL_VERIFICATION_LINK_BASE", link)

    assert settings.email_verification_link_base() == link


def test_accepts_api_gateway_stage_in_response_export_link(monkeypatch) -> None:
    link = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/response-exports"
    )
    monkeypatch.setenv("RESPONSE_EXPORT_LINK_BASE", link)

    assert settings.response_export_link_base() == link


def test_secret_reads_use_bounded_aws_client_timeouts(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class SecretsClient:
        def get_secret_value(self, *, SecretId: str) -> dict[str, str]:
            captured["secret_id"] = SecretId
            return {"SecretString": '{"api_key":"test-key"}'}

    def create_client(service_name: str, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return SecretsClient()

    settings._read_secret.cache_clear()
    monkeypatch.setenv("GOOGLE_MAPS_SECRET_ARN", "arn:test:google-maps")
    monkeypatch.setattr(boto3, "client", create_client)

    assert settings.external_secret("GOOGLE_MAPS_SECRET_ARN", "api_key") == "test-key"
    config = captured["config"]
    assert captured["service_name"] == "secretsmanager"
    assert captured["secret_id"] == "arn:test:google-maps"
    assert config.connect_timeout == 3
    assert config.read_timeout == 10
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
    settings._read_secret.cache_clear()

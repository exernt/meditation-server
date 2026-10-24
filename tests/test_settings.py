from meditation_server import settings


def test_accepts_api_gateway_stage_in_email_verification_link(monkeypatch) -> None:
    link = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/auth/email-verification/open"
    )
    monkeypatch.setenv("EMAIL_VERIFICATION_LINK_BASE", link)

    assert settings.email_verification_link_base() == link

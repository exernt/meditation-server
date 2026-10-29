from __future__ import annotations

import asyncio

from meditation_server.integrations import email


def test_verification_email_states_link_and_account_expiry(monkeypatch) -> None:
    link_base = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/auth/email-verification/open"
    )
    sent_message: dict[str, str] = {}

    async def capture_message(to_address: str, subject: str, body: str) -> None:
        sent_message.update(
            to_address=to_address,
            subject=subject,
            body=body,
        )

    monkeypatch.setenv("EMAIL_VERIFICATION_LINK_BASE", link_base)
    monkeypatch.setattr(email, "_send", capture_message)

    asyncio.run(email.send_verification("walker@example.com", "token/+"))

    assert sent_message == {
        "to_address": "walker@example.com",
        "subject": "Verify your Walking Meditation email",
        "body": (
            f"Verify your email: {link_base}#token=token%2F%2B\n\n"
            "The verification link expires after 24 hours, and unverified "
            "accounts are removed after 7 days."
        ),
    }


def test_password_reset_email_uses_configured_link_base(monkeypatch) -> None:
    link_base = (
        "https://example.execute-api.ap-southeast-2.amazonaws.com/"
        "dev/auth/password-reset/open"
    )
    sent_message: dict[str, str] = {}

    async def capture_message(to_address: str, subject: str, body: str) -> None:
        sent_message.update(
            to_address=to_address,
            subject=subject,
            body=body,
        )

    monkeypatch.setenv("PASSWORD_RESET_LINK_BASE", link_base)
    monkeypatch.setattr(email, "_send", capture_message)

    asyncio.run(email.send_password_reset("walker@example.com", "token/+"))

    assert sent_message == {
        "to_address": "walker@example.com",
        "subject": "Reset your Walking Meditation password",
        "body": f"Reset your password: {link_base}#token=token%2F%2B",
    }

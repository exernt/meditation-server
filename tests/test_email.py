from __future__ import annotations

import asyncio

from meditation_server.integrations import email


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

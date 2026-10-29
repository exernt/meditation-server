from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

import boto3

from meditation_server import settings


async def _send(to_address: str, subject: str, body: str) -> None:
    if os.getenv("EMAIL_BACKEND", "file").casefold() == "file":
        directory = Path(os.getenv("EMAIL_CAPTURE_DIRECTORY", "var/dev-mail"))
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        safe_recipient = "".join(character if character.isalnum() else "_" for character in to_address)
        message = f"To: {to_address}\nSubject: {subject}\n\n{body}\n"
        await asyncio.to_thread(
            (directory / f"{timestamp}-{safe_recipient}.eml").write_text,
            message,
            encoding="utf-8",
        )
        return
    sender = os.environ["PASSWORD_RESET_FROM_EMAIL"]
    client = boto3.client("ses", region_name=os.getenv("AWS_REGION"))
    await asyncio.to_thread(
        client.send_email,
        Source=sender,
        Destination={"ToAddresses": [to_address]},
        Message={
            "Subject": {"Data": subject, "Charset": "UTF-8"},
            "Body": {"Text": {"Data": body, "Charset": "UTF-8"}},
        },
    )


async def send_verification(to_address: str, token: str) -> None:
    link = f"{settings.email_verification_link_base()}#token={quote(token, safe='')}"
    await _send(
        to_address,
        "Verify your Walking Meditation email",
        (
            f"Verify your email: {link}\n\n"
            "The verification link expires after 24 hours, and unverified "
            "accounts are removed after 7 days."
        ),
    )


async def send_password_reset(to_address: str, token: str) -> None:
    link = f"{settings.password_reset_link_base()}#token={quote(token, safe='')}"
    await _send(to_address, "Reset your Walking Meditation password", f"Reset your password: {link}")


async def send_response_export_ready(to_address: str, export_id: str) -> None:
    link = f"{settings.response_export_link_base()}/{quote(export_id, safe='')}/download"
    await _send(
        to_address,
        "Your Walking Meditation response export is ready",
        (
            "Your CSV response export is ready. Sign in to download it within "
            f"seven days: {link}"
        ),
    )

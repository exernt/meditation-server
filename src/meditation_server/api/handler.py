from __future__ import annotations

import json


def handler(event: dict, _context: object) -> dict:
    """Temporary deployment probe until the FastAPI migration lands.

    It intentionally exposes only a health response; deploying this scaffold
    cannot accidentally expose the legacy server without its auth boundary.
    """
    path = event.get("path") or event.get("rawPath") or "/"
    if path not in {"/", "/health"}:
        return _response(503, {"detail": "Application migration is not deployed"})
    return _response(200, {"status": "ready", "component": "api-scaffold"})


def _response(status_code: int, body: dict) -> dict:
    return {
        "statusCode": status_code,
        "headers": {
            "content-type": "application/json",
            "cache-control": "no-store",
            "x-content-type-options": "nosniff",
        },
        "body": json.dumps(body),
    }


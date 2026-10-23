from __future__ import annotations


def handler(event: dict, _context: object) -> dict:
    """Fail closed until generation processing is migrated into this repository."""
    failures = []
    for record in event.get("Records", []):
        message_id = record.get("messageId")
        if message_id:
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


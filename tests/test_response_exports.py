from __future__ import annotations

import asyncio
import csv
from datetime import UTC, datetime
import io
import re
from uuid import uuid4

from meditation_server.api.response_exports import response_export_login_page
from meditation_server.auth.security import generate_respondent_code
from meditation_server.integrations import response_export_storage
from meditation_server.repositories.executor import QueryResult
from meditation_server.workers import response_exports as response_export_worker
from meditation_server.workers.response_exports import build_response_export_csv


class ExportRowsDatabase:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _sql, _parameters=None, **_kwargs):
        return QueryResult(rows=tuple(self.rows))


def test_respondent_codes_are_random_and_not_user_uuids() -> None:
    first = generate_respondent_code()
    second = generate_respondent_code()

    assert first != second
    assert re.fullmatch(r"P-[0-9A-F]{32}", first)


def test_csv_combines_questionnaires_and_flattens_multiple_choice() -> None:
    now = datetime(2026, 10, 29, 2, 30, tzinfo=UTC)
    study_id = uuid4()
    response_id = uuid4()
    questionnaire_id = uuid4()
    issuance_id = uuid4()
    database = ExportRowsDatabase(
        [
            {
                "study_id": study_id,
                "study_name": "Meditation study",
                "respondent_code": "P-0123456789ABCDEF0123456789ABCDEF",
                "response_id": response_id,
                "questionnaire_id": questionnaire_id,
                "questionnaire_title": "Weekly check-in",
                "definition": {
                    "questions": [
                        {"id": "comment", "prompt": "Comment", "type": "text"},
                        {"id": "features", "prompt": "Helpful features", "type": "multiple_choice"},
                    ]
                },
                "questionnaire_round": 2,
                "questionnaire_issuance_id": issuance_id,
                "questionnaire_issued_at": now,
                "response_submitted_at": now,
                "answers": {
                    "comment": "=2+2",
                    "features": [
                        {"value": "guidance"},
                        {"value": "other", "detail": "+free text"},
                    ],
                },
            }
        ]
    )

    content = asyncio.run(
        build_response_export_csv(
            database,
            {
                "researcher_id": uuid4(),
                "scope": "study",
                "study_id": study_id,
                "source_last_submitted_on": now,
            },
        )
    )
    rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig"))))

    assert len(rows) == 3
    assert "respondent_id" not in rows[0]
    assert rows[0]["respondent_code"] == "P-0123456789ABCDEF0123456789ABCDEF"
    assert rows[0]["questionnaire_round"] == "2"
    assert rows[0]["questionnaire_id"] == str(questionnaire_id)
    assert rows[0]["answer_value"] == "'=2+2"
    assert rows[2]["answer_detail"] == "'+free text"
    assert [row["selection_index"] for row in rows[1:]] == ["0", "1"]


def test_csv_with_no_responses_still_contains_header() -> None:
    content = asyncio.run(
        build_response_export_csv(
            ExportRowsDatabase([]),
            {
                "researcher_id": uuid4(),
                "scope": "all_studies",
                "study_id": None,
                "source_last_submitted_on": None,
            },
        )
    )

    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert "respondent_code" in rows[0]
    assert "questionnaire_round" in rows[0]


def test_download_page_is_the_requested_minimal_login_form() -> None:
    response = asyncio.run(response_export_login_page(uuid4()))
    body = response.body.decode()

    assert 'type="email"' in body
    assert 'type="password"' in body
    assert ">Log In</button>" in body
    assert "Login failed." not in body
    assert response.headers["cache-control"] == "no-store"


def test_presigned_download_uses_short_ttl_and_attachment_headers(monkeypatch) -> None:
    captured = {}

    class Client:
        def generate_presigned_url(self, operation, **kwargs):
            captured.update({"operation": operation, **kwargs})
            return "https://download.example"

    monkeypatch.setenv("RESPONSE_EXPORT_BUCKET", "private-exports")
    monkeypatch.setenv("RESPONSE_EXPORT_PRESIGNED_TTL_SECONDS", "300")

    url = response_export_storage.response_export_download_url(
        key="response-exports/researcher/export/file.csv",
        filename="file.csv",
        client=Client(),
    )

    assert url == "https://download.example"
    assert captured["operation"] == "get_object"
    assert captured["ExpiresIn"] == 300
    assert captured["Params"]["Bucket"] == "private-exports"
    assert captured["Params"]["ResponseContentDisposition"] == 'attachment; filename="file.csv"'


def test_completed_unnotified_job_retry_sends_email_without_rebuilding(monkeypatch) -> None:
    export_id = uuid4()
    researcher_id = uuid4()
    calls = []

    class Database:
        async def execute(self, sql, _parameters=None, **_kwargs):
            assert "SELECT email FROM users" in sql
            return QueryResult(rows=({"email": "researcher@example.com"},))

    async def claim(_database, *, export_id, lease_token):
        return {
            "id": export_id,
            "researcher_id": researcher_id,
            "scope": "all_studies",
            "study_id": None,
            "status": "complete",
            "attempt_count": 2,
            "source_response_count": 1,
            "source_last_submitted_on": datetime.now(UTC),
        }

    async def notify(email, claimed_export_id):
        calls.append(("email", email, claimed_export_id))

    async def mark(_database, *, export_id, lease_token):
        calls.append(("mark", export_id, lease_token))
        return True

    async def must_not_build(*_args, **_kwargs):
        raise AssertionError("A completed export must not be rebuilt for notification recovery")

    monkeypatch.setattr(response_export_worker, "claim_response_export", claim)
    monkeypatch.setattr(response_export_worker, "send_response_export_ready", notify)
    monkeypatch.setattr(response_export_worker, "mark_response_export_notified", mark)
    monkeypatch.setattr(response_export_worker, "build_response_export_csv", must_not_build)

    succeeded = asyncio.run(
        response_export_worker._handle_record(
            {
                "messageId": "delivery-2",
                "body": f'{{"export_id":"{export_id}"}}',
                "attributes": {"ApproximateReceiveCount": "2"},
            },
            Database(),
        )
    )

    assert succeeded
    assert calls == [
        ("email", "researcher@example.com", str(export_id)),
        ("mark", export_id, "delivery-2"),
    ]

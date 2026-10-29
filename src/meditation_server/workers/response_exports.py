from __future__ import annotations

import asyncio
import csv
from datetime import UTC, datetime
import io
import json
import logging
from typing import Any
from uuid import UUID

from meditation_server.application.response_exports import (
    MAX_EXPORT_ATTEMPTS,
    claim_response_export,
    complete_response_export,
    mark_response_export_notified,
    release_completed_response_export,
    release_response_export,
)
from meditation_server.integrations.email import send_response_export_ready
from meditation_server.integrations.response_export_storage import store_response_export
from meditation_server.repositories.executor import DatabaseExecutor
from meditation_server.repositories.factory import create_data_api_executor_from_env


logger = logging.getLogger(__name__)
PAGE_SIZE = 100
CSV_COLUMNS = (
    "study_id",
    "study_name",
    "respondent_code",
    "response_id",
    "questionnaire_id",
    "questionnaire_title",
    "questionnaire_round",
    "questionnaire_issuance_id",
    "questionnaire_issued_at",
    "response_submitted_at",
    "question_id",
    "question_prompt",
    "question_type",
    "answer_value",
    "answer_detail",
    "selection_index",
)


def handler(event: dict, _context: object) -> dict:
    return asyncio.run(_handle_event(event, create_data_api_executor_from_env()))


async def _handle_event(event: dict, database: DatabaseExecutor) -> dict:
    failures = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId") or "")
        if not message_id:
            logger.error("Response export queue record is missing a message ID")
            continue
        if not await _handle_record(record, database):
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}


async def _handle_record(record: dict[str, Any], database: DatabaseExecutor) -> bool:
    message_id = str(record["messageId"])
    try:
        body = json.loads(record.get("body") or "")
        export_id = UUID(str(body["export_id"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        logger.error("Response export queue record has an invalid body")
        return False

    job = await claim_response_export(
        database,
        export_id=export_id,
        lease_token=message_id,
    )
    if job is None:
        return True

    ready_for_notification = job["status"] == "complete"
    try:
        if not ready_for_notification:
            content = await build_response_export_csv(database, job)
            generated_at = datetime.now(UTC)
            filename = _filename(job, generated_at)
            key = f"response-exports/{job['researcher_id']}/{export_id}/{filename}"
            await store_response_export(key=key, filename=filename, content=content)
            completed = await complete_response_export(
                database,
                export_id=export_id,
                lease_token=message_id,
                s3_key=key,
                filename=filename,
                generated_at=generated_at,
            )
            if not completed:
                logger.warning(
                    "Response export result was not committed",
                    extra={"export_id": str(export_id)},
                )
                return True
            ready_for_notification = True
        researcher = await database.execute(
            "SELECT email FROM users WHERE id=:researcher_id::uuid",
            {"researcher_id": job["researcher_id"]},
        )
        if not researcher.rows:
            raise ValueError("Response export researcher no longer exists")
        await send_response_export_ready(researcher.rows[0]["email"], str(export_id))
        await mark_response_export_notified(
            database,
            export_id=export_id,
            lease_token=message_id,
        )
        return True
    except Exception:
        logger.exception("Response export processing failed", extra={"export_id": str(export_id)})
        receive_count = int((record.get("attributes") or {}).get("ApproximateReceiveCount", "1"))
        terminal = max(receive_count, int(job["attempt_count"])) >= MAX_EXPORT_ATTEMPTS
        if ready_for_notification:
            await release_completed_response_export(
                database,
                export_id=export_id,
                lease_token=message_id,
                error_code="response_export_email_failed",
                terminal=terminal,
            )
        else:
            await release_response_export(
                database,
                export_id=export_id,
                lease_token=message_id,
                error_code="response_export_failed",
                terminal=terminal,
            )
        return False


async def build_response_export_csv(
    database: DatabaseExecutor,
    job: dict[str, Any],
) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_COLUMNS, lineterminator="\r\n")
    writer.writeheader()

    cutoff = job.get("source_last_submitted_on")
    if cutoff is None:
        return output.getvalue().encode("utf-8-sig")

    cursor_time: datetime | None = None
    cursor_id: UUID | None = None
    while True:
        rows = await _response_page(
            database,
            researcher_id=job["researcher_id"],
            study_id=job.get("study_id"),
            cutoff=cutoff,
            cursor_time=cursor_time,
            cursor_id=cursor_id,
        )
        if not rows:
            break
        for response in rows:
            for export_row in _csv_rows(response):
                writer.writerow(export_row)
        last = rows[-1]
        cursor_time = last["response_submitted_at"]
        cursor_id = last["response_id"]
        if len(rows) < PAGE_SIZE:
            break
    return output.getvalue().encode("utf-8-sig")


async def _response_page(
    database: DatabaseExecutor,
    *,
    researcher_id: UUID,
    study_id: UUID | None,
    cutoff: datetime,
    cursor_time: datetime | None,
    cursor_id: UUID | None,
) -> tuple[dict[str, Any], ...]:
    study_filter = "AND study.id=:study_id::uuid" if study_id is not None else ""
    cursor_filter = ""
    parameters: dict[str, Any] = {
        "researcher_id": researcher_id,
        "cutoff": cutoff,
        "page_size": PAGE_SIZE,
    }
    if study_id is not None:
        parameters["study_id"] = study_id
    if cursor_time is not None and cursor_id is not None:
        cursor_filter = """AND (
            response.submitted_on > :cursor_time
            OR (response.submitted_on = :cursor_time AND response.id > :cursor_id::uuid)
        )"""
        parameters["cursor_time"] = cursor_time
        parameters["cursor_id"] = cursor_id
    result = await database.execute(
        f"""SELECT study.id AS study_id,
                   study.name AS study_name,
                   response.respondent_code,
                   response.id AS response_id,
                   questionnaire.id AS questionnaire_id,
                   questionnaire.title AS questionnaire_title,
                   questionnaire.definition,
                   issuance.round_number AS questionnaire_round,
                   issuance.id AS questionnaire_issuance_id,
                   issuance.issued_at AS questionnaire_issued_at,
                   response.submitted_on AS response_submitted_at,
                   response.answers
              FROM questionnaire_responses AS response
              JOIN study_questionnaires AS issuance
                ON issuance.id=response.study_questionnaire_id
              JOIN study ON study.id=issuance.study_id
              JOIN study_created_by AS ownership ON ownership.study_id=study.id
              JOIN questionnaires AS questionnaire ON questionnaire.id=issuance.questionnaire_id
             WHERE ownership.researcher_id=:researcher_id::uuid
               AND response.submitted_on <= :cutoff
               {study_filter}
               {cursor_filter}
             ORDER BY response.submitted_on,response.id
             LIMIT :page_size""",
        parameters,
    )
    return tuple(dict(row) for row in result.rows)


def _csv_rows(response: dict[str, Any]) -> list[dict[str, Any]]:
    definition = response.get("definition") or {}
    answers = response.get("answers") or {}
    rows: list[dict[str, Any]] = []
    for question in definition.get("questions", []):
        question_id = question.get("id")
        if question_id not in answers:
            continue
        answer = answers[question_id]
        values = answer if question.get("type") == "multiple_choice" else [answer]
        if not values:
            values = [None]
        for index, value in enumerate(values):
            answer_value, answer_detail = _answer_parts(value)
            rows.append(
                {
                    "study_id": response["study_id"],
                    "study_name": _safe_csv_text(response["study_name"]),
                    "respondent_code": response.get("respondent_code") or "",
                    "response_id": response["response_id"],
                    "questionnaire_id": response["questionnaire_id"],
                    "questionnaire_title": _safe_csv_text(response["questionnaire_title"]),
                    "questionnaire_round": response["questionnaire_round"],
                    "questionnaire_issuance_id": response["questionnaire_issuance_id"],
                    "questionnaire_issued_at": _timestamp(response["questionnaire_issued_at"]),
                    "response_submitted_at": _timestamp(response["response_submitted_at"]),
                    "question_id": _safe_csv_text(question_id),
                    "question_prompt": _safe_csv_text(question.get("prompt", "")),
                    "question_type": question.get("type", ""),
                    "answer_value": _safe_csv_text(answer_value),
                    "answer_detail": _safe_csv_text(answer_detail),
                    "selection_index": index if question.get("type") == "multiple_choice" else "",
                }
            )
    return rows


def _answer_parts(value: Any) -> tuple[Any, Any]:
    if isinstance(value, dict):
        return value.get("value", ""), value.get("detail", "")
    if value is None:
        return "", ""
    return value, ""


def _safe_csv_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value or "")


def _filename(job: dict[str, Any], generated_at: datetime) -> str:
    timestamp = generated_at.strftime("%Y%m%dT%H%M%SZ")
    if job["scope"] == "study":
        return f"study-{job['study_id']}-responses-{timestamp}.csv"
    return f"all-studies-responses-{timestamp}.csv"

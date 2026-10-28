from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4

import pytest
from botocore.exceptions import ClientError

from meditation_server.application.generation_jobs import (
    GenerationJobConflict,
    GenerationJobLimitExceeded,
    cleanup_generation_data,
    complete_generation_job,
    create_generation_job,
    release_generation_job,
    request_hash,
)
from meditation_server.application.outbox import OutboxPublishResult, publish_due_outbox
from meditation_server.repositories.executor import DatabaseExecutor, QueryResult, Transaction
from meditation_server.workers import generation, maintenance


class ScriptedExecutor(DatabaseExecutor):
    def __init__(self, responder):
        self.responder = responder
        self.statements: list[str] = []
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, sql, parameters=None, *, transaction=None):
        normalized = " ".join(sql.split())
        self.statements.append(normalized)
        return self.responder(normalized, parameters or {})

    async def execute_batch(self, sql, parameter_sets, *, transaction=None):
        raise AssertionError("batch execution is not expected")

    async def begin_transaction(self):
        return Transaction(self, "test-transaction")

    async def commit(self, transaction):
        self._transaction_value(transaction)
        self.commits += 1

    async def rollback(self, transaction):
        self._transaction_value(transaction)
        self.rollbacks += 1


def _job_row(*, fingerprint: str, job_id: UUID | None = None) -> dict:
    now = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    return {
        "id": job_id or uuid4(),
        "user_id": uuid4(),
        "idempotency_key": "12345678-1234-1234-1234-123456789abc",
        "request_hash": fingerprint,
        "kind": "without_park",
        "status": "queued",
        "audio_status": "disabled",
        "attempt_count": 0,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "last_error_code": None,
    }


def test_create_job_and_outbox_commit_together() -> None:
    user_id = uuid4()
    payload = {"context": "test"}
    row = _job_row(fingerprint=request_hash("without_park", payload))

    def respond(sql, _parameters):
        if "COUNT(*) FILTER" in sql:
            return QueryResult(rows=({"in_flight": 0, "daily": 0},))
        if "INSERT INTO generation_jobs" in sql:
            return QueryResult(rows=(row,), records_updated=1)
        return QueryResult()

    database = ScriptedExecutor(respond)
    result = asyncio.run(create_generation_job(
        database,
        user_id=user_id,
        idempotency_key=row["idempotency_key"],
        kind="without_park",
        payload=payload,
        audio_status="disabled",
    ))

    assert result.created is True
    assert result.row["id"] == row["id"]
    assert database.commits == 1
    assert database.rollbacks == 0
    assert any("INSERT INTO generation_outbox" in sql for sql in database.statements)


def test_duplicate_idempotency_key_returns_existing_job_without_new_outbox() -> None:
    user_id = uuid4()
    payload = {"context": "test"}
    row = _job_row(fingerprint=request_hash("without_park", payload))

    def respond(sql, _parameters):
        if "FROM generation_jobs" in sql:
            return QueryResult(rows=(row,))
        return QueryResult()

    database = ScriptedExecutor(respond)
    result = asyncio.run(create_generation_job(
        database,
        user_id=user_id,
        idempotency_key=row["idempotency_key"],
        kind="without_park",
        payload=payload,
        audio_status="disabled",
    ))

    assert result.created is False
    assert not any("INSERT INTO generation_outbox" in sql for sql in database.statements)
    assert database.commits == 1


def test_reused_idempotency_key_with_different_request_is_rejected() -> None:
    row = _job_row(fingerprint="0" * 64)

    def respond(sql, _parameters):
        if "FROM generation_jobs" in sql:
            return QueryResult(rows=(row,))
        return QueryResult()

    database = ScriptedExecutor(respond)
    with pytest.raises(GenerationJobConflict):
        asyncio.run(create_generation_job(
            database,
            user_id=uuid4(),
            idempotency_key=row["idempotency_key"],
            kind="without_park",
            payload={"different": True},
            audio_status="disabled",
        ))
    assert database.rollbacks == 1


def test_in_flight_limit_is_enforced_inside_transaction() -> None:
    def respond(sql, _parameters):
        if "COUNT(*) FILTER" in sql:
            return QueryResult(rows=({"in_flight": 2, "daily": 2},))
        return QueryResult()

    database = ScriptedExecutor(respond)
    with pytest.raises(GenerationJobLimitExceeded, match="active"):
        asyncio.run(create_generation_job(
            database,
            user_id=uuid4(),
            idempotency_key="12345678-1234-1234-1234-123456789abc",
            kind="without_park",
            payload={"context": "test"},
            audio_status="disabled",
        ))
    assert database.rollbacks == 1


def test_daily_limit_is_enforced_inside_transaction() -> None:
    def respond(sql, _parameters):
        if "COUNT(*) FILTER" in sql:
            return QueryResult(rows=({"in_flight": 0, "daily": 20},))
        return QueryResult()

    database = ScriptedExecutor(respond)
    with pytest.raises(GenerationJobLimitExceeded, match="24 hours"):
        asyncio.run(create_generation_job(
            database,
            user_id=uuid4(),
            idempotency_key="12345678-1234-1234-1234-123456789abc",
            kind="without_park",
            payload={"context": "test"},
            audio_status="disabled",
        ))


def test_cleanup_uses_bounded_batches_for_both_tables() -> None:
    outbox_counts = iter((500, 500, 17))
    job_counts = iter((4,))

    def respond(sql, _parameters):
        if "DELETE FROM generation_outbox" in sql:
            return QueryResult(records_updated=next(outbox_counts))
        if "DELETE FROM generation_jobs" in sql:
            return QueryResult(records_updated=next(job_counts))
        raise AssertionError(f"Unexpected SQL: {sql}")

    result = asyncio.run(cleanup_generation_data(ScriptedExecutor(respond)))
    assert result == {"outbox_deleted": 1017, "jobs_deleted": 4}


def test_terminal_transitions_clear_request_payload() -> None:
    database = ScriptedExecutor(
        lambda _sql, _parameters: QueryResult(records_updated=1)
    )
    job_id = uuid4()
    assert asyncio.run(complete_generation_job(
        database,
        job_id=job_id,
        lease_token="delivery-1",
        result={"script": {"sections": []}},
    ))
    assert asyncio.run(release_generation_job(
        database,
        job_id=job_id,
        lease_token="delivery-2",
        error_code="generation_failed",
        terminal=True,
    ))
    terminal_sql = " ".join(database.statements)
    assert "request_payload = '{}'::jsonb" in terminal_sql
    assert "request_payload = CASE WHEN :terminal" in terminal_sql


class FakeSqs:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.messages: list[dict] = []

    def send_message(self, **request):
        self.messages.append(request)
        if self.error:
            raise self.error
        return {"MessageId": "sqs-message"}


def test_outbox_publish_marks_record_after_sqs_accepts_message() -> None:
    outbox_id = uuid4()
    job_id = uuid4()
    selected = False

    def respond(sql, _parameters):
        nonlocal selected
        if "FROM generation_outbox" in sql:
            if selected:
                return QueryResult()
            selected = True
            return QueryResult(rows=({"id": outbox_id, "job_id": job_id, "attempt_count": 0},))
        return QueryResult(records_updated=1)

    database = ScriptedExecutor(respond)
    sqs = FakeSqs()
    result = asyncio.run(publish_due_outbox(
        database,
        queue_url="https://sqs.ap-southeast-2.amazonaws.com/123/queue",
        max_records=1,
        client=sqs,
    ))

    assert result.published == 1
    assert result.failed == 0
    assert sqs.messages[0]["MessageBody"] == f'{{"job_id":"{job_id}"}}'
    assert any("SET published_at = now()" in sql for sql in database.statements)


def test_outbox_failure_records_backoff_without_losing_row() -> None:
    outbox_id = uuid4()
    job_id = uuid4()

    def respond(sql, _parameters):
        if "FROM generation_outbox" in sql:
            return QueryResult(rows=({"id": outbox_id, "job_id": job_id, "attempt_count": 0},))
        return QueryResult(records_updated=1)

    error = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "retry"}},
        "SendMessage",
    )
    database = ScriptedExecutor(respond)
    result = asyncio.run(publish_due_outbox(
        database,
        queue_url="https://sqs.ap-southeast-2.amazonaws.com/123/queue",
        max_records=1,
        client=FakeSqs(error),
    ))

    assert result.failed == 1
    assert any("next_attempt_at" in sql for sql in database.statements)
    assert database.commits == 1


def test_worker_acknowledges_already_claimed_or_terminal_job(monkeypatch) -> None:
    monkeypatch.setattr(generation, "claim_generation_job", AsyncMock(return_value=None))
    result = asyncio.run(generation._handle_event({"Records": [{
        "messageId": "message-1",
        "body": f'{{"job_id":"{uuid4()}"}}',
        "attributes": {"ApproximateReceiveCount": "1"},
    }]}, object()))
    assert result == {"batchItemFailures": []}


def test_worker_archives_and_logs_completed_generation(monkeypatch, caplog) -> None:
    job_id = uuid4()
    result_payload = {
        "script": {"fallback": False, "model": "gpt-6-luna"},
        "usage": {
            "model": "gpt-6-luna",
            "input_tokens": 100,
            "output_tokens": 200,
            "total_tokens": 300,
        },
        "generation_cost_usd": 0.001,
    }
    monkeypatch.setattr(generation, "claim_generation_job", AsyncMock(return_value={
        "id": job_id,
        "kind": "without_park",
        "request_payload": {},
        "attempt_count": 1,
    }))
    async def generate(_job, *, raw_output_observer):
        raw_output_observer(
            SimpleNamespace(
                text='{"raw":"narration"}',
                usage=SimpleNamespace(model="gpt-6-luna"),
            )
        )
        return result_payload

    monkeypatch.setattr(generation, "_generate", AsyncMock(side_effect=generate))
    monkeypatch.setattr(
        generation, "complete_generation_job", AsyncMock(return_value=True)
    )
    archive = Mock(return_value=f"generation-results/2026/10/28/{job_id}.json")
    monkeypatch.setattr(generation, "archive_generation_result", archive)
    raw_archive = Mock(
        return_value=(
            f"generation-results/2026/10/28/{job_id}.attempt-1.raw.json"
        )
    )
    monkeypatch.setattr(generation, "archive_raw_generation", raw_archive)

    with caplog.at_level("INFO"):
        handled = asyncio.run(generation._handle_record({
            "messageId": "message-1",
            "body": f'{{"job_id":"{job_id}"}}',
            "attributes": {"ApproximateReceiveCount": "1"},
        }, object()))

    assert handled is True
    assert archive.call_args.kwargs == {
        "job_id": job_id,
        "kind": "without_park",
        "result": result_payload,
    }
    assert raw_archive.call_args.kwargs == {
        "job_id": job_id,
        "kind": "without_park",
        "attempt_count": 1,
        "raw_output": '{"raw":"narration"}',
        "model": "gpt-6-luna",
    }
    outcome = next(record.message for record in caplog.records if "generation_outcome" in record.message)
    assert f'"job_id":"{job_id}"' in outcome
    assert '"fallback":false' in outcome
    assert '"archived":true' in outcome
    assert '"raw_archived":true' in outcome


def test_worker_archives_raw_generation_before_retrying_validation_failure(
    monkeypatch,
) -> None:
    job_id = uuid4()
    monkeypatch.setattr(generation, "claim_generation_job", AsyncMock(return_value={
        "id": job_id,
        "kind": "park",
        "request_payload": {},
        "attempt_count": 2,
    }))

    async def fail_validation(_job, *, raw_output_observer):
        raw_output_observer(
            SimpleNamespace(
                text='{"raw":"invalid narration"}',
                usage=SimpleNamespace(model="gpt-6-luna"),
            )
        )
        raise ValueError("Generated narration contains an empty text block")

    monkeypatch.setattr(
        generation, "_generate", AsyncMock(side_effect=fail_validation)
    )
    raw_archive = Mock(return_value="generation-results/raw.json")
    monkeypatch.setattr(generation, "archive_raw_generation", raw_archive)
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(generation, "release_generation_job", release)

    handled = asyncio.run(generation._handle_record({
        "messageId": "message-2",
        "body": f'{{"job_id":"{job_id}"}}',
        "attributes": {"ApproximateReceiveCount": "2"},
    }, object()))

    assert handled is False
    assert raw_archive.call_args.kwargs == {
        "job_id": job_id,
        "kind": "park",
        "attempt_count": 2,
        "raw_output": '{"raw":"invalid narration"}',
        "model": "gpt-6-luna",
    }
    assert release.await_args.kwargs["terminal"] is False


def test_worker_releases_transient_failure_for_retry(monkeypatch) -> None:
    job_id = uuid4()
    monkeypatch.setattr(generation, "claim_generation_job", AsyncMock(return_value={
        "id": job_id,
        "kind": "without_park",
        "request_payload": {},
        "attempt_count": 1,
    }))
    monkeypatch.setattr(generation, "_generate", AsyncMock(side_effect=RuntimeError("provider")))
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(generation, "release_generation_job", release)

    result = asyncio.run(generation._handle_event({"Records": [{
        "messageId": "message-1",
        "body": f'{{"job_id":"{job_id}"}}',
        "attributes": {"ApproximateReceiveCount": "1"},
    }]}, object()))

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-1"}]}
    assert release.await_args.kwargs["terminal"] is False


def test_worker_marks_fifth_failure_terminal(monkeypatch) -> None:
    job_id = uuid4()
    monkeypatch.setattr(generation, "claim_generation_job", AsyncMock(return_value={
        "id": job_id,
        "kind": "without_park",
        "request_payload": {},
        "attempt_count": 5,
    }))
    monkeypatch.setattr(generation, "_generate", AsyncMock(side_effect=RuntimeError("provider")))
    release = AsyncMock(return_value=True)
    monkeypatch.setattr(generation, "release_generation_job", release)

    result = asyncio.run(generation._handle_event({"Records": [{
        "messageId": "message-5",
        "body": f'{{"job_id":"{job_id}"}}',
        "attributes": {"ApproximateReceiveCount": "5"},
    }]}, object()))

    assert result == {"batchItemFailures": [{"itemIdentifier": "message-5"}]}
    assert release.await_args.kwargs["terminal"] is True


def test_maintenance_surfaces_persistent_outbox_publish_failure(monkeypatch) -> None:
    monkeypatch.setenv("GENERATION_QUEUE_URL", "https://example.invalid/queue")
    monkeypatch.setattr(maintenance, "create_data_api_executor_from_env", lambda: object())
    monkeypatch.setattr(
        maintenance,
        "publish_due_outbox",
        AsyncMock(return_value=OutboxPublishResult(failed=1)),
    )
    with pytest.raises(RuntimeError, match="failed to publish"):
        asyncio.run(maintenance._handle_event({"task": "outbox-recovery"}))

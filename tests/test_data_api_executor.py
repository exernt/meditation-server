from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
import json
import threading
import unittest
from uuid import UUID

import boto3

from meditation_server.repositories import data_api
from meditation_server.repositories.data_api import RdsDataApiExecutor


class FakeDataApiClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def execute_statement(self, **request):
        self.calls.append(("execute", request))
        return {
            "columnMetadata": [{"name": "id"}, {"name": "active"}],
            "records": [[{"longValue": 7}, {"booleanValue": True}]],
            "numberOfRecordsUpdated": 0,
        }

    def batch_execute_statement(self, **request):
        self.calls.append(("batch", request))
        return {"updateResults": []}

    def begin_transaction(self, **request):
        self.calls.append(("begin", request))
        return {"transactionId": "tx-123"}

    def commit_transaction(self, *, resourceArn, secretArn, transactionId):
        request = {
            "resourceArn": resourceArn,
            "secretArn": secretArn,
            "transactionId": transactionId,
        }
        self.calls.append(("commit", request))
        return {"transactionStatus": "Transaction Committed"}

    def rollback_transaction(self, *, resourceArn, secretArn, transactionId):
        request = {
            "resourceArn": resourceArn,
            "secretArn": secretArn,
            "transactionId": transactionId,
        }
        self.calls.append(("rollback", request))
        return {"transactionStatus": "Transaction Rolled Back"}


class RdsDataApiExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = FakeDataApiClient()
        self.executor = RdsDataApiExecutor(
            resource_arn="cluster-arn",
            secret_arn="secret-arn",
            database="walking_meditation",
            client=self.client,
        )

    async def test_encodes_parameters_and_decodes_rows(self) -> None:
        user_id = UUID("00000000-0000-0000-0000-000000000007")
        occurred_at = datetime(2026, 10, 24, 12, 30, tzinfo=UTC)

        result = await self.executor.execute(
            "SELECT :id, :payload, :amount, :occurred_at, :missing",
            {
                "id": user_id,
                "payload": {"answer": True},
                "amount": Decimal("12.50"),
                "occurred_at": occurred_at,
                "missing": None,
            },
        )

        self.assertEqual(result.one(), {"id": 7, "active": True})
        request = self.client.calls[0][1]
        by_name = {item["name"]: item for item in request["parameters"]}
        self.assertEqual(by_name["id"]["typeHint"], "UUID")
        self.assertEqual(by_name["payload"]["typeHint"], "JSON")
        self.assertEqual(by_name["amount"]["typeHint"], "DECIMAL")
        self.assertEqual(by_name["occurred_at"]["typeHint"], "TIMESTAMP")
        self.assertEqual(
            by_name["occurred_at"]["value"]["stringValue"],
            "2026-10-24 12:30:00",
        )
        self.assertEqual(by_name["missing"]["value"], {"isNull": True})
        self.assertEqual(
            request["resultSetOptions"],
            {"decimalReturnType": "STRING", "longReturnType": "LONG"},
        )

    async def test_decodes_postgresql_types_consistently(self) -> None:
        user_id = UUID("00000000-0000-0000-0000-000000000007")

        class TypedClient(FakeDataApiClient):
            def execute_statement(self, **request):
                return {
                    "columnMetadata": [
                        {"name": "id", "typeName": "uuid"},
                        {"name": "amount", "typeName": "numeric"},
                        {"name": "payload", "typeName": "jsonb"},
                    ],
                    "records": [[
                        {"stringValue": str(user_id)},
                        {"stringValue": "12.50"},
                        {"stringValue": json.dumps({"ok": True})},
                    ]],
                }

        executor = RdsDataApiExecutor(
            resource_arn="cluster-arn",
            secret_arn="secret-arn",
            database="walking_meditation",
            client=TypedClient(),
        )
        self.assertEqual(
            (await executor.execute("SELECT values")).one(),
            {"id": user_id, "amount": Decimal("12.50"), "payload": {"ok": True}},
        )

    async def test_transaction_commits_and_propagates_transaction_id(self) -> None:
        async with self.executor.transaction() as transaction:
            await self.executor.execute("SELECT 1", transaction=transaction)

        self.assertEqual([name for name, _ in self.client.calls], ["begin", "execute", "commit"])
        self.assertEqual(self.client.calls[1][1]["transactionId"], "tx-123")
        self.assertEqual(
            self.client.calls[2][1],
            {
                "resourceArn": "cluster-arn",
                "secretArn": "secret-arn",
                "transactionId": "tx-123",
            },
        )

    async def test_transaction_rolls_back_on_failure(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "stop"):
            async with self.executor.transaction():
                raise RuntimeError("stop")

        self.assertEqual([name for name, _ in self.client.calls], ["begin", "rollback"])
        self.assertNotIn("database", self.client.calls[1][1])

    async def test_batch_skips_empty_parameter_sets(self) -> None:
        await self.executor.execute_batch("INSERT INTO example VALUES (:id)", [])
        self.assertEqual(self.client.calls, [])


class EventLoopResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_client_is_called_off_the_event_loop(self) -> None:
        class ThreadRecordingClient(FakeDataApiClient):
            def execute_statement(self, **request):
                self.thread_id = threading.get_ident()
                return super().execute_statement(**request)

        client = ThreadRecordingClient()
        executor = RdsDataApiExecutor(
            resource_arn="cluster-arn",
            secret_arn="secret-arn",
            database="database",
            client=client,
        )
        event_loop_thread = threading.get_ident()
        await executor.execute("SELECT 1")
        self.assertNotEqual(client.thread_id, event_loop_thread)


def test_data_api_client_timeout_is_below_api_lambda_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}
    client = object()

    def create_client(service_name: str, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return client

    monkeypatch.setattr(boto3, "client", create_client)

    assert data_api._create_client("ap-southeast-2") is client
    config = captured["config"]
    assert captured["service_name"] == "rds-data"
    assert captured["region_name"] == "ap-southeast-2"
    assert config.connect_timeout == 5
    assert config.read_timeout == 20

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time
from decimal import Decimal
import json
from typing import Any
from uuid import UUID

from .executor import DatabaseExecutor, Parameters, QueryResult, Transaction


class RdsDataApiExecutor(DatabaseExecutor):
    """Execute named-parameter PostgreSQL through the Aurora RDS Data API."""

    def __init__(
        self,
        *,
        resource_arn: str,
        secret_arn: str,
        database: str,
        client: Any | None = None,
        region_name: str | None = None,
    ) -> None:
        if not resource_arn or not secret_arn or not database:
            raise ValueError("resource_arn, secret_arn, and database are required")
        self._resource_arn = resource_arn
        self._secret_arn = secret_arn
        self._database = database
        self._client = client or _create_client(region_name)

    async def execute(
        self,
        sql: str,
        parameters: Parameters | None = None,
        *,
        transaction: Transaction | None = None,
    ) -> QueryResult:
        request: dict[str, Any] = {
            **self._database_request(),
            "sql": sql,
            "parameters": _encode_parameters(parameters or {}),
            "includeResultMetadata": True,
            "resultSetOptions": {
                "decimalReturnType": "STRING",
                "longReturnType": "LONG",
            },
        }
        if transaction is not None:
            request["transactionId"] = self._transaction_id(transaction)
        response = await asyncio.to_thread(self._client.execute_statement, **request)
        return _decode_result(response)

    async def execute_batch(
        self,
        sql: str,
        parameter_sets: Sequence[Parameters],
        *,
        transaction: Transaction | None = None,
    ) -> None:
        if not parameter_sets:
            return
        request: dict[str, Any] = {
            **self._database_request(),
            "sql": sql,
            "parameterSets": [_encode_parameters(values) for values in parameter_sets],
        }
        if transaction is not None:
            request["transactionId"] = self._transaction_id(transaction)
        await asyncio.to_thread(self._client.batch_execute_statement, **request)

    async def begin_transaction(self) -> Transaction:
        response = await asyncio.to_thread(
            self._client.begin_transaction,
            **self._database_request(),
        )
        return Transaction(self, response["transactionId"])

    async def commit(self, transaction: Transaction) -> None:
        await asyncio.to_thread(
            self._client.commit_transaction,
            **self._resource_request(),
            transactionId=self._transaction_id(transaction),
        )

    async def rollback(self, transaction: Transaction) -> None:
        await asyncio.to_thread(
            self._client.rollback_transaction,
            **self._resource_request(),
            transactionId=self._transaction_id(transaction),
        )

    def _resource_request(self) -> dict[str, str]:
        return {
            "resourceArn": self._resource_arn,
            "secretArn": self._secret_arn,
        }

    def _database_request(self) -> dict[str, str]:
        return {
            **self._resource_request(),
            "database": self._database,
        }

    def _transaction_id(self, transaction: Transaction) -> str:
        value = self._transaction_value(transaction)
        if not isinstance(value, str):
            raise TypeError("RDS Data API transaction ID must be a string")
        return value


def _create_client(region_name: str | None) -> Any:
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - exercised only in incomplete runtimes
        raise RuntimeError("boto3 is required to use the RDS Data API executor") from exc

    return boto3.client(
        "rds-data",
        region_name=region_name,
        config=Config(
            retries={"total_max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
            max_pool_connections=20,
            user_agent_appid="walking-meditation",
        ),
    )


def _encode_parameters(parameters: Parameters) -> list[dict[str, Any]]:
    return [{"name": name, **_encode_value(value)} for name, value in parameters.items()]


def _encode_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"value": {"isNull": True}}
    if isinstance(value, bool):
        return {"value": {"booleanValue": value}}
    if isinstance(value, int):
        return {"value": {"longValue": value}}
    if isinstance(value, float):
        return {"value": {"doubleValue": value}}
    if isinstance(value, bytes):
        return {"value": {"blobValue": value}}
    if isinstance(value, UUID):
        return {"value": {"stringValue": str(value)}, "typeHint": "UUID"}
    if isinstance(value, datetime):
        return {
            "value": {"stringValue": _format_timestamp(value)},
            "typeHint": "TIMESTAMP",
        }
    if isinstance(value, date):
        return {"value": {"stringValue": value.isoformat()}, "typeHint": "DATE"}
    if isinstance(value, time):
        if value.tzinfo is not None:
            raise ValueError("Timezone-aware time values are not supported by the Data API")
        return {"value": {"stringValue": _format_time(value)}, "typeHint": "TIME"}
    if isinstance(value, Decimal):
        return {"value": {"stringValue": str(value)}, "typeHint": "DECIMAL"}
    if isinstance(value, (dict, list)):
        return {
            "value": {"stringValue": json.dumps(value, separators=(",", ":"))},
            "typeHint": "JSON",
        }
    if isinstance(value, str):
        return {"value": {"stringValue": value}}
    raise TypeError(f"Unsupported SQL parameter type: {type(value).__name__}")


def _decode_result(response: Mapping[str, Any]) -> QueryResult:
    metadata = response.get("columnMetadata", [])
    columns = [column["name"] for column in metadata]
    rows = tuple(
        dict(
            zip(
                columns,
                (
                    _decode_field(field, column.get("typeName"))
                    for field, column in zip(record, metadata, strict=True)
                ),
                strict=True,
            )
        )
        for record in response.get("records", [])
    )
    return QueryResult(
        rows=rows,
        records_updated=int(response.get("numberOfRecordsUpdated", 0)),
    )


def _decode_field(field: Mapping[str, Any], type_name: str | None = None) -> Any:
    if field.get("isNull"):
        return None
    for key in ("stringValue", "longValue", "doubleValue", "booleanValue", "blobValue"):
        if key in field:
            return _coerce_result_value(field[key], type_name)
    if "arrayValue" in field:
        array = field["arrayValue"]
        for key in (
            "stringValues",
            "longValues",
            "doubleValues",
            "booleanValues",
            "arrayValues",
        ):
            if key in array:
                values = array[key]
                return [
                    _decode_field({"arrayValue": value}) if key == "arrayValues" else value
                    for value in values
                ]
    raise ValueError(f"Unsupported RDS Data API field shape: {sorted(field)}")


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    base = value.strftime("%Y-%m-%d %H:%M:%S")
    return base if value.microsecond == 0 else f"{base}.{value.microsecond // 1000:03d}"


def _format_time(value: time) -> str:
    base = value.strftime("%H:%M:%S")
    return base if value.microsecond == 0 else f"{base}.{value.microsecond // 1000:03d}"


def _coerce_result_value(value: Any, type_name: str | None) -> Any:
    normalized = (type_name or "").casefold()
    if normalized == "uuid" and isinstance(value, str):
        return UUID(value)
    if normalized in {"decimal", "numeric"} and isinstance(value, str):
        return Decimal(value)
    if normalized in {"json", "jsonb"} and isinstance(value, str):
        return json.loads(value)
    if normalized == "date" and isinstance(value, str):
        return date.fromisoformat(value)
    if normalized in {"time", "timetz"} and isinstance(value, str):
        return time.fromisoformat(value)
    if normalized in {"timestamp", "timestamptz"} and isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if normalized == "timestamptz" and parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    return value

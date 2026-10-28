from __future__ import annotations

import json
from uuid import uuid4

from meditation_server.integrations import script_archive


class FakeS3:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def put_object(self, **request) -> None:
        self.requests.append(request)


def test_archive_generation_result_writes_encrypted_json(monkeypatch) -> None:
    client = FakeS3()
    monkeypatch.setenv("GENERATION_ARCHIVE_BUCKET", "dev-script-archive")
    monkeypatch.setattr(script_archive.boto3, "client", lambda service: client)
    job_id = uuid4()
    result = {
        "script": {"fallback": True, "model": "fallback", "sections": []},
        "usage": {"model": "fallback", "total_tokens": 0},
        "generation_cost_usd": 0.0,
    }

    key = script_archive.archive_generation_result(
        job_id=job_id,
        kind="without_park",
        result=result,
    )

    assert key is not None
    assert key.startswith("generation-results/")
    assert key.endswith(f"/{job_id}.json")
    request = client.requests[0]
    assert request["Bucket"] == "dev-script-archive"
    assert request["Key"] == key
    assert request["ContentType"] == "application/json"
    assert request["ServerSideEncryption"] == "AES256"
    assert request["Metadata"] == {
        "job-id": str(job_id),
        "kind": "without_park",
        "fallback": "true",
        "model": "fallback",
    }
    assert json.loads(request["Body"])["script"]["fallback"] is True


def test_archive_generation_result_is_disabled_without_bucket(monkeypatch) -> None:
    monkeypatch.delenv("GENERATION_ARCHIVE_BUCKET", raising=False)

    key = script_archive.archive_generation_result(
        job_id=uuid4(),
        kind="park",
        result={},
    )

    assert key is None


def test_archive_raw_generation_preserves_exact_model_output(monkeypatch) -> None:
    client = FakeS3()
    monkeypatch.setenv("GENERATION_ARCHIVE_BUCKET", "dev-script-archive")
    monkeypatch.setattr(script_archive.boto3, "client", lambda service: client)
    job_id = uuid4()
    raw_output = '{"focused_attention":{"opening_narration":"Exact spacing"}}'

    key = script_archive.archive_raw_generation(
        job_id=job_id,
        kind="park",
        attempt_count=2,
        raw_output=raw_output,
        model="gpt-6-luna",
    )

    assert key is not None
    assert key.endswith(f"/{job_id}.attempt-2.raw.json")
    request = client.requests[0]
    assert request["Bucket"] == "dev-script-archive"
    assert request["Key"] == key
    assert request["Body"].decode("utf-8") == raw_output
    assert request["ContentType"] == "application/json"
    assert request["ServerSideEncryption"] == "AES256"
    assert request["Metadata"] == {
        "job-id": str(job_id),
        "kind": "park",
        "attempt-count": "2",
        "artifact": "raw-model-output",
        "model": "gpt-6-luna",
    }

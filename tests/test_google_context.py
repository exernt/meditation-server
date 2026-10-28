from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from meditation_server.api.models import LatLng, RouteStepInfo
from meditation_server.integrations import google_context
from meditation_server.integrations.google_context import EnrichedSegment


class _FakeAsyncClient:
    calls: list[tuple[str, str, dict]] = []
    get_payload: dict = {}
    post_payload: dict = {}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: self.get_payload)

    async def post(self, url: str, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: self.post_payload)


def _step(index: int, start_lon: float, end_lon: float, distance_m: int = 100):
    return RouteStepInfo(
        index=index,
        instruction=f"Step {index}",
        start=LatLng(lat=-37.81, lon=start_lon),
        end=LatLng(lat=-37.81, lon=end_lon),
        duration_s=60,
        distance_m=distance_m,
        polyline=f"encoded-{index}",
    )


def _single_coordinate_step() -> RouteStepInfo:
    point = LatLng(lat=38.5, lon=-120.2)
    return RouteStepInfo(
        index=1,
        instruction="Continue",
        start=point,
        end=point,
        duration_s=0,
        distance_m=0,
        polyline="_p~iF~ps|U",  # Encodes the single point (38.5, -120.2).
    )


def _segment(index: int, start_lon: float, end_lon: float, distance_m: int):
    step = _step(index, start_lon, end_lon, distance_m)
    return EnrichedSegment(
        index=step.index,
        instruction=step.instruction,
        start=step.start,
        end=step.end,
        polyline=step.polyline,
        duration_s=step.duration_s,
        distance_m=step.distance_m,
        elevation_classification="low elevation (mostly flat)",
        elevation_gain_m=0,
        elevation_start_m=0,
        elevation_end_m=0,
    )


def test_enrich_route_batches_all_step_boundaries_into_one_leg_request(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_payload = {
        "status": "OK",
        "results": [
            {"elevation": 10.0},
            {"elevation": 20.0},
            {"elevation": 15.0},
        ],
    }
    monkeypatch.setattr(google_context.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(google_context.settings, "google_maps_api_key", lambda: "key")

    enriched = asyncio.run(
        google_context.enrich_route(
            [_step(1, 144.96, 144.97), _step(2, 144.97, 144.98)]
        )
    )

    assert len(_FakeAsyncClient.calls) == 1
    method, url, request = _FakeAsyncClient.calls[0]
    assert method == "GET"
    assert url == google_context.ELEVATION_URL
    assert request["params"]["locations"].count("|") == 2
    assert enriched[0].elevation_gain_m == 10.0
    assert enriched[0].elevation_classification == "high elevation (uphill)"
    assert enriched[1].elevation_gain_m == -5.0
    assert enriched[1].elevation_classification == "low elevation (mostly flat)"


def test_enrich_route_supports_a_single_coordinate_step(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_payload = {
        "status": "OK",
        "results": [{"elevation": 12.5}],
    }
    monkeypatch.setattr(google_context.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(google_context.settings, "google_maps_api_key", lambda: "key")

    enriched = asyncio.run(google_context.enrich_route([_single_coordinate_step()]))

    assert len(_FakeAsyncClient.calls) == 1
    _, _, request = _FakeAsyncClient.calls[0]
    assert request["params"]["locations"] == "38.500000,-120.200000"
    assert len(enriched) == 1
    assert enriched[0].elevation_start_m == 12.5
    assert enriched[0].elevation_end_m == 12.5
    assert enriched[0].elevation_gain_m == 0.0
    assert enriched[0].elevation_classification == "low elevation (mostly flat)"


def test_enrich_route_keeps_single_coordinate_step_when_elevation_is_missing(
    monkeypatch,
):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.get_payload = {"status": "ZERO_RESULTS", "results": []}
    monkeypatch.setattr(google_context.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(google_context.settings, "google_maps_api_key", lambda: "key")

    enriched = asyncio.run(google_context.enrich_route([_single_coordinate_step()]))

    assert len(enriched) == 1
    assert enriched[0].elevation_classification == "unknown"
    assert enriched[0].elevation_start_m == 0.0
    assert enriched[0].elevation_end_m == 0.0
    assert enriched[0].elevation_gain_m == 0.0


def test_nearby_features_searches_once_at_leg_distance_midpoint(monkeypatch):
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.post_payload = {
        "places": [
            {"displayName": {"text": "Royal Park"}},
            {"displayName": {"text": "Royal Park"}},
            {"displayName": {"text": "State Library"}},
        ]
    }
    monkeypatch.setattr(google_context.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(google_context.settings, "google_maps_api_key", lambda: "key")
    monkeypatch.setattr(google_context.settings, "nearby_context_enabled", lambda: True)
    segments = [
        _segment(1, 144.96, 144.97, 100),
        _segment(2, 144.97, 144.99, 300),
        _segment(3, 144.99, 145.00, 100),
    ]

    features = asyncio.run(
        google_context.lookup_nearby_features(segments, "source_to_park")
    )

    assert len(_FakeAsyncClient.calls) == 1
    method, url, request = _FakeAsyncClient.calls[0]
    assert method == "POST"
    assert url == google_context.NEARBY_URL
    center = request["json"]["locationRestriction"]["circle"]["center"]
    assert center["latitude"] == pytest.approx(-37.81)
    assert center["longitude"] == pytest.approx(144.98)
    assert features == {
        "source_to_park_2": ["Royal Park", "State Library"]
    }

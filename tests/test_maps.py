from __future__ import annotations

import asyncio
from types import SimpleNamespace

from meditation_server.api.models import LatLng, WalkRouteRequest
from meditation_server.integrations import maps


def _place(place_id: str, latitude: float, place_type: str) -> dict:
    return {
        "id": place_id,
        "displayName": {"text": place_id},
        "location": {"latitude": latitude, "longitude": 144.97},
        "primaryType": place_type,
        "types": [place_type],
    }


class _PlacesClient:
    def __init__(self) -> None:
        self.requested_types: list[str] = []

    async def post(self, _url: str, *, json: dict, **_kwargs):
        place_type = json["includedType"]
        self.requested_types.append(place_type)
        places = [
            _place(f"{place_type}-{index}", float(index), place_type)
            for index in range(1, 5)
        ]
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"places": places},
        )


def test_parks_retains_three_candidates_from_each_place_type(monkeypatch):
    client = _PlacesClient()
    monkeypatch.setattr(maps, "_key", lambda: "test-key")

    candidates = asyncio.run(maps._parks(client, "encoded-polyline"))

    assert set(client.requested_types) == {"city_park", "garden"}
    assert [candidate["place_id"] for candidate in candidates] == [
        "city_park-1",
        "city_park-2",
        "city_park-3",
        "garden-1",
        "garden-2",
        "garden-3",
    ]


class _RouteClient:
    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def test_generate_walk_route_evaluates_gardens_after_infeasible_city_parks(
    monkeypatch,
):
    payload = WalkRouteRequest(
        source=LatLng(lat=0, lon=0),
        destination=LatLng(lat=10, lon=10),
        total_travel_time=300,
    )
    candidates = [
        {
            "place_id": f"city-{index}",
            "name": f"City {index}",
            "lat": float(index),
            "lon": 1.0,
            "primary_type": "city_park",
            "types": ["city_park"],
        }
        for index in range(1, 4)
    ] + [
        {
            "place_id": f"garden-{index}",
            "name": f"Garden {index}",
            "lat": float(index + 3),
            "lon": 1.0,
            "primary_type": "garden",
            "types": ["garden"],
        }
        for index in range(1, 4)
    ]

    async def fake_parks(_client, _polyline):
        return candidates

    async def fake_route(_client, origin: LatLng, destination: LatLng):
        if origin == payload.source and destination == payload.destination:
            duration = 100
        else:
            park = destination if origin == payload.source else origin
            duration = 200 if park.lat <= 3 else 100
        return {
            "duration_s": duration,
            "distance_m": duration,
            "polyline": "encoded",
            "steps": [],
        }

    monkeypatch.setattr(maps.httpx, "AsyncClient", _RouteClient)
    monkeypatch.setattr(maps, "_parks", fake_parks)
    monkeypatch.setattr(maps, "_route", fake_route)

    response = asyncio.run(maps.generate_walk_route(payload))

    assert [item["park"]["place_id"] for item in response["parks"]] == [
        "garden-1",
        "garden-2",
        "garden-3",
    ]


def test_map_timeout_is_below_api_lambda_timeout():
    assert maps.MAPS_TIMEOUT_SECONDS == 20

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from meditation_server import settings
from meditation_server.api.models import LatLng, RouteStepInfo, WalkRouteRequest


ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"


def _key() -> str:
    return settings.external_secret("GOOGLE_MAPS_SECRET_ARN", "api_key", "key", "GOOGLE_MAPS_API_KEY")


def _waypoint(point: LatLng) -> dict[str, Any]:
    return {"location": {"latLng": {"latitude": point.lat, "longitude": point.lon}}}


def _seconds(value: str | None) -> int:
    if not value:
        return 0
    return max(0, int(round(float(value.removesuffix("s")))))


async def _route(client: httpx.AsyncClient, origin: LatLng, destination: LatLng) -> dict[str, Any]:
    response = await client.post(
        ROUTES_URL,
        headers={
            "X-Goog-Api-Key": _key(),
            "Content-Type": "application/json",
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline,routes.legs.steps.startLocation,routes.legs.steps.endLocation,routes.legs.steps.distanceMeters,routes.legs.steps.staticDuration,routes.legs.steps.polyline.encodedPolyline,routes.legs.steps.navigationInstruction.instructions",
        },
        json={"origin": _waypoint(origin), "destination": _waypoint(destination), "travelMode": "WALK", "computeAlternativeRoutes": False},
    )
    response.raise_for_status()
    routes = response.json().get("routes") or []
    if not routes:
        raise RuntimeError("No routes returned from computeRoutes")
    route = routes[0]
    polyline = (route.get("polyline") or {}).get("encodedPolyline") or ""
    if not polyline:
        raise RuntimeError("Expected route polyline")
    steps = []
    for leg in route.get("legs") or []:
        for raw in leg.get("steps") or []:
            start = (raw.get("startLocation") or {}).get("latLng") or {}
            end = (raw.get("endLocation") or {}).get("latLng") or {}
            step_polyline = (raw.get("polyline") or {}).get("encodedPolyline") or ""
            if not step_polyline or not {"latitude", "longitude"} <= start.keys() or not {"latitude", "longitude"} <= end.keys():
                continue
            steps.append(RouteStepInfo(
                index=len(steps) + 1,
                instruction=((raw.get("navigationInstruction") or {}).get("instructions") or "Continue").strip(),
                start=LatLng(lat=start["latitude"], lon=start["longitude"]),
                end=LatLng(lat=end["latitude"], lon=end["longitude"]),
                duration_s=_seconds(raw.get("staticDuration")),
                distance_m=max(0, int(raw.get("distanceMeters") or 0)),
                polyline=step_polyline,
            ).model_dump())
    return {"duration_s": _seconds(route.get("duration")), "distance_m": max(0, int(route.get("distanceMeters") or 0)), "polyline": polyline, "steps": steps}


async def _parks(client: httpx.AsyncClient, polyline: str) -> list[dict[str, Any]]:
    async def search(place_type: str, text: str):
        response = await client.post(
            PLACES_URL,
            headers={"Content-Type": "application/json", "X-Goog-Api-Key": _key(), "X-Goog-FieldMask": "places.id,places.displayName,places.location,places.primaryType,places.types"},
            json={"textQuery": text, "includedType": place_type, "strictTypeFiltering": True, "searchAlongRouteParameters": {"polyline": {"encodedPolyline": polyline}}},
        )
        response.raise_for_status()
        return response.json().get("places") or []
    raw = (await search("city_park", "city park")) + (await search("garden", "garden"))
    unique = {}
    for place in raw:
        location = place.get("location") or {}
        if place.get("id") and location.get("latitude") is not None and location.get("longitude") is not None:
            unique[place["id"]] = {
                "place_id": place["id"], "name": (place.get("displayName") or {}).get("text") or "(unnamed park)",
                "lat": float(location["latitude"]), "lon": float(location["longitude"]),
                "primary_type": place.get("primaryType"), "types": list(place.get("types") or []),
            }
    return list(unique.values())[:3]


async def generate_walk_route(payload: WalkRouteRequest) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=40) as client:
        baseline = await _route(client, payload.source, payload.destination)
        response = {"origin": payload.source.model_dump(), "destination": payload.destination.model_dump(), "total_travel_time": payload.total_travel_time, "baseline": baseline, "parks": []}
        if baseline["duration_s"] > payload.total_travel_time:
            return response
        parks = await _parks(client, baseline["polyline"])
        semaphore = asyncio.Semaphore(6)
        async def evaluate(park):
            try:
                point = LatLng(lat=park["lat"], lon=park["lon"])
                async with semaphore:
                    to_park, from_park = await asyncio.gather(_route(client, payload.source, point), _route(client, point, payload.destination))
            except Exception:
                return None
            detour = to_park["duration_s"] + from_park["duration_s"]
            if detour > payload.total_travel_time:
                return None
            return {
                "park": park, "baseline_s": baseline["duration_s"], "detour_s": detour,
                "to_park_s": to_park["duration_s"], "park_to_destination_s": from_park["duration_s"],
                "added_s": detour - baseline["duration_s"], "slack_s": payload.total_travel_time - detour,
                "polyline_to_park": to_park["polyline"], "polyline_from_park": from_park["polyline"],
                "steps_to_park": to_park["steps"], "steps_from_park": from_park["steps"],
            }
        evaluated = await asyncio.gather(*(evaluate(park) for park in parks))
        response["parks"] = sorted((item for item in evaluated if item), key=lambda item: item["detour_s"])[:3]
        return response

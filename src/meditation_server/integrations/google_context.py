from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from meditation_server import settings
from meditation_server.api.models import LatLng, RouteStepInfo


logger = logging.getLogger(__name__)

ELEVATION_URL = "https://maps.googleapis.com/maps/api/elevation/json"
WEATHER_URL = "https://weather.googleapis.com/v1/currentConditions:lookup"
NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
NEARBY_FEATURE_TYPES = [
    "park",
    "garden",
    "playground",
    "library",
    "museum",
    "place_of_worship",
    "tourist_attraction",
    "university",
]
MAX_ELEVATION_LOCATIONS = 512


@dataclass(frozen=True)
class EnrichedSegment:
    index: int
    instruction: str
    start: LatLng
    end: LatLng
    polyline: str
    duration_s: int
    distance_m: int
    elevation_classification: str
    elevation_gain_m: float
    elevation_start_m: float
    elevation_end_m: float


def _classify_elevation(gain_m: float) -> str:
    if gain_m >= 8:
        return "high elevation (uphill)"
    if gain_m <= -8:
        return "high elevation (downhill)"
    return "low elevation (mostly flat)"


def _unknown_segment(step: RouteStepInfo) -> EnrichedSegment:
    return EnrichedSegment(
        index=step.index,
        instruction=step.instruction,
        start=step.start,
        end=step.end,
        polyline=step.polyline,
        duration_s=step.duration_s,
        distance_m=step.distance_m,
        elevation_classification="unknown",
        elevation_gain_m=0.0,
        elevation_start_m=0.0,
        elevation_end_m=0.0,
    )


def _location_key(point: LatLng) -> tuple[float, float]:
    return point.lat, point.lon


def _elevation_locations(steps: Sequence[RouteStepInfo]) -> list[tuple[float, float]]:
    unique = list(
        dict.fromkeys(
            _location_key(point)
            for step in steps
            for point in (step.start, step.end)
        )
    )
    if len(unique) <= MAX_ELEVATION_LOCATIONS:
        return unique

    # Google accepts at most 512 positional locations. Keep the single request
    # per leg and spread the retained locations across the entire leg; segments
    # whose two boundaries were not retained fall back to unknown terrain.
    last = len(unique) - 1
    selected_indexes = {
        round(index * last / (MAX_ELEVATION_LOCATIONS - 1))
        for index in range(MAX_ELEVATION_LOCATIONS)
    }
    return [point for index, point in enumerate(unique) if index in selected_indexes]


def _enriched_segment(
    step: RouteStepInfo, elevations: dict[tuple[float, float], float]
) -> EnrichedSegment:
    start_m = elevations.get(_location_key(step.start))
    end_m = elevations.get(_location_key(step.end))
    if start_m is None or end_m is None:
        return _unknown_segment(step)
    gain_m = end_m - start_m
    return EnrichedSegment(
        index=step.index,
        instruction=step.instruction,
        start=step.start,
        end=step.end,
        polyline=step.polyline,
        duration_s=step.duration_s,
        distance_m=step.distance_m,
        elevation_classification=_classify_elevation(gain_m),
        elevation_gain_m=gain_m,
        elevation_start_m=start_m,
        elevation_end_m=end_m,
    )


async def enrich_route(steps: Sequence[RouteStepInfo]) -> list[EnrichedSegment]:
    if not steps:
        return []
    locations = _elevation_locations(steps)
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                ELEVATION_URL,
                params={
                    "key": settings.google_maps_api_key(),
                    "locations": "|".join(
                        f"{latitude:.6f},{longitude:.6f}"
                        for latitude, longitude in locations
                    ),
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Elevation API returned HTTP {response.status_code}")
        data = response.json()
        results = data.get("results") or []
        if data.get("status") != "OK" or len(results) != len(locations):
            status = data.get("status") or "no samples"
            raise RuntimeError(f"Elevation API returned {status}")
        elevations = {
            location: float(result["elevation"])
            for location, result in zip(locations, results, strict=True)
        }
        return [_enriched_segment(step, elevations) for step in steps]
    except Exception as exc:
        logger.warning(
            "Elevation enrichment failed for route leg: %s", type(exc).__name__
        )
        return [_unknown_segment(step) for step in steps]


async def weather_condition(point: LatLng) -> str:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                WEATHER_URL,
                params={
                    "key": settings.google_maps_api_key(),
                    "location.latitude": f"{point.lat:.6f}",
                    "location.longitude": f"{point.lon:.6f}",
                    "unitsSystem": "METRIC",
                    "languageCode": "en",
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Weather API returned HTTP {response.status_code}")
        value: dict[str, Any] = response.json()
        description = (
            ((value.get("weatherCondition") or {}).get("description") or {}).get("text")
        )
        temperature = value.get("temperature") or {}
        parts = [str(description)] if description else []
        if temperature.get("degrees") is not None:
            suffix = (
                "°C"
                if temperature.get("unit") == "CELSIUS"
                else f" {temperature.get('unit') or ''}".rstrip()
            )
            parts.append(f"{temperature['degrees']}{suffix}")
        return ", ".join(parts) or "Weather information unavailable"
    except Exception as exc:
        logger.warning("Weather enrichment failed: %s", type(exc).__name__)
        return "Weather information unavailable"


async def lookup_nearby_features(
    segments: Sequence[EnrichedSegment], route_phase: str
) -> dict[str, list[str]]:
    if not settings.nearby_context_enabled() or not segments:
        return {}
    total_distance = sum(max(0, segment.distance_m) for segment in segments)
    representative = segments[len(segments) // 2]
    if total_distance:
        distance_to_midpoint = total_distance / 2
        distance_so_far = 0
        for segment in segments:
            distance_so_far += max(0, segment.distance_m)
            if distance_so_far >= distance_to_midpoint:
                representative = segment
                break

    segment_id = f"{route_phase}_{representative.index}"
    midpoint = {
        "latitude": (representative.start.lat + representative.end.lat) / 2,
        "longitude": (representative.start.lon + representative.end.lon) / 2,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                NEARBY_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": settings.google_maps_api_key(),
                    "X-Goog-FieldMask": "places.displayName,places.primaryType",
                },
                json={
                    "includedTypes": NEARBY_FEATURE_TYPES,
                    "maxResultCount": 3,
                    "rankPreference": "DISTANCE",
                    "locationRestriction": {
                        "circle": {"center": midpoint, "radius": 150.0}
                    },
                },
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Places API returned HTTP {response.status_code}")
        names: list[str] = []
        for place in response.json().get("places") or []:
            name = ((place.get("displayName") or {}).get("text") or "").strip()
            if name and name not in names:
                names.append(name)
        return {segment_id: names} if names else {}
    except Exception as exc:
        logger.warning(
            "Nearby enrichment failed for route leg %s: %s",
            route_phase,
            type(exc).__name__,
        )
        return {}

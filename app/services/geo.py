"""Service-area evaluation and geocoding.

A tenant's `service_area` is a JSON object with any of:

    {
      "polygon":   GeoJSON Polygon or MultiPolygon (lng, lat pairs),
      "center":    {"lat": 45.04, "lng": -79.31},  "radius_km": 60,   # alternative to polygon
      "buffer_km": 25,                                                # 'near' band outside the edge
      "fsas":      ["P1L", "P1P", "P0B"]                               # postal-code fallback
    }

Evaluation returns one of in / near / out / unknown. Distances use a flat projection
centred on the area, which is accurate to well under a kilometre at Ontario latitudes
and sizes, and keeps PostGIS out of the stack.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import httpx
from shapely.geometry import MultiPolygon, Point, Polygon, shape

from app.models import AreaStatus

KM_PER_DEG_LAT = 110.574


@dataclass(frozen=True)
class GeocodeResult:
    lat: float
    lng: float
    formatted: str
    precise: bool  # rooftop / interpolated street address, not a town or postal-code centroid


def _project_factory(lat0: float):
    k = 111.320 * math.cos(math.radians(lat0))

    def project(lng: float, lat: float) -> tuple[float, float]:
        return (lng * k, lat * KM_PER_DEG_LAT)

    return project


def _project_geometry(geom: Polygon | MultiPolygon, project):
    if isinstance(geom, Polygon):
        return Polygon(
            [project(x, y) for x, y in geom.exterior.coords],
            [[project(x, y) for x, y in ring.coords] for ring in geom.interiors],
        )
    return MultiPolygon([_project_geometry(p, project) for p in geom.geoms])


def evaluate_point(service_area: dict, lat: float, lng: float) -> AreaStatus:
    buffer_km = float(service_area.get("buffer_km") or 0)

    polygon = service_area.get("polygon")
    if polygon:
        geom = shape(polygon)
        if not isinstance(geom, Polygon | MultiPolygon) or geom.is_empty:
            return AreaStatus.UNKNOWN
        lat0 = geom.centroid.y
        project = _project_factory(lat0)
        area_km = _project_geometry(geom, project)
        point_km = Point(*project(lng, lat))
        if area_km.contains(point_km):
            return AreaStatus.IN
        if buffer_km > 0 and area_km.distance(point_km) <= buffer_km:
            return AreaStatus.NEAR
        return AreaStatus.OUT

    center = service_area.get("center")
    radius_km = service_area.get("radius_km")
    if center and radius_km:
        d = haversine_km(float(center["lat"]), float(center["lng"]), lat, lng)
        if d <= float(radius_km):
            return AreaStatus.IN
        if buffer_km > 0 and d <= float(radius_km) + buffer_km:
            return AreaStatus.NEAR
        return AreaStatus.OUT

    return AreaStatus.UNKNOWN


def evaluate_fsa(service_area: dict, fsa: str | None) -> AreaStatus:
    fsas = service_area.get("fsas") or []
    if not fsas or not fsa:
        return AreaStatus.UNKNOWN
    return AreaStatus.IN if fsa.upper() in {f.upper() for f in fsas} else AreaStatus.OUT


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
PRECISE_TYPES = {"ROOFTOP", "RANGE_INTERPOLATED"}


def geocode(address: str, api_key: str | None, *, client: httpx.Client | None = None) -> GeocodeResult | None:
    """Geocode an address with the Google Geocoding API. Returns None when unavailable or unmatched."""
    if not api_key or not address:
        return None
    params = {"address": address, "region": "ca", "key": api_key}
    try:
        if client is None:
            with httpx.Client(timeout=6.0) as c:
                resp = c.get(GEOCODE_URL, params=params)
        else:
            resp = client.get(GEOCODE_URL, params=params)
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if data.get("status") != "OK" or not data.get("results"):
        return None
    top = data["results"][0]
    loc = top["geometry"]["location"]
    location_type = top["geometry"].get("location_type", "APPROXIMATE")
    precise = location_type in PRECISE_TYPES and not top.get("partial_match", False)
    return GeocodeResult(
        lat=float(loc["lat"]), lng=float(loc["lng"]), formatted=top.get("formatted_address", address), precise=precise
    )

from app.models import AreaStatus
from app.services import geo

# Rough box around Muskoka: Gravenhurst (south) to Huntsville (north).
MUSKOKA = {
    "polygon": {"type": "Polygon", "coordinates": [[[-79.75, 44.85], [-78.95, 44.85], [-78.95, 45.45], [-79.75, 45.45], [-79.75, 44.85]]]},
    "buffer_km": 25,
}

BRACEBRIDGE = (45.0387, -79.3072)
BARRIE = (44.3894, -79.6903)       # ~55 km south of the box
ORILLIA = (44.6082, -79.4196)      # ~27 km south of the southern edge
TORONTO = (43.6532, -79.3832)


def test_polygon_in_near_out():
    assert geo.evaluate_point(MUSKOKA, *BRACEBRIDGE) == AreaStatus.IN
    assert geo.evaluate_point(MUSKOKA, *TORONTO) == AreaStatus.OUT
    assert geo.evaluate_point(MUSKOKA, *BARRIE) == AreaStatus.OUT
    near = {**MUSKOKA, "buffer_km": 30}
    assert geo.evaluate_point(near, *ORILLIA) == AreaStatus.NEAR
    assert geo.evaluate_point({**MUSKOKA, "buffer_km": 0}, *ORILLIA) == AreaStatus.OUT


def test_circle_area():
    area = {"center": {"lat": 45.04, "lng": -79.31}, "radius_km": 60, "buffer_km": 25}
    assert geo.evaluate_point(area, *BRACEBRIDGE) == AreaStatus.IN
    assert geo.evaluate_point(area, *ORILLIA) == AreaStatus.IN      # ~49 km
    assert geo.evaluate_point(area, *BARRIE) == AreaStatus.NEAR      # ~78 km
    assert geo.evaluate_point(area, *TORONTO) == AreaStatus.OUT


def test_fsa_fallback():
    area = {"fsas": ["P1L", "P1P", "P1H"]}
    assert geo.evaluate_fsa(area, "P1L") == AreaStatus.IN
    assert geo.evaluate_fsa(area, "m4e") == AreaStatus.OUT
    assert geo.evaluate_fsa(area, None) == AreaStatus.UNKNOWN
    assert geo.evaluate_fsa({}, "P1L") == AreaStatus.UNKNOWN


def test_empty_area_is_unknown():
    assert geo.evaluate_point({}, *BRACEBRIDGE) == AreaStatus.UNKNOWN


def test_haversine_sanity():
    d = geo.haversine_km(*BRACEBRIDGE, *TORONTO)
    assert 150 < d < 160


def test_geocode_without_key_is_none():
    assert geo.geocode("12 Birch Lane, Bracebridge", None) is None


def test_geocode_parses_google_response(monkeypatch):
    class FakeResp:
        def json(self):
            return {
                "status": "OK",
                "results": [{"geometry": {"location": {"lat": 45.04, "lng": -79.31}, "location_type": "ROOFTOP"}, "formatted_address": "12 Birch Ln, Bracebridge, ON"}],
            }

    class FakeClient:
        def get(self, url, params):
            assert params["address"] == "12 Birch Lane"
            return FakeResp()

    r = geo.geocode("12 Birch Lane", "key", client=FakeClient())
    assert r is not None and r.precise and abs(r.lat - 45.04) < 1e-6

#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_corridor.py — exits non-zero on failure."""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-cor-")).resolve()
os.environ["COUNTKIT_ROOT"] = str(TMP)

import app                                       # noqa: E402
from fastapi.testclient import TestClient        # noqa: E402

C = TestClient(app.app)
FAILS = []
ROAD = [[-15.39, 28.32], [-15.40, 28.33], [-15.41, 28.34]]


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


class FakeRequests:
    """Stands in for the requests module: `import requests` inside corridor_live
    resolves through sys.modules, so replacing the entry is the whole monkeypatch."""

    def __init__(self, speeds, boom=False):
        self.speeds, self.boom, self.calls = list(speeds), boom, []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((json["origin"], json["destination"]))
        assert headers["X-Goog-Api-Key"] == "k", headers
        assert json["extraComputations"] == ["TRAFFIC_ON_POLYLINE"], json
        if self.boom:
            raise OSError("no route to host")
        spans = self.speeds.pop(0)
        return FakeResponse({"routes": [{"travelAdvisory": {
            "speedReadingIntervals": [{"speed": s} for s in spans]}}]})


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def configure(**over):
    app.CONFIG["google_routes"] = {"api_key": over.pop("key", "k")}
    app.CONFIG["corridor"] = {"enabled": True, "road": ROAD,
                              "cross": [{"name": "Church Rd", "pts": ROAD[:2]}],
                              "junctions": [{"i": 1, "name": "Arcades", "site": ""}],
                              **over}
    app._corridor_cache.update(at=0.0, payload=None)


def unconfigure():
    app.CONFIG.pop("corridor", None)
    app.CONFIG["google_routes"] = {"api_key": ""}
    app._corridor_cache.update(at=0.0, payload=None)


def fake(speeds, boom=False):
    f = FakeRequests(speeds, boom)
    sys.modules["requests"] = f
    return f


def tree():
    return sorted(p.relative_to(TMP).as_posix() for p in TMP.rglob("*") if p.is_file())


# ---- gated off by default ----

def unconfigured_is_clean():
    unconfigure()
    r = C.get("/api/corridor")
    assert r.status_code == 200, r.status_code
    assert r.json() == {"configured": False}, r.json()


def a_key_without_enabled_stays_off():
    configure(enabled=False)
    assert C.get("/api/corridor").json() == {"configured": False}
    # And enabled without a key is equally off — both halves are required.
    configure(key="")
    assert C.get("/api/corridor").json() == {"configured": False}


def no_google_request_without_config():
    unconfigure()
    f = fake([])
    C.get("/api/corridor")
    assert f.calls == [], "called Google with no key configured"


# ---- the live glance ----

def speeds_map_to_levels():
    configure()
    f = fake([["NORMAL"], ["SLOW", "TRAFFIC_JAM"]])
    d = C.get("/api/corridor").json()
    assert d["configured"] is True, d
    assert len(f.calls) == 2, "one request per adjacent pair of road points"
    assert [s["level"] for s in d["segments"]] == [0, 2], d["segments"]
    # The worst reading wins: a jam inside a span must not average away to SLOW.
    assert d["segments"][1]["a"] == ROAD[1] and d["segments"][1]["b"] == ROAD[2], d
    assert d["road"] == ROAD and d["junctions"][0]["name"] == "Arcades", d


def an_empty_advisory_reads_as_normal():
    configure()
    fake([[], []])
    assert [s["level"] for s in C.get("/api/corridor").json()["segments"]] == [0, 0]


def the_glance_is_cached_in_memory_for_a_minute():
    configure()
    f = fake([["NORMAL"], ["SLOW"]])
    first = C.get("/api/corridor").json()
    second = C.get("/api/corridor").json()
    assert len(f.calls) == 2, "second call re-queried Google inside the TTL"
    assert first["segments"] == second["segments"], "cached payload changed"


def a_dead_key_degrades_to_no_glance():
    configure()
    fake([], boom=True)
    d = C.get("/api/corridor").json()
    assert d["configured"] is True and d["segments"] == [], d
    assert "no route to host" in d["error"], d
    assert d["road"] == ROAD, "the corridor still draws without the live colours"


# ---- Google's terms: live only ----

def nothing_is_persisted():
    configure()
    before = tree()
    fake([["TRAFFIC_JAM"], ["SLOW"]])
    C.get("/api/corridor")
    assert tree() == before, [p for p in tree() if p not in before]
    assert app._corridor_cache["payload"], "the TTL cache should hold it in memory only"


def the_page_and_vendored_leaflet_are_served():
    html = C.get("/corridor").text
    assert "Corridor congestion" in html, html[:200]
    for bit in ("speedReadingIntervals", "NORMAL / SLOW / TRAFFIC_JAM",
                "display-only glance", "powered by Google", "#/counts",
                "map tiles unavailable"):
        assert bit in html, bit
    assert "unpkg" not in html.split("<script>")[0] or "vendor/leaflet" in html
    assert C.get("/static/vendor/leaflet/leaflet.js").status_code == 200, "leaflet not vendored"
    assert C.get("/static/vendor/leaflet/leaflet.css").status_code == 200


check("unconfigured returns configured:false cleanly", unconfigured_is_clean)
check("key and enabled are both required", a_key_without_enabled_stays_off)
check("no Google request is made while unconfigured", no_google_request_without_config)
check("speedReadingIntervals map to levels, worst wins", speeds_map_to_levels)
check("an empty advisory reads as NORMAL", an_empty_advisory_reads_as_normal)
check("the glance is cached in memory for the TTL", the_glance_is_cached_in_memory_for_a_minute)
check("a dead key degrades to no glance, not an error page", a_dead_key_degrades_to_no_glance)
check("nothing from Google is written to disk", nothing_is_persisted)
check("the map page serves with Leaflet vendored", the_page_and_vendored_leaflet_are_served)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

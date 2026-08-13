#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_advisor.py — exits non-zero on failure.

The gate advisor proposes, validates, and persists — and only an operator's Save
ever turns a proposal into a calibration version."""

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-adv-")).resolve()
os.environ["COUNTKIT_ROOT"] = str(TMP)

import app                                      # noqa: E402
import calib                                    # noqa: E402
import gate_advisor                             # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

C = TestClient(app.app)
FAILS = []
SITE, CAM = "gerlache", "cam1"


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


# A 640x480 view. The entry gate at y=100 sits in the flow; the exit gate at y=460
# is beyond where tracks survive — the Kalambo failure in miniature.
CAL = {"image_size": [640, 480],
       "lines": [{"kind": "entry", "name": "North", "compass": "N", "dir": "AB",
                  "points": [[100, 100], [500, 100]]},
                 {"kind": "exit", "name": "North", "compass": "N", "dir": "AB",
                  "points": [[100, 460], [500, 460]]}]}


def tracks_dying_early(n=40):
    """Tracks that cross y=100 heading down, then die around y=300 mid-frame."""
    out = {}
    for t in range(n):
        x = 120.0 + t * 8
        out[t] = [(x, 60.0), (x, 140.0), (x, 220.0), (x + 4, 295.0 + (t % 7))]
    return out


def the_analytics_find_the_starved_gate():
    doc, stats = gate_advisor.advise(tracks_dying_early(), CAL)
    assert stats["gates"]["North/entry"] == 40, stats
    assert stats["gates"]["North/exit"] == 0, stats
    assert stats["proposed"] == ["North/exit"], stats
    assert stats["edge_death_pct"] == 0.0, stats
    # The replacement line runs through the terminal cloud, not the dead zone.
    new = [l for l in doc["lines"] if l["kind"] == "exit"][0]
    ys = [p[1] for p in new["points"]]
    assert all(250 < y < 350 for y in ys), new["points"]
    assert new["advisor"]["replaces"] == [[100, 460], [500, 460]], new["advisor"]
    # The healthy gate passes through untouched.
    kept = [l for l in doc["lines"] if l["kind"] == "entry"][0]
    assert kept["points"] == [[100, 100], [500, 100]] and "advisor" not in kept


def a_healthy_junction_proposes_nothing():
    tracks = {}
    for t in range(30):
        x = 120.0 + t * 10
        tracks[t] = [(x, 60.0), (x, 140.0), (x, 300.0), (x, 470.0)]   # full transit
    doc, stats = gate_advisor.advise(tracks, CAL)
    assert stats["proposed"] == [], stats
    assert all("advisor" not in l for l in doc["lines"]), doc["lines"]


def the_proposal_validates_and_saves_as_a_normal_version():
    doc, _ = gate_advisor.advise(tracks_dying_early(), CAL)
    calib._validate(doc)                        # what the Save button will run
    calib.save_calibration(SITE, CAM, CAL)      # v1: the operator's original
    r = C.post(f"/api/calib/{SITE}/{CAM}", json=doc)
    assert r.status_code == 200 and r.json()["version"] == 2, r.text
    # v1 survives untouched — never overwritten — and the saved doc keeps provenance.
    assert calib.get_calibration(SITE, CAM, 1) == CAL
    saved = calib.get_calibration(SITE, CAM, 2)
    assert saved["note"].startswith("gate-advisor proposal"), saved.get("note")
    assert saved["lines"][1]["advisor"]["replaces"], "provenance stripped on save"


def the_endpoint_serves_a_run_and_404s_before_one():
    r = C.get(f"/api/advisor/{SITE}/cam9")
    assert r.status_code == 404 and "gate_advisor.py" in r.json()["detail"], r.text
    # Pinned to the ORIGINAL calibration: the previous test activated its saved
    # advisor proposal, whose exit line already sits in the flow — run() against
    # that would rightly propose nothing, and this test's verdict would depend on
    # test order instead of the fixture.
    calib.set_active(SITE, CAM, 1)
    out = gate_advisor.run(SITE, "2026-08-04", CAM, app.data_root(), TMP / "ingest",
                           {}, tracks=tracks_dying_early())
    assert out["proposed_gates"] == ["North/exit"], out
    r = C.get(f"/api/advisor/{SITE}/{CAM}")
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["proposal"]["lines"] and a["stats"]["tracks"] == 40, a["stats"]
    assert a["critique"] is None, "no endpoint configured must mean no critique"


check("the analytics find the starved gate", the_analytics_find_the_starved_gate)
check("a healthy junction proposes nothing", a_healthy_junction_proposes_nothing)
check("the proposal validates and saves as a normal version",
      the_proposal_validates_and_saves_as_a_normal_version)
check("the endpoint serves a run and 404s before one",
      the_endpoint_serves_a_run_and_404s_before_one)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

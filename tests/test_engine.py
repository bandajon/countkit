#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_engine.py — exits non-zero on failure."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-engine-")).resolve()
os.environ["COUNTKIT_ROOT"] = str(TMP)

import app                                      # noqa: E402
import calib                                    # noqa: E402
import engine                                   # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

C = TestClient(app.app)
FAILS = []
SITE, DATE = "gerlache", "2026-08-04"
CHINA = -21600.0                                # a camera left on China time
SEG = "20260804-070000.mkv"                     # 07:00:00 local wallclock
FIX = TMP / "fixtures"


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


def gate(kind, name, dirn="AB"):
    # A horizontal gate across the frame; travel A->B is +x, so its LEFT — the side the
    # chevron points at, and for dir 'AB' the side a counted vehicle must ARRIVE on —
    # is smaller y. Objects therefore count when moving UPWARD across the line.
    return {"kind": kind, "name": name, "compass": "", "dir": dirn,
            "points": [[0, 300], [1000, 300]]}


def frames(objs_by_t):
    return "\n".join(json.dumps({"t": t, "objects": o}) for t, o in objs_by_t)


def obj(oid, y, cls="car"):
    return {"id": oid, "cls": cls, "bbox": [100, y - 10, 20, 20], "centroid": [500, y]}


def build(offsets={"cam1": 0.0, "cam2": CHINA}, verified=True):
    """Two cameras: cam1 owns the entry line, cam2 the exit line."""
    day = TMP / "ingest" / DATE / SITE
    for cam in ("cam1", "cam2"):
        (day / cam).mkdir(parents=True, exist_ok=True)
    if verified:
        (day / ".verified").touch()
    elif (day / ".verified").exists():
        (day / ".verified").unlink()
    (day / "manifest.json").write_text(json.dumps({"time_offset_s": offsets}))

    fd = FIX / SITE / DATE
    fd.mkdir(parents=True, exist_ok=True)
    (fd / "segments.json").write_text(json.dumps(
        {"cam1": [{"file": SEG, "duration": 600}], "cam2": [{"file": SEG, "duration": 600}]}))

    # obj 1 crosses IN (bottom -> top, arriving on the chevron side) at t=10, then
    # re-crosses at t=12 (inside the 5 s debounce: must NOT double-count). obj 2 wobbles
    # on the line — it first crosses against the gate, then back with it 2 s later; the
    # debounce must swallow the second, or a vehicle stopped on the gate makes counts.
    (fd / "cam1.jsonl").write_text(frames([
        (0.0, [obj(1, 400), obj(2, 200)]),
        (10.0, [obj(1, 200), obj(2, 400)]),          # 1 crosses IN, 2 crosses against
        (12.0, [obj(1, 400), obj(2, 200)]),          # both re-cross within 5 s
        (30.0, [obj(3, 400, "minibus")]),
        (31.0, [obj(3, 200, "minibus")]),            # 3 crosses IN
    ]))
    (fd / "cam2.jsonl").write_text(frames([
        (0.0, [obj(7, 400)]),
        (5.0, [obj(7, 200)]),                        # exit crossing on the China-time cam
    ]))
    calib.save_calibration(SITE, "cam1", {"image_size": [1000, 600],
                                          "lines": [gate("entry", "Great East Rd")]})
    calib.save_calibration(SITE, "cam2", {"image_size": [1000, 600],
                                          "lines": [gate("exit", "Great East Rd")]})


def run():
    return engine.analyze(SITE, DATE, TMP / "ingest", app.data_root(),
                          engine.mock_factory(FIX))


def rows():
    db = engine.connect(app.data_root(), SITE)
    r = db.execute("SELECT cam, obj_id, cls, line, kind, corrected_ts, crop FROM events "
                   "WHERE site=? AND date=? ORDER BY corrected_ts", (SITE, DATE)).fetchall()
    db.close()
    return r


# ---- geometry, in isolation: the part that decides every number in the report ----

def chevron_convention():
    """Pinned by hand so the convention can never silently invert again.

    Gate [[0,100],[200,100]] with dir 'AB': travel A->B is +x, its LEFT in screen
    coords (y grows downward) is y < 100, and that is where the Label tab draws the
    chevron. The arrow shows the way counted traffic travels, so a vehicle must ARRIVE
    at y < 100 to count.
        (50,150) -> (50,50)  moves UP, into the chevron side  => counts       (True)
        (50,50)  -> (50,150) moves DOWN, away from it         => not a count  (False)
    """
    g = {"kind": "entry", "name": "pin", "compass": "", "dir": "AB",
         "points": [[0, 100], [200, 100]]}
    assert engine.gate_crossing(g, [50, 150], [50, 50]) is True, "into the chevron side"
    assert engine.gate_crossing(g, [50, 50], [50, 150]) is False, "away from the chevron"


def geometry():
    g = gate("entry", "x")                            # IN side is y < 300
    assert engine.gate_crossing(g, [500, 400], [500, 200]) is True, "upward counts"
    assert engine.gate_crossing(g, [500, 200], [500, 400]) is False, "downward is against"
    flipped = gate("entry", "x", "BA")
    assert engine.gate_crossing(flipped, [500, 200], [500, 400]) is True, "BA reverses"
    assert engine.gate_crossing(flipped, [500, 400], [500, 200]) is False
    # Beyond the gate's end the infinite line still flips sign — the segment must not.
    assert engine.gate_crossing(g, [1500, 200], [1500, 400]) is None, "past the end"
    assert engine.gate_crossing(g, [500, 100], [500, 200]) is None, "never reaches it"


def epochs():
    assert engine.segment_epoch(SEG) == time.mktime(time.strptime(
        "20260804-070000", "%Y%m%d-%H%M%S"))
    try:
        engine.segment_epoch("nonsense.mkv")
        raise AssertionError("bad segment name accepted")
    except ValueError:
        pass


# ---- the pipeline end to end ----

def detects_and_debounces():
    build()
    r = run()
    assert r["events"] == 3, f"expected 3 crossings, got {r['events']}: {rows()}"
    got = [(x[0], x[1], x[2], x[4]) for x in rows()]
    assert ("cam1", 1, "car", "entry") in got, got
    assert ("cam1", 3, "minibus", "entry") in got, got
    assert ("cam2", 7, "car", "exit") in got, got
    assert len([x for x in got if x[1] == 1]) == 1, "re-cross inside 5 s double-counted"
    assert not [x for x in got if x[1] == 2], "a crossing against the gate dir counted"


def offset_is_applied():
    base = engine.segment_epoch(SEG)
    by_cam = {x[0]: x[5] for x in rows()}
    assert abs(by_cam["cam2"] - (base + 5.0 + CHINA)) < 0.01, by_cam
    assert abs(by_cam["cam1"] - (base + 31.0)) < 0.01, by_cam     # cam1 is synced
    # The whole point of the offset: the two cameras' clocks now agree to within hours.
    assert by_cam["cam2"] < by_cam["cam1"] - 3600, "China-time camera not corrected"


def crops_written():
    for cam, oid, cls, line, kind, ts, crop in rows():
        assert crop and crop.startswith(f"{SITE}/{DATE}/{cam}/"), crop
        p = app.data_root() / "crops" / crop
        assert p.exists() and p.read_bytes()[:2] == b"\xff\xd8", f"no crop at {p}"


def rerun_is_idempotent():
    before = rows()
    crop_dir = app.data_root() / "crops" / SITE / DATE
    stale = crop_dir / "cam1" / "999-stale-1.jpg"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_bytes(b"\xff\xd8stale")
    run()
    assert rows() == before, "re-run changed the events"
    assert not stale.exists(), "re-run left a crop from the previous run behind"


def unset_offset_blocks():
    build(offsets={"cam1": 0.0})                      # cam2 missing entirely
    r = C.post("/api/analyze/queue", json={"site": SITE, "date": DATE})
    assert r.status_code == 400, r.status_code
    assert r.json()["detail"] == ("cam2: time offset unset — set it in Counts → "
                                  "Offsets. Unset is not zero."), r.json()
    build(offsets={"cam1": 0.0, "cam2": 0.0})         # explicit zero is a real answer
    assert C.post("/api/analyze/queue", json={"site": SITE, "date": DATE}).status_code == 200
    build()                                            # restore


def unverified_blocks():
    build(verified=False)
    r = C.post("/api/analyze/queue", json={"site": SITE, "date": DATE})
    assert r.status_code == 400 and "not verified" in r.json()["detail"], r.json()
    build()


def queue_serialises():
    marks = []

    def run_one(site, date, say, cancelled):
        marks.append(("start", time.time()))
        time.sleep(0.4)
        say(msg="done", pct=100)
        marks.append(("end", time.time()))
        return {"events": 0}

    jobs = engine.Jobs(TMP / "qtest", run_one)
    (TMP / "qtest").mkdir(exist_ok=True)
    a, b = jobs.add(SITE, DATE), jobs.add(SITE, "2026-08-05")
    time.sleep(0.15)
    states = {j["id"]: j["state"] for j in jobs.list()}
    assert states[a["id"]] == "RUNNING" and states[b["id"]] == "QUEUED", states
    for _ in range(60):
        if all(j["state"] == "DONE" for j in jobs.list()):
            break
        time.sleep(0.1)
    assert [j["state"] for j in jobs.list()] == ["DONE", "DONE"], jobs.list()
    order = [x[0] for x in marks]
    assert order == ["start", "end", "start", "end"], f"jobs overlapped: {order}"


def survives_restart():
    d = TMP / "restart"
    d.mkdir(exist_ok=True)
    (d / "jobs.json").write_text(json.dumps([
        {"id": 1, "site": SITE, "date": DATE, "state": "RUNNING", "attempts": 1,
         "progress": {"pct": 40}, "log": [], "result": None, "queued": 0},
        {"id": 2, "site": SITE, "date": "2026-08-05", "state": "QUEUED", "attempts": 0,
         "progress": {}, "log": [], "result": None, "queued": 0}]))
    seen = []
    jobs = engine.Jobs(d, lambda *a: seen.append(a) or {"events": 0})
    states = [j["state"] for j in sorted(jobs.list(), key=lambda j: j["id"])]
    assert states[1] == "QUEUED" or states[1] == "RUNNING", states
    for _ in range(40):
        if all(j["state"] == "DONE" for j in jobs.list()):
            break
        time.sleep(0.1)
    assert len(seen) == 2, f"a job caught RUNNING by a restart was not re-run: {seen}"


def api_reports_state():
    build()
    run()
    s = C.get(f"/api/analyze/status/{SITE}/{DATE}").json()
    assert s["blocked"] == "" and s["events"] == 3, s
    assert C.get("/api/analyze/jobs").status_code == 200


check("chevron convention: counted traffic arrives on the arrow side", chevron_convention)
check("crossing geometry: direction, dir flip, segment ends", geometry)
check("segment filenames parse to wallclock", epochs)
check("crossings detected once per object per gate", detects_and_debounces)
check("clock offset corrects a China-time camera", offset_is_applied)
check("every event carries a written crop", crops_written)
check("re-run replaces events and crops", rerun_is_idempotent)
check("unset offset blocks the site-day with the exact copy", unset_offset_blocks)
check("unverified footage is refused", unverified_blocks)
check("queue runs one job at a time", queue_serialises)
check("a job caught RUNNING by a restart is re-run", survives_restart)
check("status route reports blocked and event counts", api_reports_state)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

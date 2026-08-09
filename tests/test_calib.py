#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_calib.py — exits non-zero on failure."""

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-calib-")).resolve()
os.environ["COUNTKIT_ROOT"] = str(TMP)

import app                                      # noqa: E402
import calib                                    # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

C = TestClient(app.app)
FAILS = []


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


def gate(kind, name, compass="", pts=((10, 10), (90, 10))):
    return {"kind": kind, "name": name, "compass": compass,
            "points": [list(p) for p in pts], "dir": "AB"}


def doc(*gates):
    return {"image_size": [1920, 1080], "lines": list(gates)}


def post(site, cam, d):
    return C.post(f"/api/calib/{site}/{cam}", json=d)


def versions_are_immutable():
    assert post("gerlache", "cam1", doc(gate("entry", "Great East Rd"))).json()["version"] == 1
    assert calib.active_version("gerlache", "cam1") == 1
    r = post("gerlache", "cam1", doc(gate("entry", "Great East Rd"),
                                     gate("exit", "Great East Rd")))
    assert r.json()["version"] == 2, r.json()
    v1 = TMP / "data" / "calibrations" / "gerlache" / "cam1" / "v1.json"
    assert len(json.loads(v1.read_text())["lines"]) == 1, "v1 must survive the v2 save"
    vs = C.get("/api/calib/gerlache/cam1/versions").json()
    assert [(v["version"], v["active"], v["lines"]) for v in vs] == [(1, False, 1), (2, True, 2)], vs
    assert vs[0]["saved"].startswith("20"), vs[0]


def activate_repoints_only():
    assert C.post("/api/calib/gerlache/cam1/activate", json={"version": 1}).status_code == 200
    assert len(C.get("/api/calib/gerlache/cam1").json()["lines"]) == 1
    assert (TMP / "data/calibrations/gerlache/cam1/v2.json").exists(), "v2 must not be deleted"
    assert C.post("/api/calib/gerlache/cam1/activate", json={"version": 9}).status_code == 400
    C.post("/api/calib/gerlache/cam1/activate", json={"version": 2})   # leave v2 active


def invalid_docs_rejected():
    bad = {
        "no lines": doc(),
        "one-point line": doc(gate("entry", "GE", pts=((1, 1),))),
        "bad kind": doc(gate("crossing", "GE")),
        "bad dir": {"image_size": [1920, 1080],
                    "lines": [{"kind": "entry", "name": "GE",
                               "points": [[1, 1], [2, 2]], "dir": "sideways"}]},
        "no name": doc(gate("entry", "  ")),
        "bad image_size": {"image_size": [0, 1080], "lines": [gate("entry", "GE")]},
    }
    for why, d in bad.items():
        r = post("gerlache", "cam9", d)
        assert r.status_code == 400, f"{why}: got {r.status_code}"
        assert r.json()["detail"], why
    assert not (TMP / "data/calibrations/gerlache/cam9").exists(), "rejects must not create dirs"


def path_traversal_rejected():
    # HTTP clients collapse ../ before the request leaves, so the guard has to hold at
    # the calib layer — that is also what every later task calls directly.
    def rejects(what, fn):
        try:
            fn()
            raise AssertionError(f"{what} was accepted")
        except ValueError:
            pass

    for bad in ("../x", "g/../..", "", "a.b", "x" * 65):
        rejects(f"site {bad!r}", lambda: calib.arm_map(bad))
        rejects(f"site {bad!r}", lambda: calib.get_calibration(bad, "cam1"))
        rejects(f"cam {bad!r}", lambda: calib.get_calibration("gerlache", bad))
        rejects(f"cam {bad!r}",
                lambda: calib.save_calibration("gerlache", bad, doc(gate("entry", "GE"))))
    # Anything that survives URL encoding must 400 at the route, not 500 or a file read.
    assert C.get("/api/calib/gerlache/%2e%2e").status_code == 400   # ".." reaches the route
    assert C.get("/api/calib/gerlache/%2e%2e%2f%2e%2e").status_code == 404   # decodes to 3 segments
    assert post("gerlache", "a.b", doc(gate("entry", "GE"))).status_code == 400
    assert not (TMP / "data" / "calibrations" / "x").exists()


def armmap_states():
    # cam1 owns Great East entry+exit (from the earlier saves); cam2 owns Church Rd entry
    # only, and re-declares Great East entry — double-owning it.
    post("gerlache", "cam2", doc(gate("entry", "Church Rd", "N"),
                                 gate("entry", "great east rd")))
    m = C.get("/api/calib/gerlache/armmap").json()
    assert m["cams"] == ["cam1", "cam2"], m["cams"]
    arms = {a["arm"].lower(): a for a in m["arms"]}
    ge = arms["great east rd"]
    assert ge["entry"] == ["cam1", "cam2"] and ge["state"] == "double", ge
    church = arms["church rd"]
    assert church["exit"] == [] and church["state"] == "unowned" and church["compass"] == "N", church
    assert m["notes"] == [{"cam": "cam2",
                           "note": "volumes only from this view — exits live on other cameras"}], m

    # A clean two-camera site: cam1 owns the entry, cam2 the exit.
    post("kabulonga", "cam1", doc(gate("entry", "Great East Rd", "E")))
    r = post("kabulonga", "cam2", doc(gate("exit", "Great East Rd", "E")))
    owned = r.json()["armmap"]["arms"][0]      # the save returns the fresh map
    assert owned == {"arm": "Great East Rd", "compass": "E", "entry": ["cam1"],
                     "exit": ["cam2"], "state": "owned"}, owned


def missing_calibration_404():
    assert C.get("/api/calib/gerlache/nosuchcam").status_code == 404
    assert C.get("/api/calib/emptysite/armmap").json() == {"arms": [], "notes": [], "cams": []}


def reference_frame_upload():
    mkv = TMP / "ingest" / "2026-08-04" / "gerlache" / "cam1"
    mkv.mkdir(parents=True, exist_ok=True)
    (mkv / "0500.mkv").write_bytes(b"not really an mkv")
    # Footage present but undecodable: a clean 404 with a message, never a stack trace.
    r = C.get("/api/frame/gerlache/cam1")
    assert r.status_code == 404, r.status_code
    assert ("0500.mkv" in r.text) or ("ffmpeg not found" in r.text), r.text
    assert C.get("/api/frame/gerlache/cam7").status_code == 404, "no footage at all"

    assert C.post("/api/frame/gerlache/cam1", content=b"<html>").status_code == 400
    jpeg = b"\xff\xd8\xff\xe0 pretend jpeg"
    assert C.post("/api/frame/gerlache/cam1", content=jpeg).status_code == 200
    r = C.get("/api/frame/gerlache/cam1")     # upload wins over the mkv path
    assert r.status_code == 200 and r.content == jpeg, r.status_code
    assert r.headers["content-type"] == "image/jpeg"


def concurrent_saves_both_survive():
    # Handlers run in FastAPI's threadpool: read-then-write let both saves pick the same
    # version number and one was silently overwritten.
    start = threading.Barrier(2)
    got = []

    def save(name):
        start.wait()
        got.append(calib.save_calibration("race", "cam1", doc(gate("entry", name)))["version"])

    ts = [threading.Thread(target=save, args=(n,)) for n in ("A", "B")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(got) == [1, 2], got
    d = TMP / "data" / "calibrations" / "race" / "cam1"
    arms = sorted(json.loads((d / f"v{n}.json").read_text())["lines"][0]["name"]
                  for n in (1, 2))
    assert arms == ["A", "B"], f"a save was destroyed: {arms}"


def a_trailing_newline_is_not_a_new_site():
    for bad in ("site1\n", "cam1\n", "\n"):
        try:
            calib.get_calibration(bad, "cam1")
            raise AssertionError(f"{bad!r} was accepted as a site name")
        except ValueError:
            pass
    assert not (TMP / "data" / "calibrations" / "site1\n").exists()


def an_oversize_reference_is_refused():
    big = b"\xff\xd8" + b"x" * (25 << 20)
    assert C.post("/api/frame/gerlache/cam1", content=big).status_code == 413
    r = C.get("/api/frame/gerlache/cam1")          # the earlier upload is untouched
    assert r.status_code == 200 and len(r.content) < 100, len(r.content)


check("saves are versioned and never overwritten", versions_are_immutable)
check("activate repoints without deleting", activate_repoints_only)
check("invalid calibrations rejected with 400", invalid_docs_rejected)
check("path traversal in site/cam names rejected", path_traversal_rejected)
check("arm map: owned / unowned / double / volumes-only note", armmap_states)
check("missing calibration is 404, empty site is an empty map", missing_calibration_404)
check("reference frame: upload wins, non-JPEG rejected", reference_frame_upload)
check("two concurrent saves both survive as v1 and v2", concurrent_saves_both_survive)
check("a name with a trailing newline is rejected", a_trailing_newline_is_not_a_new_site)
check("an oversize reference photo is refused with 413", an_oversize_reference_is_refused)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

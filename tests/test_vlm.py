#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_vlm.py — exits non-zero on failure.

VLM class proposals: a machine's class suggestion annotates the review queue and
never moves a number by itself. The human's confirmed row carries provenance."""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-vlm-")).resolve()
os.environ["COUNTKIT_ROOT"] = str(TMP)

import aggregate                                # noqa: E402
import app                                      # noqa: E402
import calib                                    # noqa: E402
import engine                                   # noqa: E402
import vlm_batch                                # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

C = TestClient(app.app)
FAILS = []
SITE, DATE = "gerlache", "2026-08-04"
CAT = timezone(timedelta(hours=2))
GE, CH = "Great East Rd", "Church Rd"


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


def ts(hhmm, sec=0):
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 8, 4, h, m, sec, tzinfo=CAT).timestamp()


def write(events):
    db = engine.connect(app.data_root(), SITE)
    db.execute("DELETE FROM events WHERE site=? AND date=?", (SITE, DATE))
    for cam, oid, cls, line, kind, t, crop in events:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                   (SITE, DATE, cam, oid, cls, line, kind, t, crop))
    db.commit()
    db.close()


def calibrate():
    line = lambda kind, name: {"kind": kind, "name": name, "compass": "", "dir": "AB",
                               "points": [[0, 100], [200, 100]]}
    calib.save_calibration(SITE, "cam1", {"image_size": [640, 480],
                                          "lines": [line("entry", GE), line("exit", GE)]})


def segments(cam, hhmms):
    d = TMP / "ingest" / DATE / SITE / cam
    d.mkdir(parents=True, exist_ok=True)
    for hhmm in hhmms:
        h, m = hhmm.split(":")
        (d / f"20260804-{h}{m}00.mkv").write_bytes(b"")


def sidecar(rows):
    p = TMP / "data" / "vlm" / f"{SITE}-{DATE}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def crop_file(name, blob=b"\xff\xd8x"):
    d = TMP / "data" / "crops" / SITE / DATE / "cam1"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(blob)
    return f"{SITE}/{DATE}/cam1/{name}"


calibrate()
segments("cam1", ["07:00"])


# ---- proposals annotate the queue and move nothing ----

def a_proposal_annotates_and_moves_nothing():
    write([("cam1", 1, "car", GE, "entry", ts("07:03"), None)])
    sidecar([{"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
              "ts": ts("07:03"), "proposed": "lgv", "model": "gemma4:12b"}])
    r = C.get(f"/api/refine/events/{SITE}/{DATE}").json()
    e = r["events"][0]
    assert e["proposed"] == "lgv" and e["proposed_by"] == "gemma4:12b", e
    assert r["proposed_total"] == 1, r
    # The count still reports the DETECTOR's class — nothing moved.
    d = aggregate.counts(SITE, DATE, app.data_root(), TMP / "ingest", app.CONFIG)
    hit = [b for b in d["bins"] if b["total"]][0]
    assert hit["arms"][GE]["classes"] == {"passenger_car": 1}, hit["arms"][GE]


def a_proposal_survives_an_offset_edit():
    # Same slack contract as refinements: the stamp moved 60 s, the proposal holds.
    write([("cam1", 1, "car", GE, "entry", ts("07:03") + 60, None)])
    sidecar([{"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
              "ts": ts("07:03"), "proposed": "lgv", "model": "m"}])
    e = C.get(f"/api/refine/events/{SITE}/{DATE}").json()["events"][0]
    assert e["proposed"] == "lgv", e


def a_regenerated_obj_id_proposes_nothing():
    # Re-analysis renumbered the tracker: the cached row matches no event and is
    # silently dropped — the sidecar is a cache, regeneration is cheap.
    write([("cam1", 99, "car", GE, "entry", ts("07:03"), None)])
    sidecar([{"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
              "ts": ts("07:03"), "proposed": "lgv", "model": "m"}])
    r = C.get(f"/api/refine/events/{SITE}/{DATE}").json()
    assert r["events"][0]["proposed"] is None and r["proposed_total"] == 0, r


def an_off_target_proposal_never_surfaces():
    # A sidecar from an older config must not offer a button POST /api/refine refuses.
    write([("cam1", 1, "car", GE, "entry", ts("07:03"), None)])
    sidecar([{"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
              "ts": ts("07:03"), "proposed": "hovercraft", "model": "m"}])
    r = C.get(f"/api/refine/events/{SITE}/{DATE}").json()
    assert r["events"][0]["proposed"] is None, r["events"][0]


def a_confirmed_proposal_carries_provenance():
    write([("cam1", 1, "car", GE, "entry", ts("07:03"), None)])
    (TMP / "data" / "refine").mkdir(parents=True, exist_ok=True)
    (TMP / "data" / "refine" / f"{SITE}-{DATE}.jsonl").write_text("")
    r = C.post("/api/refine", json={"site": SITE, "date": DATE, "rows": [
        {"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry", "ts": ts("07:03"),
         "to": "lgv", "src": "vlm:gemma4:12b"}]})
    assert r.status_code == 200, r.text
    row = json.loads((TMP / "data" / "refine" / f"{SITE}-{DATE}.jsonl")
                     .read_text().splitlines()[-1])
    assert row["src"] == "vlm:gemma4:12b", row
    d = aggregate.counts(SITE, DATE, app.data_root(), TMP / "ingest", app.CONFIG)
    assert d["qa"]["refined_assisted"] == 1, d["qa"]["refined_assisted"]
    # And the refinement itself applies like any human one.
    hit = [b for b in d["bins"] if b["total"]][0]
    assert hit["arms"][GE]["classes"] == {"lgv": 1}, hit["arms"][GE]


def an_unassisted_row_stays_unassisted():
    (TMP / "data" / "refine" / f"{SITE}-{DATE}.jsonl").write_text("")
    write([("cam1", 1, "car", GE, "entry", ts("07:03"), None)])
    C.post("/api/refine", json={"site": SITE, "date": DATE, "rows": [
        {"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry", "ts": ts("07:03"),
         "to": "mgv"}]})
    row = json.loads((TMP / "data" / "refine" / f"{SITE}-{DATE}.jsonl")
                     .read_text().splitlines()[-1])
    assert "src" not in row, row
    d = aggregate.counts(SITE, DATE, app.data_root(), TMP / "ingest", app.CONFIG)
    assert d["qa"]["refined_assisted"] == 0


# ---- the batch job ----

def the_batch_skips_what_it_cannot_see_and_says_so():
    real = crop_file("real.jpg")
    ph = crop_file("ph.jpg", engine.PLACEHOLDER_JPEG)
    write([("cam1", 1, "car", GE, "entry", ts("07:03"), real),
           ("cam1", 2, "car", GE, "entry", ts("07:04"), ph),
           ("cam1", 3, "car", GE, "entry", ts("07:05"), f"{SITE}/{DATE}/cam1/gone.jpg"),
           ("cam1", 4, "car", GE, "exit", ts("07:06"), real)])   # exits never proposed
    (TMP / "data" / "vlm" / f"{SITE}-{DATE}.jsonl").unlink(missing_ok=True)
    res = vlm_batch.run(SITE, DATE, app.data_root(), app.CONFIG, ask=lambda b: "lgv")
    assert res["proposed"] == 1 and res["skipped"] == 2, res
    assert res["skips"] == {"placeholder": 1, "missing": 1, "error": 0}, res["skips"]


def a_hosted_endpoint_is_refused_before_any_crop_leaves():
    ok = vlm_batch.check_local
    for url in ("http://localhost:11434", "http://127.0.0.1:11434",
                "http://169.254.67.22:11434",       # the Thunderbolt bridge
                "http://192.168.1.50:8091", "http://macbook-pro-2.local:11434"):
        assert ok(url) == url, url
    for url in ("https://api.example.com/v1", "http://8.8.8.8:80",
                "https://generativelanguage.googleapis.com"):
        try:
            ok(url)
            raise AssertionError(f"{url} passed the local-only check")
        except ValueError:
            pass


def the_class_gate_limits_what_the_batch_asks_about():
    """vlm.classes is the measured pilot gate: only detector classes that cleared the
    accuracy bar get proposals — a car crop is never asked about, even though the
    model would happily answer."""
    real = crop_file("gated.jpg")
    write([("cam1", 1, "truck", GE, "entry", ts("07:03"), real),
           ("cam1", 2, "car", GE, "entry", ts("07:04"), real)])
    (TMP / "data" / "vlm" / f"{SITE}-{DATE}.jsonl").unlink(missing_ok=True)
    cfg = {**app.CONFIG, "vlm": {"model": "t", "classes": ["truck"]}}
    res = vlm_batch.run(SITE, DATE, app.data_root(), cfg, ask=lambda b: "mgv")
    assert res["proposed"] == 1, res
    row = json.loads((TMP / "data" / "vlm" / f"{SITE}-{DATE}.jsonl").read_text())
    assert row["obj_id"] == 1, row


def the_queue_accepts_a_vlm_job_only_after_analysis():
    db = engine.connect(app.data_root(), "emptysite")
    db.close()
    r = C.post("/api/analyze/queue", json={"site": "emptysite", "date": DATE,
                                           "kind": "vlm"})
    assert r.status_code == 400 and "nothing analyzed" in r.json()["detail"], r.text
    write([("cam1", 1, "car", GE, "entry", ts("07:03"), None)])
    r = C.post("/api/analyze/queue", json={"site": SITE, "date": DATE, "kind": "vlm"})
    assert r.status_code == 200 and r.json()["kind"] == "vlm", r.text
    C.post("/api/analyze/cancel", json={"job_id": r.json()["id"]})
    r = C.post("/api/analyze/queue", json={"site": SITE, "date": DATE, "kind": "sing"})
    assert r.status_code == 400 and "unknown job kind" in r.json()["detail"], r.text


# ---- pair tie-break suggestions ----

CH = "Church Rd"


def _cal_cam2():
    line = lambda kind, name: {"kind": kind, "name": name, "compass": "", "dir": "AB",
                               "points": [[0, 100], [200, 100]]}
    calib.save_calibration(SITE, "cam2", {"image_size": [640, 480],
                                          "lines": [line("entry", CH),
                                                    line("exit", CH)]})


def _ambig():
    _cal_cam2()
    segments("cam2", ["07:00"])
    write([("cam1", 1, "car", GE, "entry", ts("07:03"), None),
           ("cam1", 2, "car", GE, "entry", ts("07:03", 2), None),
           ("cam2", 51, "car", CH, "exit", ts("07:03", 30), None),
           ("cam2", 52, "car", CH, "exit", ts("07:03", 32), None)])


def suggestions(rows):
    p = TMP / "data" / "vlmpair" / f"{SITE}-{DATE}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))


def a_suggestion_badges_a_candidate_and_binds_nothing():
    _ambig()
    ekey = {"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry", "ts": ts("07:03")}
    suggestions([{"entry": ekey,
                  "exit": {"cam": "cam2", "obj_id": 52, "line": CH, "kind": "exit",
                           "ts": ts("07:03", 32)},
                  "verdict": "same", "detail": "roof rack", "model": "m"}])
    q = (f"/api/pair/candidates/{SITE}/{DATE}?cam=cam1&obj_id=1"
         f"&line={GE.replace(' ', '%20')}&kind=entry&ts={ts('07:03')}")
    d = C.get(q).json()
    # The suggested exit sorts FIRST despite the other exit's shorter transit time.
    assert d["candidates"][0]["obj_id"] == 52, [c["obj_id"] for c in d["candidates"]]
    assert d["candidates"][0]["suggested"], d["candidates"][0]
    assert not d["candidates"][1]["suggested"], d["candidates"][1]
    # The model's cited detail is often a plate fragment: the API serves the boolean
    # and nothing else — the detail lives in the sidecar, internal like the crops.
    assert "roof rack" not in json.dumps(d), "suggestion detail leaked into the API"
    # And nothing paired: suggestions never touch the movement books.
    cnt = aggregate.counts(SITE, DATE, app.data_root(), TMP / "ingest", app.CONFIG)
    assert cnt["movements"]["tier2"] == 0 and cnt["movements"]["tier3"] == 0 \
        and cnt["movements"]["tier4"] == 0, cnt["movements"]


def a_non_same_verdict_surfaces_nothing():
    _ambig()
    ekey = {"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry", "ts": ts("07:03")}
    suggestions([{"entry": ekey,
                  "exit": {"cam": "cam2", "obj_id": 52, "line": CH, "kind": "exit",
                           "ts": ts("07:03", 32)},
                  "verdict": "unsure", "detail": "", "model": "m"}])
    q = (f"/api/pair/candidates/{SITE}/{DATE}?cam=cam1&obj_id=1"
         f"&line={GE.replace(' ', '%20')}&kind=entry&ts={ts('07:03')}")
    d = C.get(q).json()
    assert not any(c["suggested"] for c in d["candidates"]), d["candidates"]


def the_gate_hands_refused_contests_to_the_batch():
    _ambig()
    evs = aggregate.load_events(app.data_root(), SITE, DATE)
    stats = {}
    aggregate.pair(evs, aggregate.class_map(app.CONFIG), None, stats)
    got = stats["ambiguous_pairs"]
    assert len(got) >= 2, got
    assert {"cam", "obj_id", "line", "kind", "ts", "crop"} == set(got[0]["entry"]), got[0]


check("a suggestion badges a candidate and binds nothing",
      a_suggestion_badges_a_candidate_and_binds_nothing)
check("a non-same verdict surfaces nothing", a_non_same_verdict_surfaces_nothing)
check("the gate hands refused contests to the batch",
      the_gate_hands_refused_contests_to_the_batch)
check("a proposal annotates the queue and moves nothing",
      a_proposal_annotates_and_moves_nothing)
check("a proposal survives an offset edit", a_proposal_survives_an_offset_edit)
check("a regenerated obj_id proposes nothing", a_regenerated_obj_id_proposes_nothing)
check("an off-target proposal never surfaces", an_off_target_proposal_never_surfaces)
check("a confirmed proposal carries provenance", a_confirmed_proposal_carries_provenance)
check("an unassisted row stays unassisted", an_unassisted_row_stays_unassisted)
check("the batch skips what it cannot see and says so",
      the_batch_skips_what_it_cannot_see_and_says_so)
check("a hosted endpoint is refused before any crop leaves",
      a_hosted_endpoint_is_refused_before_any_crop_leaves)
check("the class gate limits what the batch asks about",
      the_class_gate_limits_what_the_batch_asks_about)
check("the queue accepts a vlm job only after analysis",
      the_queue_accepts_a_vlm_job_only_after_analysis)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

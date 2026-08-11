#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_aggregate.py — exits non-zero on failure."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-agg-")).resolve()
os.environ["COUNTKIT_ROOT"] = str(TMP)

import aggregate                                # noqa: E402
import app                                      # noqa: E402
import calib                                    # noqa: E402
import engine                                   # noqa: E402
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
    """A local Africa/Lusaka wallclock on the survey date, as an epoch."""
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 8, 4, h, m, sec, tzinfo=CAT).timestamp()


def write(events):
    db = engine.connect(app.data_root(), SITE)
    db.execute("DELETE FROM events WHERE site=? AND date=?", (SITE, DATE))
    for cam, oid, cls, line, kind, t in events:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                   (SITE, DATE, cam, oid, cls, line, kind, t, None))
    db.commit()
    db.close()


def segments(cam, hhmms):
    d = TMP / "ingest" / DATE / SITE / cam
    d.mkdir(parents=True, exist_ok=True)
    for p in d.glob("*.mkv"):
        p.unlink()
    for hhmm in hhmms:
        h, m = hhmm.split(":")
        (d / f"20260804-{h}{m}00.mkv").write_bytes(b"")


def calibrate():
    """cam1 owns both Great East lines; cam2 owns Church Rd."""
    line = lambda kind, name: {"kind": kind, "name": name, "compass": "", "dir": "AB",
                               "points": [[0, 100], [200, 100]]}
    calib.save_calibration(SITE, "cam1", {"image_size": [640, 480],
                                          "lines": [line("entry", GE), line("exit", GE)]})
    calib.save_calibration(SITE, "cam2", {"image_size": [640, 480],
                                          "lines": [line("entry", CH), line("exit", CH)]})


def run(config=None):
    return aggregate.counts(SITE, DATE, app.data_root(), TMP / "ingest",
                            config or app.CONFIG)


# ---- acceptance #3: a China-time camera bins with a synced one ----

def offset_bins_together():
    # Both vehicles pass at 07:03 CAT. cam1's clock is right; cam3 is six hours behind,
    # so the engine already added -21600 to its raw stamps. Both must land in 07:00.
    write([("cam1", 1, "car", GE, "entry", ts("07:03")),
           ("cam3", 9, "car", GE, "entry", ts("07:03", 30))])
    r = run()
    hits = [b for b in r["bins"] if b["total"]]
    assert len(hits) == 1, [b["start"] for b in hits]
    assert hits[0]["start"].startswith("2026-08-04T07:00"), hits[0]["start"]
    assert hits[0]["arms"][GE]["total"] == 2, hits[0]
    # And the raw (uncorrected) China stamp would have binned six hours out.
    assert aggregate.bin_start(ts("07:03") + 21600) != aggregate.bin_start(ts("07:03"))


# ---- acceptance #4: two-tier pairing on a 2-camera site ----

def two_tier_pairing():
    write([
        # tier 1: cam1 sees obj 1 enter GE and exit CH itself, 40 s apart
        ("cam1", 1, "car", GE, "entry", ts("08:00")),
        ("cam1", 1, "car", CH, "exit", ts("08:00", 40)),
        # tier 2: enters GE on cam1, leaves CH on cam2 30 s later — different cams,
        # same reported class, different arms
        ("cam1", 2, "car", GE, "entry", ts("08:10")),
        ("cam2", 55, "car", CH, "exit", ts("08:10", 30)),
        # 121 s apart: outside the window, must NOT pair
        ("cam1", 3, "car", GE, "entry", ts("08:20")),
        ("cam2", 56, "car", CH, "exit", ts("08:22", 1)),
        # same arm across cameras: an indistinguishable U-turn, must NOT pair
        ("cam1", 4, "car", GE, "entry", ts("08:30")),
        ("cam2", 57, "car", GE, "exit", ts("08:30", 20)),
    ])
    r = run()
    m = r["movements"]
    assert m["tier1"] == 1 and m["tier2"] == 1, m
    assert m["unpaired"] == 4, m          # two entries and two exits left over
    od = {(c["entry"], c["exit"]): c for c in m["od"]}
    assert od[(GE, CH)]["count"] == 2 and od[(GE, CH)]["tier2_count"] == 1, od
    assert r["qa"]["unpaired_per_cam"] == {"cam1": {"entry": 2, "exit": 0},
                                           "cam2": {"entry": 0, "exit": 2}}, r["qa"]


def greedy_takes_the_closest():
    # One leftover entry, two candidate exits on the other camera: 60 s and 20 s away.
    # Greedy by Δt must take the 20 s one.
    write([("cam1", 1, "car", GE, "entry", ts("09:00")),
           ("cam2", 50, "car", CH, "exit", ts("09:01")),
           ("cam2", 51, "car", CH, "exit", ts("09:00", 20))])
    m = run()["movements"]
    assert m["tier2"] == 1, m
    db_moves = aggregate.pair(aggregate.load_events(app.data_root(), SITE, DATE),
                              aggregate.class_map(app.CONFIG))[0]
    assert db_moves[0]["dt"] == 20.0, db_moves[0]


def class_must_match_for_tier2():
    write([("cam1", 1, "bus", GE, "entry", ts("09:30")),
           ("cam2", 60, "car", CH, "exit", ts("09:30", 20))])
    assert run()["movements"]["tier2"] == 0, "tier 2 paired across different classes"


# ---- pairing-rate maths at the flag boundaries ----

def pairing_thresholds():
    def rate_for(paired, total):
        evs, moves = [], []
        for i in range(total):
            evs.append({"kind": "entry", "cam": "cam1", "i": i})
        for i in range(paired):
            moves.append({"tier": 1, "entry_cam": "cam1"})
        return aggregate.pairing_qa(evs, moves)

    assert rate_for(17, 20)["rate"] == 85.0, "17/20 is exactly 85%"
    assert rate_for(17, 20)["state"] == "ok", "85.0 is the pass mark, not a failure"
    assert rate_for(849, 1000)["rate"] == 84.9
    assert rate_for(849, 1000)["state"] == "bad", "84.9 must flag the site"
    # Heavy cross-camera inference warns even when the combined rate is fine.
    evs = [{"kind": "entry", "cam": "cam1", "i": i} for i in range(10)]
    moves = [{"tier": 1, "entry_cam": "cam1"}] * 6 + [{"tier": 2, "entry_cam": "cam1"}] * 4
    q = aggregate.pairing_qa(evs, moves)
    assert q["rate"] == 100.0 and q["inferred_share"] == 40.0 and q["state"] == "warn", q


# ---- PHF, hand-computed ----

def phf_hand_computed():
    # 07:00-08:00 gets 40, 30, 20, 10 = 100 in the hour, busiest quarter 40.
    # PHF = 100 / (4 x 40) = 0.625 -> 0.62 (banker's rounding on .625).
    evs = []
    for i, (hhmm, n) in enumerate([("07:00", 40), ("07:15", 30), ("07:30", 20),
                                   ("07:45", 10)]):
        evs += [("cam1", 1000 + i * 100 + k, "car", GE, "entry", ts(hhmm, k))
                for k in range(n)]
    write(evs)
    segments("cam1", ["07:00", "07:10", "07:20", "07:30", "07:40", "07:50"])
    am = run()["peaks"]["am"]
    assert am["total"] == 100, am
    assert am["start"].startswith("2026-08-04T07:00"), am
    assert am["phf"] == round(100 / (4 * 40), 2), am
    assert run()["peaks"]["pm"] is None, "no afternoon traffic, no PM peak"


# ---- coverage gaps ----

def gap_bins_are_marked():
    # engine.segment_epoch reads segment filenames in the HOST timezone, so coverage
    # only lines up with CAT event stamps on a CAT host (the Jetson is one). Until that
    # is fixed in engine.py, say so plainly rather than failing as arithmetic.
    assert engine.segment_epoch("20260804-070000.mkv") == ts("07:00"), (
        "host is not Africa/Lusaka: engine.segment_epoch parses segment names in the "
        "host timezone, so coverage windows shift. Run with TZ=Africa/Lusaka.")
    # cam1 records 07:00-07:20 then stops until 08:00. The 07:30 bin is inside the gap
    # and must be marked, not rendered as a zero.
    segments("cam1", ["07:00", "07:10", "08:00", "08:10"])
    write([("cam1", 1, "car", GE, "entry", ts("07:05"))])
    r = run()
    by = {b["start"][11:16]: b["arms"][GE] for b in r["bins"]}
    assert by["07:00"]["gap"] is False and by["07:00"]["total"] == 1, by["07:00"]
    assert by["07:30"]["gap"] is True and by["07:30"]["total"] == 0, by["07:30"]
    assert by["08:00"]["gap"] is False, by["08:00"]
    cov = r["qa"]["coverage"]
    assert cov["per_cam"]["cam1"]["gaps"] == [[ts("07:20"), ts("08:00")]], cov
    assert cov["per_cam"]["cam1"]["hours"] == round(40 / 60, 2), cov
    # Window spans every camera: 07:00 to cam1's last segment ending 08:20 = 1h20.
    assert cov["expected_hours"] == round(80 / 60, 2), cov
    assert cov["per_cam"]["cam2"]["hours"] == round(20 / 60, 2), "cam2 stopped at 07:20"


# ---- class mapping and PCU ----

def unknown_class_and_pcu():
    write([("cam1", 1, "car", GE, "entry", ts("07:05")),
           ("cam1", 2, "oxcart", GE, "entry", ts("07:06")),
           ("cam1", 3, "bus", GE, "entry", ts("07:07"))])
    r = run()
    # Report classes come from the config table, not from code — name them through it,
    # or re-pointing the taxonomy (TrafficCamNet -> a TOR fine-tune) reds this test.
    cmap = aggregate.class_map(app.CONFIG)
    cls = [b for b in r["bins"] if b["total"]][0]["arms"][GE]["classes"]
    assert cls == {cmap["car"]: 1, cmap["bus"]: 1, "other": 1}, cls
    assert r["qa"]["unmapped"] == {"oxcart": 1}, r["qa"]["unmapped"]
    # 2 cars at the default 1.0 + 1 bus at 2.5: a class with no factor is one car's worth.
    assert aggregate.apply_pcu({"car": 2, "bus": 1}, {"bus": 2.5}) == 4.5


# ---- probe join ----

def probe_joins_and_absent_is_null():
    write([("cam1", 1, "car", GE, "entry", ts("07:05"))])
    segments("cam1", ["07:00", "07:10"])
    assert run()["qa"]["probe"] is None, "no dataset configured -> no probe anywhere"

    csv_path = TMP / "probe.csv"
    csv_path.write_text(
        "site,arm,bin_start_iso,delay_s,speed_kmh,sample_n\n"
        f"{SITE},{GE},2026-08-04T07:00:00+02:00,42.5,18.3,14\n"
        f"{SITE},{GE},2026-08-04T07:15:00+02:00,50.0,15.0,4\n"
        "othersite,Elsewhere Rd,2026-08-04T07:00:00+02:00,1,1,99\n"
        f"{SITE},{GE},bad-timestamp,1,1,1\n")
    cfg = {**app.CONFIG, "probe": {"provider": "tomtom", "dataset": str(csv_path)}}
    p = aggregate.counts(SITE, DATE, app.data_root(), TMP / "ingest", cfg)["qa"]["probe"]
    assert p["provider"] == "tomtom", p
    assert len(p["cells"]) == 2, "another site's rows and a malformed row must be dropped"
    assert p["cells"][0]["delay_s"] == 42.5 and p["cells"][0]["sample_n"] == 14, p["cells"]
    assert p["cells"][0]["bin_start"].startswith("2026-08-04T07:00"), p["cells"][0]
    assert p["median_n"] == 9, p          # median of 14 and 4
    assert p["bins_pct"] == 100.0, p      # both survey bins carry probe data


# ---- offsets round trip ----

def offsets_round_trip():
    man = TMP / "ingest" / DATE / SITE / "manifest.json"
    man.write_text(json.dumps({"sha256": {"a.mkv": "deadbeef"}, "durations": {"a.mkv": 600},
                               "time_offset_s": {"cam1": 0.0}}))
    r = C.post("/api/offsets", json={"site": SITE, "date": DATE, "cam": "cam2",
                                     "offset_s": -21600})
    assert r.status_code == 200 and r.json() == {"cam1": 0.0, "cam2": -21600.0}, r.json()
    doc = json.loads(man.read_text())
    assert doc["sha256"] == {"a.mkv": "deadbeef"}, "other manifest keys were clobbered"
    assert doc["durations"] == {"a.mkv": 600}, doc

    got = C.get(f"/api/offsets/{SITE}/{DATE}").json()
    assert got["cam1"] == 0.0 and got["cam2"] == -21600.0, got

    # null unsets — and unset is what BLOCKS analysis, so it must really disappear.
    C.post("/api/offsets", json={"site": SITE, "date": DATE, "cam": "cam2",
                                 "offset_s": None})
    assert "cam2" not in json.loads(man.read_text())["time_offset_s"]
    assert C.get(f"/api/offsets/{SITE}/{DATE}").json()["cam2"] is None
    assert json.loads(man.read_text())["sha256"], "unset clobbered the rest of the manifest"
    assert C.post("/api/offsets", json={"site": SITE, "date": DATE, "cam": "c",
                                        "offset_s": "soon"}).status_code == 400


# ---- tier 1 pairs every entry, not just the first ----

def tier1_walks_every_entry():
    # One tracker id, two round trips through the junction. Taking the LAST exit in the
    # window would time the first movement wrong and strand the second forever.
    write([("cam1", 7, "car", GE, "entry", ts("10:00")),
           ("cam1", 7, "car", CH, "exit", ts("10:00", 8)),
           ("cam1", 7, "car", CH, "exit", ts("10:01", 40)),
           ("cam1", 7, "car", GE, "entry", ts("10:01", 32))])
    moves, leftover = aggregate.pair(aggregate.load_events(app.data_root(), SITE, DATE),
                                     aggregate.class_map(app.CONFIG))
    assert len(moves) == 2 and all(m["tier"] == 1 for m in moves), moves
    assert sorted(m["dt"] for m in moves) == [8.0, 8.0], [m["dt"] for m in moves]
    assert not leftover, leftover
    r = run()["movements"]
    assert r["tier1"] == 2 and r["unpaired"] == 0, r


# ---- coverage trusts the footage, not the nominal segment length ----

def a_truncated_segment_is_not_ten_minutes():
    """A segment cut short by a power failure claims up to ten minutes of footage it
    does not have — and a real gap then renders as a hard zero."""
    d = TMP / "ingest" / DATE / SITE / "cam1"
    segments("cam1", ["07:00"])
    made = subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                           "color=c=black:s=64x64:d=3:r=5", "-c:v", "libx264",
                           str(d / "20260804-070000.mkv")], capture_output=True)
    assert made.returncode == 0, made.stderr[-400:]
    cov = aggregate.coverage(TMP / "ingest", DATE, SITE, app.data_root())
    s, e = cov["per_cam"]["cam1"]["recorded"][0]
    assert 2.5 < e - s < 3.5, cov["per_cam"]["cam1"]      # three seconds, not six hundred
    assert cov["durations_estimated"] is True, "cam2's empty stubs still fall back to 600"
    cache = json.loads((app.data_root() / "segdur" / f"{SITE}-{DATE}.json").read_text())
    assert any(k.startswith("cam1/20260804-070000.mkv:") and abs(v - 3) < .5
               for k, v in cache.items()), cache
    # Probed once: a second call answers from the cache even with ffprobe unusable.
    real, subprocess.run = subprocess.run, lambda *a, **k: (_ for _ in ()).throw(OSError())
    try:
        again = aggregate.coverage(TMP / "ingest", DATE, SITE, app.data_root())
    finally:
        subprocess.run = real
    assert again["per_cam"]["cam1"]["recorded"] == cov["per_cam"]["cam1"]["recorded"]
    segments("cam1", ["07:00", "07:10"])


# ---- the bin list is contiguous ----

def bins_are_contiguous():
    # No ingest tree for cam4, so the events alone decide the span: 07:05 and 08:05 with
    # nothing between. Non-adjacent bins would let _peak call four scattered ones an hour.
    segments("cam1", [])
    segments("cam2", [])
    write([("cam1", 1, "car", GE, "entry", ts("07:05")),
           ("cam1", 2, "car", GE, "entry", ts("08:05"))])
    starts = [b["ts"] for b in run()["bins"]]
    assert starts == list(range(starts[0], starts[-1] + 1, aggregate.BIN_S)), starts
    assert len(starts) == 5, [b["start"] for b in run()["bins"]]   # 07:00 .. 08:00
    segments("cam1", ["07:00", "07:10"])
    segments("cam2", ["07:00", "07:10"])


# ---- QA names what is missing ----

def qa_names_unset_offsets_and_uncalibrated_cams():
    cam3 = TMP / "ingest" / DATE / SITE / "cam3"
    cam3.mkdir(parents=True, exist_ok=True)
    (cam3 / "20260804-070000.mkv").write_bytes(b"")
    aggregate.set_offset(TMP / "ingest", DATE, SITE, "cam1", 0.0)
    aggregate.set_offset(TMP / "ingest", DATE, SITE, "cam2", None)
    try:
        qa = run()["qa"]
        # Keyed on the cameras that have footage: an unset offset must be able to show
        # up as None, or the UI's "offsets all set" can never fail.
        assert qa["offsets"] == {"cam1": 0.0, "cam2": None, "cam3": None}, qa["offsets"]
        # cam3 has footage and no calibration, so it silently skips analysis and its arm
        # vanishes from every table — say so rather than let the junction look smaller.
        assert qa["uncalibrated"] == ["cam3"], qa["uncalibrated"]
    finally:
        shutil.rmtree(cam3)
        aggregate.set_offset(TMP / "ingest", DATE, SITE, "cam1", 0.0)
    assert run()["qa"]["uncalibrated"] == [], "all cameras calibrated -> empty list"


# ---- arm naming ----

def probe_cells_use_the_display_arm():
    write([("cam1", 1, "car", GE, "entry", ts("07:05"))])
    csv_path = TMP / "probe-case.csv"
    csv_path.write_text("site,arm,bin_start_iso,delay_s,speed_kmh,sample_n\n"
                        f"{SITE},{GE.upper()},2026-08-04T07:00:00+02:00,42.5,18.3,14\n"
                        f"{SITE},Nowhere Rd,2026-08-04T07:00:00+02:00,1,1,1\n")
    cfg = {**app.CONFIG, "probe": {"provider": "tomtom", "dataset": str(csv_path)}}
    r = aggregate.counts(SITE, DATE, app.data_root(), TMP / "ingest", cfg)
    arms = {c["arm"] for c in r["qa"]["probe"]["cells"]}
    # The overlay matches probe cells to d.arms by string: a lowercased key never matches.
    assert GE in arms, (arms, r["arms"])
    assert "nowhere rd" in arms, "an arm the junction does not have keeps the raw key"


def an_unstripped_arm_name_does_not_fork_a_column():
    write([("cam1", 1, "car", GE, "entry", ts("07:05")),
           ("cam1", 2, "car", GE + " ", "entry", ts("07:06"))])
    r = run()
    assert r["arms"].count(GE) == 1 and (GE + " ") not in r["arms"], r["arms"]
    assert [b for b in r["bins"] if b["total"]][0]["arms"][GE]["total"] == 2, r["bins"]


def api_serves_counts():
    write([("cam1", 1, "car", GE, "entry", ts("07:05"))])
    r = C.get(f"/api/counts/{SITE}/{DATE}")
    assert r.status_code == 200, r.status_code
    body = r.json()
    assert json.dumps(body), "the payload must be JSON-serialisable for the UI"
    for k in ("bins", "movements", "qa", "peaks", "pcu_factors", "classes", "arms"):
        assert k in body, k


# ---- human class refinement, applied as a read-time overlay ----

def refine(rows):
    """Replace the overlay. It is append-only in the field; each check needs its own."""
    aggregate.refine_path(app.data_root(), SITE, DATE).write_text("")
    aggregate.add_refinements(app.data_root(), SITE, DATE, rows)


def refinement_overlays_the_class():
    write([("cam1", 1, "truck", GE, "entry", ts("11:00")),
           ("cam1", 1, "truck", CH, "exit", ts("11:00", 20))])
    refine([{"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
             "ts": ts("11:00"), "to": "hgv_mineral"}])
    r = run()
    cls = [b for b in r["bins"] if b["total"]][0]["arms"][GE]["classes"]
    assert cls == {"hgv_mineral": 1}, cls
    assert [m["cls"] for m in r["movements"]["od"]] == ["hgv_mineral"], r["movements"]
    assert r["qa"]["refined"] == 1, r["qa"]["refined"]
    # Nothing edited the event: the detector's own call is still there to audit.
    en = [e for e in aggregate.load_events(app.data_root(), SITE, DATE)
          if e["kind"] == "entry"][0]
    assert en["cls"] == "truck" and en["refined"] == "hgv_mineral", en


def later_row_wins_and_a_stale_row_is_not_coverage():
    write([("cam1", 1, "truck", GE, "entry", ts("11:00"))])
    refine([{"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
             "ts": ts("11:00"), "to": "lgv"},
            {"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
             "ts": ts("11:00"), "to": "hgv_non_mineral"},
            # Matches no event — what a re-analysis leaves behind.
            {"cam": "cam1", "obj_id": 99, "line": GE, "kind": "entry",
             "ts": ts("11:30"), "to": "lgv"}])
    r = run()
    assert r["classes"] == ["hgv_non_mineral"], r["classes"]
    assert r["qa"]["refined"] == 1, "a stale row must not claim review coverage"
    assert aggregate.read_refinements(app.data_root(), SITE, DATE)["count"] == 3


def a_refined_tier2_pair_needs_both_ends():
    write([("cam1", 1, "truck", GE, "entry", ts("12:00")),
           ("cam2", 70, "truck", CH, "exit", ts("12:00", 30))])
    both = [{"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
             "ts": ts("12:00"), "to": "hgv_mineral"},
            {"cam": "cam2", "obj_id": 70, "line": CH, "kind": "exit",
             "ts": ts("12:00", 30), "to": "hgv_mineral"}]
    refine(both)
    assert run()["movements"]["tier2"] == 1, "both ends refined: still one movement"
    # And this is why the UI saves the pair: tier 2 matches on class, so a half-refined
    # movement silently falls apart at the next aggregation.
    refine(both[:1])
    m = run()["movements"]
    assert m["tier2"] == 0 and m["unpaired"] == 2, m


def refine_targets_are_config_then_the_classes_table():
    assert aggregate.refine_targets({**app.CONFIG, "refine_targets": ["lgv", "mgv"]}) \
        == ["lgv", "mgv"], "an explicit list wins verbatim"
    # Default: the PCU table plus every report class, so a TOR split that lives only in
    # pcu (nothing detects hgv_mineral) is still offerable, and vice versa.
    cfg = {"pcu": {"lgv": 1.5, "hgv_mineral": 3.0},
           "classes": [{"model": "truck", "report": "goods_vehicle"}]}
    assert aggregate.refine_targets(cfg) == ["goods_vehicle", "hgv_mineral", "lgv"], cfg
    d = aggregate.refine_targets(app.CONFIG)
    assert set(app.CONFIG["pcu"]) <= set(d), d
    assert set(aggregate.class_map(app.CONFIG).values()) <= set(d), d


def api_refines_a_pair_and_lists_events():
    write([("cam1", 1, "truck", GE, "entry", ts("13:00")),
           ("cam2", 70, "car", CH, "exit", ts("13:00", 30)),
           ("cam1", 2, "car", GE, "entry", ts("13:05"))])
    refine([])
    rows = [{"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
             "ts": ts("13:00"), "to": "hgv_mineral"},
            {"cam": "cam2", "obj_id": 70, "line": CH, "kind": "exit",
             "ts": ts("13:00", 30), "to": "hgv_mineral"}]
    app.CONFIG["refine_targets"] = ["hgv_mineral", "lgv"]
    try:
        assert C.post("/api/refine", json={"site": SITE}).status_code == 400
        bad = C.post("/api/refine", json={"site": SITE, "date": DATE,
                                          "rows": rows + [{**rows[0], "to": "spaceship"}]})
        assert bad.status_code == 400, bad.status_code
        assert "hgv_mineral" in bad.json()["detail"], bad.json()
        assert aggregate.read_refinements(app.data_root(), SITE, DATE)["count"] == 0, \
            "one bad target rejects the whole batch — nothing written"

        ok = C.post("/api/refine", json={"site": SITE, "date": DATE, "rows": rows})
        assert ok.status_code == 200 and ok.json()["count"] == 2, ok.json()
        assert C.get(f"/api/refine/{SITE}/{DATE}").json()["count"] == 2

        one = C.get(f"/api/refine/events/{SITE}/{DATE}?cls=hgv_mineral").json()
        assert one["total"] == 1 and len(one["events"]) == 1, one
        e = one["events"][0]
        assert e["cls"] == "truck" and e["rc"] == "hgv_mineral" and e["refined"], e
        assert e["paired"]["cam"] == "cam2" and e["paired"]["kind"] == "exit", e

        page = C.get(f"/api/refine/events/{SITE}/{DATE}?limit=1&offset=1").json()
        assert page["total"] == 2, "total counts the filter, not the page"
        assert [x["obj_id"] for x in page["events"]] == [2], page["events"]
        assert page["events"][0]["paired"] is None, "an unpaired entry pairs to nothing"
    finally:
        app.CONFIG.pop("refine_targets", None)


calibrate()
segments("cam1", ["07:00", "07:10"])
segments("cam2", ["07:00", "07:10"])
check("acceptance #3: corrected clocks bin together", offset_bins_together)
check("acceptance #4: tier-1, tier-2, window and U-turn gating", two_tier_pairing)
check("tier 2 is greedy on the smallest transit time", greedy_takes_the_closest)
check("tier 2 refuses to pair different classes", class_must_match_for_tier2)
check("pairing rate flags at the 85% / 30% boundaries", pairing_thresholds)
check("peak hour and PHF, hand-computed", phf_hand_computed)
check("a bin inside a footage gap is marked, not zero", gap_bins_are_marked)
check("unknown class maps to other, PCU multiplies", unknown_class_and_pcu)
check("probe joins on bins; absent dataset is null", probe_joins_and_absent_is_null)
check("offset editor preserves the manifest; null unsets", offsets_round_trip)
check("tier 1 pairs every entry with its own first exit", tier1_walks_every_entry)
check("a truncated segment claims only the footage it has", a_truncated_segment_is_not_ten_minutes)
check("the bin list has no holes", bins_are_contiguous)
check("QA names unset offsets and uncalibrated cameras",
      qa_names_unset_offsets_and_uncalibrated_cams)
check("probe cells carry the display-cased arm", probe_cells_use_the_display_arm)
check("an unstripped arm name does not fork a column", an_unstripped_arm_name_does_not_fork_a_column)
check("counts route serves the UI payload", api_serves_counts)
check("a refinement overlays the class without editing the event",
      refinement_overlays_the_class)
check("the later refinement wins; a stale one is not coverage",
      later_row_wins_and_a_stale_row_is_not_coverage)
check("refining both ends keeps a tier-2 pair; one end breaks it",
      a_refined_tier2_pair_needs_both_ends)
check("refine targets: config list, else the classes table",
      refine_targets_are_config_then_the_classes_table)
check("refine API rejects an unknown target and pages the review list",
      api_refines_a_pair_and_lists_events)


def hostile_names_never_reach_the_filesystem():
    """site/date become filenames (event DB, flags, refinements, segdur cache). The
    JSON-body routes accept arbitrary strings, so a slash-bearing site must 400 at the
    name check — not append a jsonl outside data_root."""
    evil = "../../outside"
    marker = (app.data_root().parent / f"outside-2026-08-06.jsonl")
    for req in (
        lambda: C.post("/api/flag", json={"site": evil, "date": "2026-08-06", "bin": 1}),
        lambda: C.post("/api/refine", json={"site": evil, "date": "2026-08-06",
                                            "rows": [{"to": "lgv"}]}),
        lambda: C.post("/api/flag", json={"site": "gerlache", "date": "../evil"}),
    ):
        r = req()
        assert r.status_code == 400, (r.status_code, r.text)
    # Path params cannot carry a slash (the router 404s them), but a dot-only name
    # can — the name check must catch what routing lets through.
    r = C.get("/api/analyze/status/../2026-08-06")
    assert r.status_code in (400, 404), (r.status_code, r.text)
    assert not marker.exists(), "a hostile site name wrote outside data_root"


check("hostile site/date names never reach the filesystem",
      hostile_names_never_reach_the_filesystem)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

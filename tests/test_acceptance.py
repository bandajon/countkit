#!/usr/bin/env python3
"""PRD acceptance, end to end: python3 tests/test_acceptance.py

One story against the real app over HTTP: label a 2-camera site, hit the offset
gate, analyze a fixture site-day through the real job queue, review counts,
flag a pair, export the deliverable. Points needing hardware (real DeepStream,
real R2) assert their guarded seams instead. Numbers map to docs/COUNTKIT_PRD.md
§Acceptance.
"""

import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-accept-"))
os.environ["COUNTKIT_ROOT"] = str(TMP)

import app                                    # noqa: E402  (boots against TMP)
import engine                                 # noqa: E402
import offload                                # noqa: E402
from fastapi.testclient import TestClient     # noqa: E402

client = TestClient(app.app)
SITE, DATE = "accsite", "2026-08-04"
FAILS = []


def check(n, name, fn):
    try:
        fn()
        print(f"ok   #{n} {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL #{n} {name}: {type(e).__name__}: {e}")


# Gates are horizontal lines; after the Task-4 direction fix a crossing counts
# when the object ARRIVES on the side the chevron points at (dir AB = left of
# travel; for a left-to-right line that is decreasing y).
GATE = lambda name, compass, y, dir_: {  # noqa: E731
    "kind": "", "name": name, "compass": compass,
    "points": [[0, y], [200, y]], "dir": dir_}


def calib_doc(lines):
    for kind, ln in lines:
        ln["kind"] = kind
    return {"image_size": [1920, 1080], "lines": [ln for _, ln in lines]}


def seg_file(hhmmss):
    return f"20260804-{hhmmss}.mkv"


def ingest_tree():
    d = TMP / "ingest" / DATE / SITE
    # cam1 records 07:00 and 07:30 segments — the 07:10–07:30 hole is the
    # deliberate coverage gap of acceptance #5. cam2 runs on China time: its
    # segments are named six hours ahead and only the -21600 offset lands them
    # in the same clock-quarters as cam1 (acceptance #3).
    for cam, names in (("cam1", (seg_file("070000"), seg_file("073000"))),
                       ("cam2", (seg_file("130000"),))):
        (d / cam).mkdir(parents=True, exist_ok=True)
        for n in names:
            (d / cam / n).write_bytes(b"x")
    (d / ".verified").write_text("")


def fixtures():
    f = TMP / "fixtures" / SITE / DATE
    f.mkdir(parents=True, exist_ok=True)
    (f / "segments.json").write_text(json.dumps({
        "cam1": [{"file": seg_file("070000"), "duration": 600},
                 {"file": seg_file("073000"), "duration": 600}],
        "cam2": [{"file": seg_file("130000"), "duration": 600}]}))
    up = lambda t, oid, cls="car": [  # noqa: E731  two frames stepping over y=100
        {"t": t, "seg": seg_file("070000"),
         "objects": [{"id": oid, "cls": cls, "bbox": [40, 140, 20, 20],
                      "centroid": [50, 150]}]},
        {"t": t + 1, "seg": seg_file("070000"),
         "objects": [{"id": oid, "cls": cls, "bbox": [40, 40, 20, 20],
                      "centroid": [50, 50]}]}]
    down = lambda t, oid: [  # noqa: E731  steps over y=300 downward (exit, dir BA)
        {"t": t, "seg": seg_file("070000"),
         "objects": [{"id": oid, "cls": "car", "bbox": [40, 240, 20, 20],
                      "centroid": [50, 250]}]},
        {"t": t + 1, "seg": seg_file("070000"),
         "objects": [{"id": oid, "cls": "car", "bbox": [40, 340, 20, 20],
                      "centroid": [50, 350]}]}]
    # cam1: obj 1 enters Arm A then exits Arm B (tier 1, dt 30 s);
    #       obj 2 enters Arm A and is never seen leaving on cam1 (tier-2 entry).
    cam1 = up(10, 1) + down(40, 1) + up(60, 2)
    (f / "cam1.jsonl").write_text("\n".join(json.dumps(r) for r in cam1))
    # cam2 (China clock): its exit of Arm C at corrected t≈100 pairs with obj 2's
    # entry at t=60 — different camera, different arm, same class, dt 40 s < 120.
    cam2 = [{"t": r["t"], "seg": seg_file("130000"), "objects": r["objects"]}
            for r in down(100, 9)]
    (f / "cam2.jsonl").write_text("\n".join(json.dumps(r) for r in cam2))


def a1_label():
    """#1 Label both cameras via the API; the arm map judges the site."""
    r = client.post(f"/api/calib/{SITE}/cam1", json=calib_doc([
        ("entry", GATE("Arm A", "E", 100, "AB")),
        ("exit", GATE("Arm B", "W", 300, "BA"))]))
    assert r.status_code == 200, r.text
    r = client.post(f"/api/calib/{SITE}/cam2", json=calib_doc([
        ("entry", GATE("Arm B", "W", 100, "AB")),
        ("exit", GATE("Arm C", "N", 300, "BA"))]))
    arms = {a["arm"]: a["state"] for a in r.json()["armmap"]["arms"]}
    assert arms["Arm A"] == "unowned", arms      # exit not owned yet — said out loud
    assert arms["Arm B"] == "owned", arms
    # v2 for cam1 proves versioning: the save must not overwrite v1.
    r = client.post(f"/api/calib/{SITE}/cam1", json=calib_doc([
        ("entry", GATE("Arm A", "E", 100, "AB")),
        ("exit", GATE("Arm B", "W", 300, "BA"))]))
    assert r.json()["version"] == 2
    vs = client.get(f"/api/calib/{SITE}/cam1/versions").json()
    assert [v["version"] for v in vs] == [1, 2], vs


def a6_blocked():
    """#6 An unset offset blocks analysis with the exact copy."""
    r = client.post("/api/analyze/queue", json={"site": SITE, "date": DATE})
    assert r.status_code == 400, r.text
    assert "time offset unset — set it in Counts → Offsets. Unset is not zero." \
        in r.json()["detail"], r.json()


def a3_offsets():
    """#3 setup: the offset editor writes the manifest; zero is explicit."""
    for cam, off in (("cam1", 0.0), ("cam2", -21600.0)):
        r = client.post("/api/offsets", json={"site": SITE, "date": DATE,
                                              "cam": cam, "offset_s": off})
        assert r.status_code == 200, r.text
    assert client.get(f"/api/offsets/{SITE}/{DATE}").json() == \
        {"cam1": 0.0, "cam2": -21600.0}


def a2_analyze():
    """#2 A queued site-day runs through the real queue; events + crops land."""
    r = client.post("/api/analyze/queue", json={"site": SITE, "date": DATE})
    assert r.status_code == 200, r.text
    for _ in range(120):
        jobs = client.get("/api/analyze/jobs").json()
        j = next(x for x in jobs if x["site"] == SITE)
        if j["state"] in ("DONE", "FAILED"):
            break
        time.sleep(0.25)
    assert j["state"] == "DONE", j
    db = engine.connect(app.data_root(), SITE)
    n = db.execute("SELECT COUNT(*) FROM events WHERE site=? AND date=?",
                   (SITE, DATE)).fetchone()[0]
    db.close()
    assert n == 4, f"expected 4 crossing events, got {n}"   # 2 entries + 2 exits
    crops = list((app.data_root() / "crops" / SITE / DATE).rglob("*.jpg"))
    assert len(crops) == 4, crops
    # Re-run is idempotent: same row count, no dupes (acceptance #2 second half).
    client.post("/api/analyze/queue", json={"site": SITE, "date": DATE})
    for _ in range(120):
        jobs = [x for x in client.get("/api/analyze/jobs").json()
                if x["site"] == SITE]
        if all(x["state"] in ("DONE", "FAILED") for x in jobs):
            break
        time.sleep(0.25)
    db = engine.connect(app.data_root(), SITE)
    n2 = db.execute("SELECT COUNT(*) FROM events WHERE site=? AND date=?",
                    (SITE, DATE)).fetchone()[0]
    db.close()
    assert n2 == n, (n, n2)


def a34_counts():
    """#3 + #4 Counts: cross-clock binning, two-tier movements, residuals."""
    d = client.get(f"/api/counts/{SITE}/{DATE}").json()
    m = d["movements"]
    assert m["tier1"] == 1 and m["tier2"] == 1, m
    od = {(c["entry"], c["exit"]): c for c in m["od"]}
    assert od[("Arm A", "Arm B")]["count"] == 1
    assert od[("Arm A", "Arm C")] == {**od[("Arm A", "Arm C")], "count": 1,
                                      "tier2_count": 1}
    # cam2's China-clock exit landed in the 07:0x bins only because its -21600
    # offset was applied — a same-morning bin, not a 13:00 one.
    assert all(b["start"].split("T")[1] < "08:00" for b in d["bins"]
               if b["total"]), [b["start"] for b in d["bins"]]
    p = d["qa"]["pairing"]
    assert p["entries"] == 2 and p["paired"] == 2 and p["rate"] == 100.0, p
    assert p["inferred_share"] == 50.0, p


def a5_gap_and_flag():
    """#5 The artificial coverage gap flags Arm A's bins; a flag moves only QA."""
    d = client.get(f"/api/counts/{SITE}/{DATE}").json()
    gap_bins = [b["start"] for b in d["bins"] if b["arms"]["Arm A"]["gap"]]
    assert any("T07:15" in s for s in gap_bins), gap_bins
    # The offset-corrected window ends when the last camera stops (07:40) — a
    # China-clock camera must not stretch it into the afternoon.
    assert all(s.split("T")[1] < "08:00" for s in gap_bins), gap_bins
    mv = client.get(f"/api/movements/{SITE}/{DATE}").json()
    assert mv["total"] == 2 and mv["movements"][0]["tier"] == 2, mv
    before = d["qa"]["pairing"]["flagged"]
    m = mv["movements"][0]
    client.post("/api/flag", json={"site": SITE, "date": DATE, "entry": m["entry"],
                                   "exit": m["exit"], "bin": m["bin"],
                                   "entry_ts": m["entry_ts"]})
    after = client.get(f"/api/counts/{SITE}/{DATE}").json()
    assert after["qa"]["pairing"]["flagged"] == before + 1
    assert after["movements"]["od"] == d["movements"]["od"], \
        "a flag changed a count — the warranty rule is broken"


def a7_report():
    """#7 The bundle exports, hashes verify, files are traversal-guarded."""
    r = client.post(f"/api/report/{SITE}/{DATE}")
    assert r.status_code == 200, r.text
    files = r.json()["files"]
    d = app.data_root() / "reports" / SITE / DATE
    for name, meta in files.items():
        raw = (d / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == meta["sha256"], name
    xlsx = next(n for n in files if n.endswith(".xlsx"))
    pdf = next(n for n in files if n.endswith(".pdf"))
    assert (d / xlsx).read_bytes()[:2] == b"PK"
    assert (d / pdf).read_bytes()[:5] == b"%PDF-"
    assert client.get(f"/api/report/file/{SITE}/{DATE}/{xlsx}").status_code == 200
    assert client.get(f"/api/report/file/{SITE}/{DATE}/evil.txt").status_code == 404


def a9_probe():
    """#9 Probe UI data appears only when a dataset is configured."""
    assert client.get(f"/api/counts/{SITE}/{DATE}").json()["qa"]["probe"] is None
    csv = TMP / "data" / "probe.csv"
    csv.write_text("site,arm,bin_start_iso,delay_s,speed_kmh,sample_n\n"
                   f"{SITE},Arm A,2026-08-04T07:00:00+02:00,42.5,18.3,14\n")
    text = (TMP / "config.yaml").read_text().replace(
        'provider: ""', 'provider: "tomtom"').replace(
        'dataset: ""', f'dataset: "{csv}"')
    assert client.post("/api/config", json={"text": text}).json()["ok"]
    pr = client.get(f"/api/counts/{SITE}/{DATE}").json()["qa"]["probe"]
    assert pr and pr["median_n"] == 14 and pr["provider"] == "tomtom", pr


def a_corridor():
    """Corridor glance: unconfigured is clean, and nothing is ever persisted."""
    before = sorted(str(p) for p in (TMP / "data").rglob("*") if p.is_file())
    assert client.get("/api/corridor").json() == {"configured": False}
    after = sorted(str(p) for p in (TMP / "data").rglob("*") if p.is_file())
    assert before == after, "a corridor call wrote to disk"


def a8_10_seams():
    """#8 + #10 Hardware seams: R2 offload idle by default; the DeepStream runner
    and nvinfer_config key exist so the 5080 rig is a config change, not a port."""
    o = offload.Offload({}, TMP / "data")
    o.sweep()
    assert o.info()["enabled"] is False and not o.last_error, o.info()
    import deepstream_runner
    assert hasattr(deepstream_runner, "factory")
    assert "nvinfer_config" in (ROOT / "config.example.yaml").read_text()


ingest_tree()
fixtures()
check(1, "label two cameras; arm map judges the site", a1_label)
check(6, "unset offset blocks with the exact copy", a6_blocked)
check(3, "offset editor writes the manifest", a3_offsets)
check(2, "analyze end-to-end; events + crops; re-run idempotent", a2_analyze)
check(3, "China-clock camera bins with the synced one", a34_counts)
check(5, "coverage gap flags bins; flags never move a count", a5_gap_and_flag)
check(7, "report bundle exports and verifies", a7_report)
check(9, "probe appears only when configured", a9_probe)
check(0, "corridor glance persists nothing", a_corridor)
check(8, "hardware seams guarded (R2, DeepStream, 5080)", a8_10_seams)

print(f"\n{len(FAILS)} failed" if FAILS else "\nacceptance: all points pass")
sys.exit(1 if FAILS else 0)

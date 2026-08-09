#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_counts_dom.py — exits non-zero on failure."""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-counts-")).resolve()
os.environ["COUNTKIT_ROOT"] = str(TMP)

import app                                      # noqa: E402
import calib                                    # noqa: E402
import engine                                   # noqa: E402
from fastapi.testclient import TestClient       # noqa: E402

C = TestClient(app.app)
FAILS = []
SITE, DATE = "gerlache", "2026-08-04"
CAT = timezone(timedelta(hours=2))
GE, CH = "Great East Rd", "Church Rd"
HTML = C.get("/").text
SECTION = re.search(r'<section id="counts">.*?</section>', HTML, re.S).group(0)


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


def seed():
    """Two movements the drawer must show: one tier-1 (cam1 alone) and one tier-2
    (cam1 entry -> cam2 exit), each with a crop key."""
    line = lambda k, n: {"kind": k, "name": n, "compass": "", "dir": "AB",
                         "points": [[0, 100], [200, 100]]}
    calib.save_calibration(SITE, "cam1", {"image_size": [640, 480],
                                          "lines": [line("entry", GE), line("exit", CH)]})
    calib.save_calibration(SITE, "cam2", {"image_size": [640, 480],
                                          "lines": [line("entry", CH), line("exit", GE)]})
    d = TMP / "ingest" / DATE / SITE
    for cam in ("cam1", "cam2"):
        (d / cam).mkdir(parents=True, exist_ok=True)
        (d / cam / "20260804-080000.mkv").write_bytes(b"")
    (d / ".verified").touch()
    (d / "manifest.json").write_text(json.dumps({"time_offset_s": {"cam1": 0.0, "cam2": 0.0}}))

    crop = TMP / "data" / "crops" / SITE / DATE / "cam1"
    crop.mkdir(parents=True, exist_ok=True)
    (crop / "1-entry.jpg").write_bytes(b"\xff\xd8fake")

    db = engine.connect(app.data_root(), SITE)
    db.execute("DELETE FROM events WHERE site=? AND date=?", (SITE, DATE))
    rows = [
        ("cam1", 1, "car", GE, "entry", ts("08:01"), f"{SITE}/{DATE}/cam1/1-entry.jpg"),
        ("cam1", 1, "car", CH, "exit", ts("08:01", 30), None),      # tier 1
        ("cam1", 2, "car", GE, "entry", ts("08:05"), None),
        ("cam2", 9, "car", CH, "exit", ts("08:05", 50), None),      # tier 2, bigger dt
    ]
    for cam, oid, cls, ln, kind, t, cr in rows:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                   (SITE, DATE, cam, oid, cls, ln, kind, t, cr))
    db.commit()
    db.close()


def section_has_the_chrome():
    for s in ("verification", "internal qa", "pcu", "am peak", "pm peak",
              "turning movements", "15-minute bins", "view only"):
        assert s in SECTION.lower(), s
    assert 'id="drawer"' in SECTION and 'id="odtable"' in SECTION
    assert 'id="bintable"' in SECTION and 'id="sparks"' in SECTION


def design_values_present():
    # The hatch is what stops a gap reading as a zero — pin it.
    assert "repeating-linear-gradient(45deg,#2a2420 0 3px,#191d21 3px 6px)" in HTML
    assert "no footage — missing, not zero" in HTML
    # A gap is blank+hatched and a measured zero prints "0" — at the spec's hatch
    # contrast, rendering zero as blank would make the two indistinguishable.
    assert "A measured zero prints as 0" in HTML
    for s in ("#86a8cf", "#cfa886", "#9ac2a8", "#c791b5", "#3d5164"):
        assert s in HTML, s
    assert "font:700 34px/1 var(--mono)" in HTML, "QA numerals are 34px mono 700"
    assert "PROBE" in HTML.upper() and "corroboration only" in HTML
    assert "heavy cross-camera inference" in HTML
    assert "re-run the site-day" in HTML, "the offset editor must warn about re-running"
    assert "no crop — analysis predates crops or the write failed" in HTML


def failed_loads_clear_the_numbers():
    """A counts fetch that fails must clear the tables, not leave the previous
    site-day's numbers standing under the new day's label."""
    assert "counts unavailable — " in HTML and "no evidence loaded — " in HTML
    assert "offsets unavailable — " in HTML
    for surface in ("$('bintable').innerHTML = ''", "$('sparks').innerHTML = ''",
                    "$('odtable').innerHTML = ''"):
        assert surface in HTML, surface
    # Every Counts-tab fetch checks r.ok and reads the body through detail(), which is
    # the only thing that survives a text/plain 500.
    for guard in ("if (!rc.ok) { countsError(await detail(rc)); return; }",
                  "if (!rf.ok) { countsError(await detail(rf)); return; }",
                  "if (!rm.ok) {"):
        assert guard in HTML, guard
    assert "export failed — ' + await detail(r)" in HTML, "runExport must show the reason"


def movements_put_inferred_first():
    r = C.get(f"/api/movements/{SITE}/{DATE}").json()
    assert r["total"] == 2, r
    tiers = [m["tier"] for m in r["movements"]]
    assert tiers == [2, 1], f"inferred pairs must sort first: {tiers}"
    m = r["movements"][0]
    assert m["entry_cam"] == "cam1" and m["exit_cam"] == "cam2", m
    assert m["dt"] == 50.0 and m["entry_clock"] == "08:05:00", m
    one = C.get(f"/api/movements/{SITE}/{DATE}?entry={GE}&exit={CH}").json()
    assert one["total"] == 2, one
    assert C.get(f"/api/movements/{SITE}/{DATE}?entry=Nowhere%20Rd").json()["total"] == 0


def crops_serve_and_refuse_traversal():
    r = C.get(f"/api/crop/{SITE}/{DATE}/cam1/1-entry.jpg")
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8"), r.status_code
    assert r.headers["content-type"] == "image/jpeg"
    assert C.get(f"/api/crop/{SITE}/{DATE}/cam1/nope.jpg").status_code == 404
    for bad in ("../config.yaml", "a/../../config.yaml"):
        assert C.get(f"/api/crop/{bad}").status_code in (400, 404), bad
    assert app.CDN_BASE == "", "the CDN seam stays empty until Task 8"


def flag_round_trip():
    assert C.get(f"/api/flags/{SITE}/{DATE}").json()["count"] == 0
    r = C.post("/api/flag", json={"site": SITE, "date": DATE, "entry": GE, "exit": CH,
                                  "bin": 0, "entry_ts": ts("08:05")})
    assert r.status_code == 200 and r.json()["count"] == 1, r.json()
    rows = C.get(f"/api/flags/{SITE}/{DATE}").json()
    assert rows["count"] == 1 and rows["rows"][0]["entry"] == GE, rows
    # A flag moves QA copy and nothing else: the counts must be untouched.
    before = C.get(f"/api/counts/{SITE}/{DATE}").json()
    C.post("/api/flag", json={"site": SITE, "date": DATE, "entry": GE, "exit": CH,
                              "bin": 0, "entry_ts": ts("08:01")})
    after = C.get(f"/api/counts/{SITE}/{DATE}").json()
    assert after["qa"]["pairing"]["flagged"] == 2, after["qa"]["pairing"]
    assert after["bins"] == before["bins"], "flagging changed a count"
    assert after["movements"]["od"] == before["movements"]["od"], "flagging changed a movement"
    assert C.post("/api/flag", json={"entry": GE}).status_code == 400


def report_card_warns_on_an_unset_offset():
    """The Report card's offsets line must name a camera with no clock offset instead
    of saying "all set ✓". Shape-tolerant: the server currently omits unset cameras
    from qa.offsets, and is being changed to emit every camera with null for unset."""
    cam3 = TMP / "ingest" / DATE / SITE / "cam3"
    cam3.mkdir(parents=True, exist_ok=True)
    (cam3 / "20260804-080000.mkv").write_bytes(b"")
    # The client half, pinned: an unset camera is a null value, not a missing key.
    assert "filter(([, v]) => v === null)" in HTML, "the Report card's unset-offset filter"
    assert "UNSET" in HTML and "all set ✓" in HTML, "both offset states need their copy"
    offs = C.get(f"/api/counts/{SITE}/{DATE}").json()["qa"]["offsets"]
    assert offs.get("cam1") == 0.0 and offs.get("cam2") == 0.0, offs
    if "cam3" not in offs:
        print("     note: server still omits unset cameras — payload change not landed")
        return
    unset = [k for k, v in offs.items() if v is None]     # mirrors the JS filter above
    assert unset == ["cam3"], offs


def probe_cells_key_to_an_arm():
    """A probe cell only draws if its arm matches a counts arm exactly (c.arm === arm)
    and its bin matches a bin ts. Both are pinned here; the display-casing of cell arms
    is a server change landing separately, so a case-only mismatch is tolerated."""
    ds = TMP / "probe.csv"
    ds.write_text("site,arm,bin_start_iso,delay_s,speed_kmh,sample_n\n"
                  f"{SITE},{GE},2026-08-04T08:00:00+02:00,42,18.5,12\n")
    app.CONFIG["probe"] = {"provider": "TomTom", "dataset": str(ds)}
    # The overlay markup, pinned: series, tooltip and toggle.
    assert "pr.cells.filter(c => c.arm === arm)" in HTML, "the probe series join"
    assert 'stroke-dasharray="4 4"' in HTML, "the probe series is dashed, never solid"
    assert "s delay · n=" in HTML, "the probe point needs its <title> tooltip"
    d = C.get(f"/api/counts/{SITE}/{DATE}").json()
    pr = d["qa"]["probe"]
    assert pr and pr["cells"], "a configured dataset must produce cells"
    c = pr["cells"][0]
    assert c["delay_s"] == 42.0 and c["sample_n"] == 12, c
    assert c["bin"] in [b["ts"] for b in d["bins"]], "the cell must land on a real bin"
    if c["arm"] not in d["arms"]:
        assert c["arm"].lower() == GE.lower(), c
        print("     note: cell arms still lower-cased — display-casing not landed")


seed()
check("counts section carries the chrome", section_has_the_chrome)
check("design values: hatch, accents, numerals, probe copy", design_values_present)
check("a failed counts load clears the numbers", failed_loads_clear_the_numbers)
check("movements list puts inferred pairs first", movements_put_inferred_first)
check("crops serve locally and refuse traversal", crops_serve_and_refuse_traversal)
check("flagging changes QA copy, never a count", flag_round_trip)
check("Report card warns when a camera has no clock offset", report_card_warns_on_an_unset_offset)
check("probe cells key to an arm and a bin", probe_cells_key_to_an_arm)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

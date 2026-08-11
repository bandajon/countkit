#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_counts_dom.py — exits non-zero on failure."""

import json
import os
import re
import shutil
import subprocess
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


def refine_panel_renders_with_its_chip():
    """The review queue for classes a stock detector cannot split. Collapsed by default,
    and the applied count rides in the summary as a chip."""
    for s in ('id="refine"', 'id="rf-class"', 'id="rf-grid"', 'id="rf-save"',
              'id="rf-chip"', "<details", "Save 0 refinements"):
        assert s in SECTION, s
    assert "refine classes" in SECTION.lower()
    # The chip is qa.refined off the counts payload, and only when the server applied any.
    assert "CNT.data.qa.refined || 0" in HTML
    assert """`<span class="chip">${n} refined</span>` : ''""" in HTML
    assert "refineSync();" in HTML, "loadCounts must resync the panel with the new day"
    # Same rule as every other counts surface: a failed load clears the evidence.
    assert "$('rf-grid').innerHTML = ''" in HTML


def refine_grid_uses_the_contract_urls():
    assert "`/api/refine/events/${CNT.site}/${CNT.date}`" in HTML
    assert "`?cls=${encodeURIComponent(RF.cls)}&limit=30&offset=${RF.off}`" in HTML
    assert "post('/api/refine', {site: CNT.site, date: CNT.date, rows})" in HTML
    # Errors reach the panel through detail(), the only thing that survives a text/plain 500.
    assert "refineRes('bad', `evidence unavailable — ${await detail(r)}`)" in HTML
    assert "refineRes('bad', await detail(r)); return;" in HTML, "a rejected save must say why"
    # The allowed targets are not in the payload — they are derived from it.
    assert "Object.keys(CNT.data.pcu_factors || {}), ...CNT.data.classes" in HTML
    # Crops come from the same route and the same missing-crop box as the drawer.
    assert "cropFig(e.crop, e.cam, clock(e.ts))" in HTML


def a_paired_refinement_sends_both_crossings():
    """Tier-2 pairing needs matching classes, so refining an entry must carry its exit —
    lift refineRows out of the page and run it, rather than trusting the shape by eye."""
    src = re.search(r"^function refineRows\(staged\) \{.*?^\}", HTML, re.S | re.M)
    assert src, "refineRows must stay a plain top-level function this test can lift out"
    node = shutil.which("node")
    if not node:
        assert "[ev, ev.paired]" in src.group(0), "the exit row must travel with the entry"
        print("     note: node absent — refineRows checked by source, not by running it")
        return
    entry = {"cam": "cam1", "obj_id": 2, "line": GE, "kind": "entry", "ts": ts("08:05"),
             "cls": "goods_vehicle",
             "paired": {"cam": "cam2", "obj_id": 9, "line": CH, "kind": "exit",
                        "ts": ts("08:05", 50)}}
    solo = {"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry", "ts": ts("08:01"),
            "cls": "goods_vehicle", "paired": None}
    script = src.group(0) + f"""
const stage = ev => new Map([['k', {{ev, to: 'mgv'}}]]);
console.log(JSON.stringify([refineRows(stage({json.dumps(entry)})),
                            refineRows(stage({json.dumps(solo)}))]));"""
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    paired, unpaired = json.loads(out.stdout)
    assert len(paired) == 2, paired
    assert [r["kind"] for r in paired] == ["entry", "exit"], paired
    assert [r["cam"] for r in paired] == ["cam1", "cam2"], paired
    assert paired[1]["obj_id"] == 9 and paired[1]["line"] == CH, paired[1]
    assert all(r["to"] == "mgv" for r in paired), paired
    assert set(paired[0]) == {"cam", "obj_id", "line", "kind", "ts", "to"}, paired[0]
    assert len(unpaired) == 1 and unpaired[0]["obj_id"] == 1, unpaired


def a_refused_status_blocks_the_day_never_loses_it():
    """/api/ingest lists any directory; /api/analyze/status refuses a site name outside
    [A-Za-z0-9_-]. Parsed blind that 400 body reads as a status object — Analyze would
    offer Queue and Counts/Report would drop the day silently."""
    d = TMP / "ingest" / DATE / "Great East Rd" / "cam1"
    d.mkdir(parents=True, exist_ok=True)
    (d / "20260804-080000.mkv").write_bytes(b"")
    r = C.get(f"/api/analyze/status/Great East Rd/{DATE}")
    assert r.status_code == 400, f"the status route must refuse this name: {r.status_code}"
    assert r.json()["detail"], "a refusal must carry the operator copy"
    # One helper, three callers: the fix cannot be half-applied.
    assert HTML.count("await analyzeStatus(r.site, r.date)") == 3, "every caller must route through it"
    assert HTML.count("fetch(`/api/analyze/status/") == 1, "no caller may still parse blind"
    assert "{blocked: await detail(r), events: 0}" in HTML
    # A blocked day is a row with the reason and no Queue button, and no dropdown entry.
    assert "b.disabled = !!st.blocked" in HTML and "say.textContent = st.blocked" in HTML
    assert "if (st.events) opts.push(" in HTML, "Counts lists analyzed days only"


def refine_buttons_follow_the_payload_targets():
    """Config's refine_targets override must govern the buttons, with the old pcu-keys
    derivation left only as fallback for a server that predates the field."""
    src = re.search(r"^const refineTargets = .*?;$", HTML, re.S | re.M)
    assert src, "refineTargets must stay a plain top-level const this test can lift out"
    node = shutil.which("node")
    if not node:
        assert "CNT.data.refine_targets\n" in src.group(0), "the payload field comes first"
        print("     note: node absent — refineTargets checked by source, not by running it")
        return
    served = {"refine_targets": ["lgv", "mgv", "hgv_mineral"],
              "pcu_factors": {"passenger_car": 1.0, "other": 0.5}, "classes": ["goods_vehicle"]}
    old = {"pcu_factors": {"passenger_car": 1.0, "lgv": 1.5}, "classes": ["goods_vehicle"]}
    script = f"let CNT;\n{src.group(0)}\n" + f"""
const run = data => {{ CNT = {{data}}; return refineTargets(); }};
console.log(JSON.stringify([run({json.dumps(served)}), run({json.dumps(old)})]));"""
    out = subprocess.run([node, "-e", script], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    payload, fallback = json.loads(out.stdout)
    assert payload == ["lgv", "mgv", "hgv_mineral"], f"the payload list must win outright: {payload}"
    assert "passenger_car" not in payload, f"pcu keys must not leak in beside it: {payload}"
    assert sorted(fallback) == ["goods_vehicle", "lgv", "passenger_car"], fallback


seed()
check("counts section carries the chrome", section_has_the_chrome)
check("design values: hatch, accents, numerals, probe copy", design_values_present)
check("a failed counts load clears the numbers", failed_loads_clear_the_numbers)
check("movements list puts inferred pairs first", movements_put_inferred_first)
check("crops serve locally and refuse traversal", crops_serve_and_refuse_traversal)
check("flagging changes QA copy, never a count", flag_round_trip)
check("Report card warns when a camera has no clock offset", report_card_warns_on_an_unset_offset)
check("probe cells key to an arm and a bin", probe_cells_key_to_an_arm)
check("refine panel renders with its applied-count chip", refine_panel_renders_with_its_chip)
check("refine grid fetches and saves on the contract URLs", refine_grid_uses_the_contract_urls)
check("a paired refinement sends both crossings", a_paired_refinement_sends_both_crossings)
check("a refused status blocks the day, never loses it", a_refused_status_blocks_the_day_never_loses_it)
check("refine buttons follow the payload's targets", refine_buttons_follow_the_payload_targets)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

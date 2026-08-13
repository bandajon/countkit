#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_emb.py — exits non-zero on failure.

The visual tie-break tier: a calibrated re-ID embedding may settle a contest the
ambiguity gate refused — decisively or not at all, priced as inference always."""

import base64
import json
import os
import struct
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="countkit-emb-")).resolve()
os.environ["COUNTKIT_ROOT"] = str(TMP)

import aggregate                                # noqa: E402
import app                                      # noqa: E402
import calib                                    # noqa: E402
import engine                                   # noqa: E402

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
    for cam, oid, cls, line, kind, t in events:
        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                   (SITE, DATE, cam, oid, cls, line, kind, t, None))
    db.commit()
    db.close()


def pack(vec):
    n = sum(c * c for c in vec) ** 0.5 or 1.0
    return base64.b64encode(
        struct.pack(f"<{len(vec)}e", *[c / n for c in vec])).decode()


def embs(rows, threshold=0.6, margin=0.1):
    p = TMP / "data" / "emb" / f"{SITE}-{DATE}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"meta": {"model": "t", "threshold": threshold,
                                  "margin": margin}})]
    lines += [json.dumps({"cam": c, "obj_id": o, "line": l, "kind": k, "ts": t,
                          "v": pack(v)}) for c, o, l, k, t, v in rows]
    p.write_text("".join(x + "\n" for x in lines))


def calibrate():
    line = lambda kind, name: {"kind": kind, "name": name, "compass": "", "dir": "AB",
                               "points": [[0, 100], [200, 100]]}
    calib.save_calibration(SITE, "cam1", {"image_size": [640, 480],
                                          "lines": [line("entry", GE), line("exit", GE)]})
    calib.save_calibration(SITE, "cam2", {"image_size": [640, 480],
                                          "lines": [line("entry", CH), line("exit", CH)]})


def segments(cam, hhmms):
    d = TMP / "ingest" / DATE / SITE / cam
    d.mkdir(parents=True, exist_ok=True)
    for hhmm in hhmms:
        h, m = hhmm.split(":")
        (d / f"20260804-{h}{m}00.mkv").write_bytes(b"")


def run():
    return aggregate.counts(SITE, DATE, app.data_root(), TMP / "ingest", app.CONFIG)


calibrate()
segments("cam1", ["08:00"])
segments("cam2", ["08:00"])

# Two same-class vehicles 2 s apart — the gate's canonical refusal.
AMBIG = [("cam1", 1, "car", GE, "entry", ts("08:00")),
         ("cam1", 2, "car", GE, "entry", ts("08:00", 2)),
         ("cam2", 51, "car", CH, "exit", ts("08:00", 30)),
         ("cam2", 52, "car", CH, "exit", ts("08:00", 32))]

# Orthogonal-ish vectors: A matches A', B matches B'.
A = [1.0, 0.0, 0.0, 0.1]
B = [0.0, 1.0, 0.0, 0.1]


def a_decisive_separation_pairs_as_tier4():
    write(AMBIG)
    embs([("cam1", 1, GE, "entry", ts("08:00"), A),
          ("cam1", 2, GE, "entry", ts("08:00", 2), B),
          ("cam2", 51, CH, "exit", ts("08:00", 30), A),
          ("cam2", 52, CH, "exit", ts("08:00", 32), B)])
    d = run()
    # (1,51) wins its contest visually; with those ends claimed, (2,52) has no rival
    # left and pairs as plain tier 2 — the same unlock cascade the plate tier has.
    assert d["movements"]["tier4"] >= 1, d["movements"]
    assert d["movements"]["tier4"] + d["movements"]["tier2"] == 2, d["movements"]
    pr = d["qa"]["pairing"]
    assert pr["paired"] == 2 and pr["inferred_share"] == 100.0, pr
    assert pr["emb"]["reads"] == 4 and pr["emb"]["resolved"] == 4, pr["emb"]
    assert pr["emb"]["pairs"] == d["movements"]["tier4"], pr["emb"]


def a_sub_margin_separation_still_refuses():
    write(AMBIG)
    # Everybody looks the same (white Hiaces): cosines identical, no margin cleared.
    embs([("cam1", 1, GE, "entry", ts("08:00"), A),
          ("cam1", 2, GE, "entry", ts("08:00", 2), A),
          ("cam2", 51, CH, "exit", ts("08:00", 30), A),
          ("cam2", 52, CH, "exit", ts("08:00", 32), A)])
    d = run()
    assert d["movements"]["tier4"] == 0 and d["movements"]["tier2"] == 0, d["movements"]
    assert d["qa"]["pairing"]["ambiguous"] == 2, d["qa"]["pairing"]["ambiguous"]


def a_rival_without_a_vector_holds_the_gate():
    write(AMBIG)
    # The winner's ends both have vectors; one rival end does not — it cannot be
    # ruled out visually, so nothing pairs.
    embs([("cam1", 1, GE, "entry", ts("08:00"), A),
          ("cam2", 51, CH, "exit", ts("08:00", 30), A),
          ("cam2", 52, CH, "exit", ts("08:00", 32), B)])
    d = run()
    assert d["movements"]["tier4"] == 0, d["movements"]


def below_threshold_never_pairs():
    write(AMBIG)
    embs([("cam1", 1, GE, "entry", ts("08:00"), A),
          ("cam1", 2, GE, "entry", ts("08:00", 2), B),
          ("cam2", 51, CH, "exit", ts("08:00", 30), A),
          ("cam2", 52, CH, "exit", ts("08:00", 32), B)],
         threshold=1.05)     # unreachable by construction — cosine tops out at ~1
    d = run()
    assert d["movements"]["tier4"] == 0, d["movements"]


def a_missing_or_uncalibrated_sidecar_changes_nothing():
    write(AMBIG)
    p = TMP / "data" / "emb" / f"{SITE}-{DATE}.jsonl"
    p.unlink(missing_ok=True)
    base = run()["movements"]
    assert base["tier4"] == 0 and base["tier2"] == 0, base
    # A file with no meta header binds nothing rather than guessing a threshold.
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cam": "cam1", "obj_id": 1, "line": GE, "kind": "entry",
                             "ts": ts("08:00"), "v": pack(A)}) + "\n")
    assert run()["movements"] == base, "an uncalibrated sidecar changed pairing"
    p.unlink()


def an_out_of_contest_rival_still_holds_the_gate():
    """The reviewed live bug: the anchor's contest is not closed under shares-an-end.
    Entry A (cam1/GE) and entry B (cam2/CH, 2.5 s earlier); exits X (cam3/East) and
    Y (cam4/GE). A->Y is same-arm-excluded, so the dt-sorted anchor A->X gathers only
    B->X as a rival — B->Y (2.5 s from B->X and cosine-identical to it) sat outside
    the contest and was never margin-checked, letting a coin flip pair as tier 4."""
    line = lambda kind, name: {"kind": kind, "name": name, "compass": "", "dir": "AB",
                               "points": [[0, 100], [200, 100]]}
    calib.save_calibration(SITE, "cam3", {"image_size": [640, 480],
                                          "lines": [line("exit", "East Rd")]})
    calib.save_calibration(SITE, "cam4", {"image_size": [640, 480],
                                          "lines": [line("exit", GE)]})
    segments("cam3", ["08:00"])
    segments("cam4", ["08:00"])
    write([("cam1", 1, "car", GE, "entry", ts("08:00")),           # A
           ("cam2", 2, "car", CH, "entry", ts("08:00") - 2.5),     # B
           ("cam3", 61, "car", "East Rd", "exit", ts("08:01")),    # X
           ("cam4", 62, "car", GE, "exit", ts("08:01") + 2.5)])     # Y (A->Y same-arm)
    B = [0.0, 1.0, 0.0, 0.1]
    embs([("cam1", 1, GE, "entry", ts("08:00"), [1.0, 0.0, 0.0, 0.1]),   # A: distinct
          ("cam2", 2, CH, "entry", ts("08:00") - 2.5, B),
          ("cam3", 61, "East Rd", "exit", ts("08:01"), B),          # X matches B...
          ("cam4", 62, GE, "exit", ts("08:01") + 2.5, B)])           # ...but so does Y
    d = run()
    # B->X vs B->Y is cosine-identical: the embedding provably cannot separate them,
    # so NOTHING pairs and the contest is counted ambiguous — never a tier-4 export.
    assert d["movements"]["tier4"] == 0, d["movements"]
    assert d["qa"]["pairing"]["ambiguous"] >= 1, d["qa"]["pairing"]["ambiguous"]
    # Cleanup for the other checks.
    for c in ("cam3", "cam4"):
        import shutil
        shutil.rmtree(TMP / "ingest" / DATE / SITE / c, ignore_errors=True)


def tier4_counts_into_the_inferred_percentage():
    write(AMBIG)
    embs([("cam1", 1, GE, "entry", ts("08:00"), A),
          ("cam1", 2, GE, "entry", ts("08:00", 2), B),
          ("cam2", 51, CH, "exit", ts("08:00", 30), A),
          ("cam2", 52, CH, "exit", ts("08:00", 32), B)])
    pr = run()["qa"]["pairing"]
    # 2 entries, both paired cross-camera by machine reasoning (one tier 4, one
    # unlocked tier 2): the 'Cross-camera inferred %' figure must own BOTH — a
    # tier-4 pair excluded from it would understate machine inference in the
    # deliverable while inferred_share on the same sheet includes it.
    assert pr["inferred"] == 100.0, pr
    assert pr["inferred_share"] == 100.0, pr


def vectors_reach_no_api_payload():
    write(AMBIG)
    embs([("cam1", 1, GE, "entry", ts("08:00"), A)])
    d = run()
    s = json.dumps(d)
    assert '"emb"' not in json.dumps(d["bins"]) and "emb_thr" not in s, \
        "embedding internals leaked into the counts payload"


check("a decisive separation pairs as tier 4", a_decisive_separation_pairs_as_tier4)
check("a sub-margin separation still refuses", a_sub_margin_separation_still_refuses)
check("a rival without a vector holds the gate", a_rival_without_a_vector_holds_the_gate)
check("below threshold never pairs", below_threshold_never_pairs)
check("an out-of-contest rival still holds the gate",
      an_out_of_contest_rival_still_holds_the_gate)
check("tier 4 counts into the inferred percentage",
      tier4_counts_into_the_inferred_percentage)
check("a missing or uncalibrated sidecar changes nothing",
      a_missing_or_uncalibrated_sidecar_changes_nothing)
check("vectors reach no API payload", vectors_reach_no_api_payload)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

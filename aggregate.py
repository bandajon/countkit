#!/usr/bin/env python3
"""Aggregation: events -> 15-min bins, two-tier movements, QA, peaks, probe join.

Read-only over the events table. Nothing here may edit a count — QA flags are the
remedy for bad data, because a hand-edited number destroys the warranty argument."""

import csv
import json
import statistics
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import calib
import engine

CAT = timezone(timedelta(hours=2))     # Africa/Lusaka, fixed, no DST
BIN_S = 900                            # 15 minutes
PAIR_WINDOW = 120.0                    # max transit time entry -> exit
SEG_S = 600                            # FieldKit segment length
OK_RATE = 85.0                         # combined pairing below this flags the site
MAX_INFERRED = 30.0                    # inferred share of paired above this warns


def bin_start(ts):
    """Clock-quarter containing ts. CAT is a whole number of hours from UTC, so
    flooring the epoch to 900 s lands on :00/:15/:30/:45 local without a tz round-trip."""
    return int(ts // BIN_S * BIN_S)


def bin_iso(ts):
    return datetime.fromtimestamp(ts, CAT).isoformat(timespec="minutes")


def local_hour(ts):
    return datetime.fromtimestamp(ts, CAT).hour


def class_map(config):
    return {c["model"]: c["report"] for c in (config.get("classes") or [])
            if isinstance(c, dict) and c.get("model")}


def report_class(label, cmap):
    """An unmapped detector label is reported as 'other' and counted in QA — silently
    dropping it would shrink the totals with no trace."""
    return cmap.get(label, "other")


def load_events(data_root, site, date):
    db = engine.connect(data_root, site)
    rows = db.execute(
        "SELECT cam, obj_id, cls, line, kind, corrected_ts, crop FROM events "
        "WHERE site=? AND date=? ORDER BY corrected_ts", (site, date)).fetchall()
    db.close()
    cols = ("cam", "obj_id", "cls", "line", "kind", "ts", "crop")
    # Stripped on load: arm_map strips calibration names but the events table stores them
    # verbatim, so "North " and "North" would fork one arm into two columns.
    return [dict(zip(cols, r), line=(r[3] or "").strip()) for r in rows]


# ---------------------------------------------------------------- pairing

def _move(en, ex, tier):
    return {"entry": en["line"], "exit": ex["line"], "cls": en["rc"],
            "bin": bin_start(en["ts"]), "tier": tier, "dt": round(ex["ts"] - en["ts"], 2),
            "entry_cam": en["cam"], "exit_cam": ex["cam"],
            "entry_ts": en["ts"], "exit_ts": ex["ts"],
            "crops": [en["crop"], ex["crop"]]}


def pair(evs, cmap):
    """Tier 1: one camera saw the whole movement (same tracker id) — confident.
    Tier 2: a leftover entry matched to a leftover exit on ANOTHER camera by class and
    transit time. No visual re-identification, so tier 2 is always reported separately."""
    for i, e in enumerate(evs):
        e["i"] = i
        e["rc"] = report_class(e["cls"], cmap)
    used, moves = set(), []

    by_obj = {}
    for e in evs:
        by_obj.setdefault((e["cam"], e["obj_id"]), []).append(e)
    for grp in sorted(by_obj.values(), key=lambda g: g[0]["ts"]):
        entries = sorted([e for e in grp if e["kind"] == "entry"], key=lambda e: e["ts"])
        exits = sorted([e for e in grp if e["kind"] == "exit"], key=lambda e: e["ts"])
        # Every entry, and the FIRST free exit after it: a tracker id that comes round
        # twice is two movements, and taking the last exit in the window would both
        # mis-time the first and leave the second unpairable by construction.
        for en in entries:
            ex = next((x for x in exits if x["i"] not in used
                       and 0 < x["ts"] - en["ts"] <= PAIR_WINDOW), None)
            if ex is None:
                continue
            used |= {en["i"], ex["i"]}
            moves.append(_move(en, ex, 1))

    # ponytail: O(entries x exits) candidate scan. A site-day is thousands of events,
    # not millions; bucket by bin if a wave ever outgrows it.
    cands = []
    for en in [e for e in evs if e["kind"] == "entry" and e["i"] not in used]:
        for ex in [e for e in evs if e["kind"] == "exit" and e["i"] not in used]:
            dt = ex["ts"] - en["ts"]
            if not 0 < dt <= PAIR_WINDOW or en["cam"] == ex["cam"] or en["rc"] != ex["rc"]:
                continue
            # Same arm across two cameras is either tier 1's job or an indistinguishable
            # U-turn; either way it is not evidence of a movement.
            if en["line"].strip().lower() == ex["line"].strip().lower():
                continue
            cands.append((dt, en["i"], ex["i"], en, ex))
    for dt, i, j, en, ex in sorted(cands, key=lambda c: (c[0], c[1], c[2])):
        if i in used or j in used:
            continue
        used |= {i, j}
        moves.append(_move(en, ex, 2))

    return moves, [e for e in evs if e["i"] not in used]


def pairing_qa(evs, moves):
    entries = [e for e in evs if e["kind"] == "entry"]
    total = len(entries)
    t1 = sum(1 for m in moves if m["tier"] == 1)
    t2 = sum(1 for m in moves if m["tier"] == 2)
    pct = lambda n: round(n / total * 100, 1) if total else 0.0
    paired = t1 + t2
    inferred_share = round(t2 / paired * 100, 1) if paired else 0.0
    rate = pct(paired)
    per_cam = {}
    for cam in sorted({e["cam"] for e in entries}):
        n = sum(1 for e in entries if e["cam"] == cam)
        p = sum(1 for m in moves if m["entry_cam"] == cam)
        per_cam[cam] = {"entries": n, "paired": p,
                        "rate": round(p / n * 100, 1) if n else 0.0}
    return {"rate": rate, "same": pct(t1), "inferred": pct(t2),
            "inferred_share": inferred_share, "entries": total, "paired": paired,
            "state": "bad" if rate < OK_RATE else
                     "warn" if inferred_share > MAX_INFERRED else "ok",
            "per_cam": per_cam,
            "flagged": 0}      # the verification drawer writes flags here


# ---------------------------------------------------------------- coverage

def _merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _ffprobe_s(path):
    """True length of one segment, or None. Never raises: a missing ffprobe must cost
    a warning on the coverage bar, not the whole count."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=20)
        return float(out.stdout.strip())
    except Exception:
        return None


def _durations(data_root, site, date, segs):
    """Real segment lengths, cached per site-day. SEG_S is only the recorder's NOMINAL
    length: a final segment truncated by a power failure would otherwise claim up to ten
    minutes of footage that does not exist, and render a real gap as a hard zero.

    segs: {cam: [(name, path), ...]} -> ({(cam, name): seconds}, any_estimated)."""
    # Keyed by cam/name/size: two cameras start segments on the same wallclock second so
    # bare filenames collide, and a re-pulled segment reuses its name with new bytes.
    p = Path(data_root) / "segdur" / f"{site}-{date}.json" if data_root else None
    try:
        cache = json.loads(p.read_text())
    except (OSError, ValueError, AttributeError):
        cache = {}
    # ponytail: serial probes, ~30 ms each. A full day is a few seconds on the first
    # counts() after ingest and free thereafter; thread it if a site ever gets wide.
    out, estimated, fresh = {}, False, False
    for cam, files in segs.items():
        for name, path in files:
            k = f"{cam}/{name}:{path.stat().st_size}"
            if k not in cache:
                d = _ffprobe_s(path)
                if d is None:
                    estimated = True          # not cached: a retry may yet succeed
                    out[(cam, name)] = float(SEG_S)
                    continue
                cache[k], fresh = d, True
            out[(cam, name)] = float(cache[k])
    if fresh and p:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cache, indent=2))
        except OSError:
            pass
    return out, estimated


def coverage(ingest_root, date, site, data_root=None):
    """Recorded stretches per camera from the segment filenames, and the gaps between
    them. A gap must never render as a zero — that is the difference between 'no
    traffic' and 'no footage'."""
    # Segment filenames carry each camera's OWN clock; a China-time camera would
    # otherwise stretch the survey window six hours sideways and poison every gap.
    # Unset offsets fall back to 0 here — display-only; analysis is blocked anyway.
    offs = engine.offsets(ingest_root, date, site)
    segs = {}
    for cam in engine.site_day_cams(ingest_root, date, site):
        d = Path(ingest_root) / date / site / cam
        segs[cam] = sorted((p.name, p) for p in d.glob("*.mkv")
                           if engine.SEG_NAME.match(p.name))
    durs, estimated = _durations(data_root, site, date, segs)
    per_cam = {}
    for cam, files in segs.items():
        off = float(offs.get(cam, 0.0))
        spans = [[engine.segment_epoch(n) + off,
                  engine.segment_epoch(n) + off + durs[(cam, n)]] for n, _ in files]
        per_cam[cam] = {"recorded": _merge(spans)}
    spans = [r for c in per_cam.values() for r in c["recorded"]]
    window = [min(s for s, _ in spans), max(e for _, e in spans)] if spans else [0, 0]
    for cam, c in per_cam.items():
        gaps, at = [], window[0]
        for s, e in c["recorded"]:
            if s > at:
                gaps.append([at, s])
            at = max(at, e)
        if at < window[1]:
            gaps.append([at, window[1]])
        rec = sum(e - s for s, e in c["recorded"])
        c.update(gaps=gaps, hours=round(rec / 3600, 2),
                 gaps_iso=[[bin_iso(s), bin_iso(e)] for s, e in gaps])
    expected = round((window[1] - window[0]) / 3600, 2)
    return {"window": window, "window_iso": [bin_iso(window[0]), bin_iso(window[1])],
            "expected_hours": expected, "per_cam": per_cam,
            "durations_estimated": estimated}


def _covered(cov, cam, a, b):
    """True when the camera recorded any part of [a, b) — a bin wholly inside a gap
    is the only thing marked as missing."""
    for s, e in cov["per_cam"].get(cam, {}).get("recorded", []):
        if s < b and e > a:
            return True
    return False


# ---------------------------------------------------------------- probe

def probe_load(path, site):
    """Licensed floating-car data: corroboration only, never a second count. Absent
    dataset -> None, and no probe UI appears anywhere."""
    if not path or not Path(path).exists():
        return None
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("site") or "").strip() != site:
                continue
            try:
                ts = bin_start(datetime.fromisoformat(r["bin_start_iso"]).timestamp())
                out[(r["arm"].strip().lower(), ts)] = {
                    "delay_s": float(r.get("delay_s") or 0),
                    "speed_kmh": float(r.get("speed_kmh") or 0),
                    "sample_n": int(float(r.get("sample_n") or 0))}
            except (ValueError, KeyError):
                continue      # a malformed provider row must not sink the whole join
    return out


# ---------------------------------------------------------------- peaks

def _peak(series, bins, morning):
    """Highest four CONSECUTIVE bins in the half-day; may start at :15/:30/:45."""
    idx = [i for i, b in enumerate(bins)
           if (local_hour(b) < 12) == morning]
    best = None
    for i in idx:
        if i + 3 >= len(bins) or i + 3 not in idx:
            continue
        four = series[i:i + 4]
        tot = sum(four)
        if best is None or tot > best[0]:
            best = (tot, i, max(four))
    if not best or not best[0]:
        return None
    tot, i, busiest = best
    return {"start": bin_iso(bins[i]), "total": tot,
            "phf": round(tot / (4 * busiest), 2) if busiest else 0.0}


# ---------------------------------------------------------------- the whole thing

def counts(site, date, data_root, ingest_root, config):
    cmap = class_map(config)
    evs = load_events(data_root, site, date)
    moves, leftovers = pair(evs, cmap)
    cov = coverage(ingest_root, date, site, data_root)

    # Which camera owns each arm's entry line — that is the camera whose footage gap
    # makes an arm's bin unmeasured rather than empty.
    owner, display = {}, {}
    for a in calib.arm_map(site)["arms"]:
        if a["entry"]:
            owner[a["arm"].strip().lower()] = a["entry"][0]
            display[a["arm"].strip().lower()] = a["arm"].strip()

    entries = [e for e in evs if e["kind"] == "entry"]
    # Display-cased names only: the lowercase owner keys are lookups, and unioning
    # them in would render a phantom duplicate column for every active arm.
    arms = sorted({e["line"] for e in entries} | set(display.values()))
    starts = ([bin_start(cov["window"][0])] if cov["window"][1] else [])
    if starts:
        while starts[-1] + BIN_S < cov["window"][1]:
            starts.append(starts[-1] + BIN_S)
    for e in entries:                       # events outside the recorded window still count
        if bin_start(e["ts"]) not in starts:
            starts.append(bin_start(e["ts"]))
    starts = sorted(set(starts))
    if starts:
        # Contiguous, always: _peak sums four ADJACENT entries, so a hole here would let
        # it call four scattered bins a peak hour, and the bins table would quietly skip
        # the missing quarters instead of showing them as unmeasured.
        starts = list(range(starts[0], starts[-1] + 1, BIN_S))

    rows, totals = [], []
    for b in starts:
        row = {"ts": b, "start": bin_iso(b), "arms": {}, "total": 0}
        for arm in arms:
            hits = [e for e in entries if e["line"] == arm and bin_start(e["ts"]) == b]
            cls = {}
            for e in hits:
                cls[e["rc"]] = cls.get(e["rc"], 0) + 1
            cam = owner.get(arm.strip().lower())
            row["arms"][arm] = {
                "classes": cls, "total": len(hits),
                "gap": bool(cam) and not _covered(cov, cam, b, b + BIN_S)}
            row["total"] += len(hits)
        rows.append(row)
        totals.append(row["total"])

    od = {}
    for m in moves:
        k = (m["entry"], m["exit"], m["cls"], m["bin"])
        c = od.setdefault(k, {"entry": m["entry"], "exit": m["exit"], "cls": m["cls"],
                              "bin": m["bin"], "bin_start": bin_iso(m["bin"]),
                              "count": 0, "tier2_count": 0})
        c["count"] += 1
        if m["tier"] == 2:
            c["tier2_count"] += 1

    unmapped = {}
    for e in evs:
        if e["cls"] not in cmap:
            unmapped[e["cls"]] = unmapped.get(e["cls"], 0) + 1

    probe = probe_load((config.get("probe") or {}).get("dataset"), site)
    probe_out = None
    if probe is not None:
        # Provider arm names are folded to lower case at load; hand back the display
        # casing the rest of the payload uses, or the overlay matches nothing.
        cells = [{"arm": display.get(a, a), "bin": t, "bin_start": bin_iso(t), **v}
                 for (a, t), v in sorted(probe.items())]
        seen = [v["sample_n"] for (a, t), v in probe.items() if t in starts]
        probe_out = {"provider": (config.get("probe") or {}).get("provider") or "",
                     "cells": cells,
                     "median_n": statistics.median(seen) if seen else 0,
                     "bins_pct": round(len({t for (a, t) in probe if t in starts})
                                       / len(starts) * 100, 1) if starts else 0.0}

    qa_pairing = pairing_qa(evs, moves)
    qa_pairing["flagged"] = read_flags(data_root, site, date)["count"]

    cams = engine.site_day_cams(ingest_root, date, site)
    offs = engine.offsets(ingest_root, date, site)
    # A camera with footage but no active calibration skips analysis entirely and its arm
    # simply vanishes from every table — name it rather than let the junction look smaller.
    uncalibrated = []
    for cam in cams:
        try:
            calib.get_calibration(site, cam)
        except LookupError:
            uncalibrated.append(cam)
    return {
        "site": site, "date": date,
        "bins": rows,
        "arms": arms,
        "classes": sorted({e["rc"] for e in evs}),
        "pcu_factors": config.get("pcu") or {},
        "movements": {"od": sorted(od.values(), key=lambda c: (c["bin"], c["entry"])),
                      "tier1": sum(1 for m in moves if m["tier"] == 1),
                      "tier2": sum(1 for m in moves if m["tier"] == 2),
                      "unpaired": len(leftovers)},
        "peaks": {"am": _peak(totals, starts, True), "pm": _peak(totals, starts, False)},
        "qa": {"pairing": qa_pairing,
               "coverage": cov,
               "unmapped": unmapped,
               # Keyed on the cameras that HAVE footage, so an unset offset shows up as
               # None. Keying on the offsets themselves can never report one missing.
               "offsets": {cam: offs.get(cam) for cam in cams},
               "uncalibrated": uncalibrated,
               "unpaired_per_cam": _leftover_qa(leftovers),
               "probe": probe_out},
    }


def _leftover_qa(leftovers):
    out = {}
    for e in leftovers:
        d = out.setdefault(e["cam"], {"entry": 0, "exit": 0})
        d[e["kind"]] += 1
    return out


def movements_detail(site, date, data_root, config, entry=None, exit=None,
                     bin_ts=None, limit=30):
    """Individual movements behind an aggregated cell, for the verification drawer.
    Inferred pairs sort first: they are the ones a human most needs to eyeball."""
    evs = load_events(data_root, site, date)
    moves, _ = pair(evs, class_map(config))
    out = [m for m in moves
           if (entry is None or m["entry"] == entry)
           and (exit is None or m["exit"] == exit)
           and (bin_ts is None or m["bin"] == bin_ts)]
    out.sort(key=lambda m: (-m["tier"], -m["dt"]))
    return {"total": len(out),
            "movements": [{**m, "bin_start": bin_iso(m["bin"]),
                           "entry_time": bin_iso(m["entry_ts"])[11:],
                           "entry_clock": datetime.fromtimestamp(m["entry_ts"], CAT)
                                                  .strftime("%H:%M:%S"),
                           "exit_clock": datetime.fromtimestamp(m["exit_ts"], CAT)
                                                 .strftime("%H:%M:%S")}
                          for m in out[:limit]]}


def flags_path(data_root, site, date):
    p = Path(data_root) / "flags"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{site}-{date}.jsonl"


def add_flag(data_root, site, date, row):
    """Flags land in QA notes and never touch a count — hand-edited numbers would
    destroy the warranty argument, so the remedy is always re-running the analysis."""
    with open(flags_path(data_root, site, date), "a") as f:
        f.write(json.dumps({**row, "at": datetime.now(CAT).isoformat(timespec="seconds")})
                + "\n")


def read_flags(data_root, site, date):
    p = flags_path(data_root, site, date)
    rows = []
    if p.exists():
        rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    return {"count": len(rows), "rows": rows}


def apply_pcu(classes, factors):
    """Passenger-car units: a bus is 2.5 cars' worth of road space."""
    return round(sum(n * float(factors.get(c, 1.0)) for c, n in classes.items()), 2)


# ---------------------------------------------------------------- offsets

def set_offset(ingest_root, date, site, cam, offset_s):
    """Writes into the footage manifest so the correction travels with the footage.
    None REMOVES the key — back to unset, which is not the same as zero."""
    p = Path(ingest_root) / date / site / "manifest.json"
    try:
        doc = json.loads(p.read_text())
    except (OSError, ValueError):
        doc = {}
    if not isinstance(doc, dict):
        raise ValueError("manifest.json is not an object — refusing to overwrite it")
    offs = dict(doc.get("time_offset_s") or {})
    if offset_s is None:
        offs.pop(cam, None)
    else:
        offs[cam] = float(offset_s)
    doc["time_offset_s"] = offs
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, indent=2))     # every other manifest key is preserved
    return offs

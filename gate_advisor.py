#!/usr/bin/env python3
"""Gate advisor: measure where tracks actually flow and die, propose better gate
lines, and let a local Gemma critique the scene — a PROPOSAL, never a calibration.

The Kalambo trial's smoking gun (1,581 entries in, 135 out on one arm; half of all
tracks dying at the frame edge) was a gate-placement problem no model retune could
fix. This tool runs at calibration time: it samples a few segments, tracks every
vehicle, and reports per-gate crossing counts plus where tracks are born and die.
For an exit gate the flow barely reaches, it proposes a replacement line drawn
through the terminal cloud — where the tracker demonstrably still sees vehicles.

Division of labour is deliberate: COORDINATES come from track analytics (VLMs
ground pixels poorly), JUDGMENT comes from Gemma looking at the reference frame
(occlusions, parked ranks, why tracks die there), and the DECISION stays human —
the proposal loads into the Label tab as a draft, and the reviewer's Save goes
through calib.save_calibration like any hand-drawn version: a new version, never
an overwrite, provenance note carried in the doc.

Output: data/advisor/<site>-<cam>.json. Never read by analysis; only the Label tab.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import calib
import engine

STARVED = 0.25       # a gate crossing under this share of its camera's best is starved
EDGE_PX = 0.03       # terminal points within this fraction of a border = frame-edge


def _track_stats(tracks, cal):
    """Pure analytics over {tid: [(x, y), ...]} against one calibration."""
    w, h = cal["image_size"]
    gates = {f'{l["name"]}/{l["kind"]}': {"line": l, "crossings": 0}
             for l in cal["lines"]}
    deaths, edge_deaths = [], 0
    for pts in tracks.values():
        for prev, cur in zip(pts, pts[1:]):
            for g in gates.values():
                if engine.gate_crossing(g["line"], prev, cur) is not None:
                    g["crossings"] += 1
        x, y = pts[-1]
        at_edge = (x < w * EDGE_PX or x > w * (1 - EDGE_PX)
                   or y < h * EDGE_PX or y > h * (1 - EDGE_PX))
        edge_deaths += at_edge
        if not at_edge:
            deaths.append((x, y))
    return gates, deaths, edge_deaths


def _principal_segment(points, span=1.5):
    """A straight segment through a 2D cloud: centroid ± span*sigma along the
    principal axis. 2x2 eigenvector by hand — no numpy at this layer."""
    n = len(points)
    cx = sum(p[0] for p in points) / n
    cy = sum(p[1] for p in points) / n
    sxx = sum((p[0] - cx) ** 2 for p in points) / n
    syy = sum((p[1] - cy) ** 2 for p in points) / n
    sxy = sum((p[0] - cx) * (p[1] - cy) for p in points) / n
    # Largest eigenvalue of [[sxx, sxy], [sxy, syy]] and its eigenvector.
    tr, det = sxx + syy, sxx * syy - sxy * sxy
    lam = tr / 2 + ((tr * tr) / 4 - det) ** 0.5
    vx, vy = (sxy, lam - sxx) if abs(sxy) > 1e-9 else \
        ((1.0, 0.0) if sxx >= syy else (0.0, 1.0))
    norm = (vx * vx + vy * vy) ** 0.5 or 1.0
    vx, vy = vx / norm, vy / norm
    sigma = max(lam, 1.0) ** 0.5
    return ([round(cx - vx * span * sigma, 1), round(cy - vy * span * sigma, 1)],
            [round(cx + vx * span * sigma, 1), round(cy + vy * span * sigma, 1)])


def advise(tracks, cal):
    """(proposal calibration doc, stats). Healthy gates pass through verbatim;
    starved ones are re-drawn through the nearby terminal cloud. Proposing is free —
    binding is the reviewer's Save."""
    gates, deaths, edge_deaths = _track_stats(tracks, cal)
    best = max((g["crossings"] for g in gates.values()), default=0)
    stats = {"tracks": len(tracks),
             "edge_death_pct": round(edge_deaths / max(len(tracks), 1) * 100, 1),
             "gates": {k: g["crossings"] for k, g in gates.items()}}
    lines, proposed = [], []
    for key, g in gates.items():
        line = g["line"]
        starved = best > 0 and g["crossings"] < best * STARVED
        cloud = None
        if starved and deaths:
            # The terminal points nearest THIS gate: where its traffic stops existing.
            mx = sum(p[0] for p in line["points"]) / len(line["points"])
            my = sum(p[1] for p in line["points"]) / len(line["points"])
            near = sorted(deaths, key=lambda p: (p[0] - mx) ** 2 + (p[1] - my) ** 2)
            cloud = near[:max(10, len(near) // 4)]
        if cloud and len(cloud) >= 10:
            a, b = _principal_segment(cloud)
            lines.append({**line, "points": [a, b],
                          "advisor": {"replaces": line["points"],
                                      "why": f"gate saw {g['crossings']} crossings vs "
                                             f"{best} on this camera's best gate; "
                                             f"re-drawn through the {len(cloud)} "
                                             "nearest track terminal points"}})
            proposed.append(key)
        else:
            lines.append(dict(line))
    doc = {"image_size": list(cal["image_size"]), "lines": lines,
           "note": "gate-advisor proposal — reviewed and saved by an operator"}
    stats["proposed"] = proposed
    return doc, stats


def capture(ingest_root, date, site, cam, n_segments=2, stride=3):
    """{tid: [(x, y), ...]} from the first n segments, via ultralytics tracking.
    Runs in the analysis venv; imports lazily so advise() stays stdlib-testable."""
    try:
        from ultralytics import YOLO
    except ImportError:
        raise ValueError("gate_advisor capture needs the analysis venv "
                         "(ultralytics missing) — run it where analyze runs")
    segs = sorted((Path(ingest_root) / date / site / cam).glob("*.mkv"))[:n_segments]
    if not segs:
        raise LookupError(f"no footage for {site}/{cam} on {date}")
    model = YOLO("yolo11s.pt")
    tracks = {}
    for seg in segs:
        for res in model.track(source=str(seg), stream=True, persist=True,
                               vid_stride=stride, verbose=False):
            if res.boxes is None or res.boxes.id is None:
                continue
            for tid, xywh in zip(res.boxes.id.tolist(), res.boxes.xywh.tolist()):
                x, y, _w, _h = xywh
                tracks.setdefault(int(tid), []).append((float(x), float(y)))
    return {t: p for t, p in tracks.items() if len(p) >= 2}


def critique(config, site, cam, ingest_root, stats):
    """Gemma's scene judgment on the reference frame — advisory text, never data.
    Missing endpoint or frame degrades to None rather than blocking the analytics."""
    cfg = config.get("vlm") or {}
    if not (cfg.get("endpoint") or cfg.get("endpoints")):
        return None
    try:
        frame = calib.reference_frame(site, cam, ingest_root)
    except Exception:
        return None
    import base64
    import urllib.request
    prompt = (
        "This is a junction camera used for traffic counting. Gate lines are drawn "
        "on it; vehicles are counted when their tracked centroid crosses a gate.\n"
        f"Measured this session: {json.dumps(stats)}\n"
        "(edge_death_pct = share of vehicle tracks that end at the frame edge; "
        "gates maps each gate to its crossing count; proposed lists gates the "
        "analytics re-drew through the observed track terminal points.)\n"
        "As a traffic-camera engineer, explain what in THIS scene would starve a "
        "gate or kill tracks early — occlusions, parked vehicles, vendor stalls, "
        "trees, glare, the camera's angle — and whether re-aiming the camera would "
        "serve better than moving gates. Be concrete about locations in the image.\n"
        'JSON only: {"assessment": "<3-6 sentences>", "cautions": ["..."]}')
    from vlm_batch import check_local
    endpoints = [check_local(u) for u in (cfg.get("endpoints") or [cfg["endpoint"]])]
    body = json.dumps({
        "model": cfg.get("model") or "",
        "messages": [{"role": "user", "content": prompt,
                      "images": [base64.b64encode(frame).decode()]}],
        "stream": False,
        "format": {"type": "object",
                   "properties": {"assessment": {"type": "string"},
                                  "cautions": {"type": "array",
                                               "items": {"type": "string"}}},
                   "required": ["assessment"]},
        "options": {"temperature": 0},
    }).encode()
    try:
        req = urllib.request.Request(endpoints[0].rstrip("/") + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(json.load(r)["message"]["content"])
    except Exception:
        return None


def advisor_path(data_root, site, cam):
    calib._check("site", site)                      # both become filename segments
    calib._check("camera", cam)
    d = Path(data_root) / "advisor"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{site}-{cam}.json"


def run(site, date, cam, data_root, ingest_root, config, tracks=None):
    tracks = tracks if tracks is not None else capture(ingest_root, date, site, cam)
    cal = calib.get_calibration(site, cam)
    doc, stats = advise(tracks, cal)
    calib._validate(doc)        # a proposal the Save button would refuse helps nobody
    out = {"proposal": doc, "stats": stats,
           "critique": critique(config, site, cam, ingest_root, stats),
           "site": site, "cam": cam, "date": date,
           "at": datetime.now(engine.CAT).isoformat(timespec="seconds")}
    p = advisor_path(data_root, site, cam)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=2))
    tmp.replace(p)
    return {"proposed_gates": stats["proposed"], "tracks": stats["tracks"],
            "path": str(p)}


if __name__ == "__main__":
    # Deliberately NOT `import app`: app's module body constructs the Jobs queue,
    # and a second worker in this process would re-run the server's live jobs.
    # The CLI needs only the three roots, resolved the way app resolves them.
    import os

    import yaml
    root = Path(os.environ.get("COUNTKIT_ROOT") or Path(__file__).parent).resolve()
    cfg = yaml.safe_load((root / "config.yaml").read_text()) or {}
    dr = root / (cfg.get("data_root") or "./data")
    ir = root / (cfg.get("ingest_root") or "./ingest")
    calib.DATA_ROOT = dr
    site, date, cam = sys.argv[1], sys.argv[2], sys.argv[3]
    n = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    res = run(site, date, cam, dr, ir, cfg, tracks=capture(ir, date, site, cam, n))
    print(json.dumps(res, indent=2))

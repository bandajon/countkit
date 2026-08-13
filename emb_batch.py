#!/usr/bin/env python3
"""Vehicle re-ID embeddings for a site-day's evidence crops.

Writes data/emb/<site>-<date>.jsonl: a meta header (model, threshold, margin — both
CALIBRATED from this site-day's own footage), then one row per event with an
L2-normalized vector packed as base64 float16. aggregate.pair() consults cosines
only inside a contest the ambiguity gate refused; a decisive visual separation
pairs as its own tier, anything less holds the gate exactly as before.

Calibration needs no human labels: the tracker already supplies them. The same
tracker id crossing entry and exit is a positive pair (same physical vehicle, two
views); two different ids on one camera within a few seconds are a negative pair
(different vehicles, same scene). Threshold = 99th percentile of negative cosines,
margin = half the gap to the positive median — refuse loudly when the site-day has
too few labelled pairs to calibrate, because an uncalibrated threshold is a guess.

Model: any ONNX image-embedding net trained for vehicle re-ID (VeRi-776 class),
named by config emb.model_path. Deliberately NOT in requirements.txt — boto3
precedent: optional capability, loud when named but absent.
"""
import base64
import json
import statistics
import struct
from datetime import datetime

import aggregate
import engine
from vlm_batch import _crop_bytes

MIN_CAL_PAIRS = 20        # fewer positives or negatives than this = no calibration
NEG_WINDOW_S = 5.0        # co-visible on one camera within this = a negative pair
MARGIN_FLOOR = 0.05


def pack(vec):
    return base64.b64encode(struct.pack(f"<{len(vec)}e", *vec)).decode()


def _embedder(cfg):
    path = (cfg or {}).get("model_path") or ""
    if not path:
        raise ValueError("emb: model_path must point at an ONNX vehicle re-ID model "
                         "in config.yaml — see docs (VeRi-776-trained, e.g. a "
                         "fastreid R50 export)")
    try:
        import cv2
        import numpy as np
        import onnxruntime
    except ImportError as e:
        raise ValueError(f"emb: {e.name} missing — pip install onnxruntime "
                         "opencv-python-headless")
    sess = onnxruntime.InferenceSession(path, providers=["CPUExecutionProvider"])
    inp = sess.get_inputs()[0]
    h, w = (inp.shape[2], inp.shape[3]) if len(inp.shape) == 4 else (256, 256)

    def embed(blob):
        img = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        x = cv2.resize(img, (int(w), int(h)))[:, :, ::-1].astype("float32") / 255.0
        x = (x - [0.485, 0.456, 0.406]) / [0.229, 0.224, 0.225]
        x = x.transpose(2, 0, 1)[None].astype("float32")
        v = sess.run(None, {inp.name: x})[0].flatten()
        n = float((v @ v) ** 0.5) or 1.0
        return [float(c) / n for c in v]

    return embed


def _cos(a, b):
    return sum(x * y for x, y in zip(a, b))     # both already L2-normalized


def _calibrate(vecs, evs):
    """(threshold, margin) from tracker-given labels, or a ValueError that names
    the shortfall."""
    by_obj = {}
    for e in evs:
        if e["i"] in vecs:
            by_obj.setdefault((e["cam"], e["obj_id"]), []).append(e)
    pos = []
    for grp in by_obj.values():
        ens = [e for e in grp if e["kind"] == "entry"]
        exs = [e for e in grp if e["kind"] == "exit"]
        if ens and exs:
            pos.append(_cos(vecs[ens[0]["i"]], vecs[exs[0]["i"]]))
    neg = []
    by_cam = {}
    for e in evs:
        if e["i"] in vecs:
            by_cam.setdefault(e["cam"], []).append(e)
    for grp in by_cam.values():
        grp.sort(key=lambda e: e["ts"])
        for a, b in zip(grp, grp[1:]):
            if b["ts"] - a["ts"] <= NEG_WINDOW_S and a["obj_id"] != b["obj_id"]:
                neg.append(_cos(vecs[a["i"]], vecs[b["i"]]))
    if len(pos) < MIN_CAL_PAIRS or len(neg) < MIN_CAL_PAIRS:
        raise ValueError(
            f"emb: only {len(pos)} same-track and {len(neg)} co-visible pairs to "
            f"calibrate against (need {MIN_CAL_PAIRS} of each) — an uncalibrated "
            "threshold is a guess, not a measurement. Skip the emb tier for this "
            "site-day or record longer.")
    neg.sort()
    threshold = neg[int(len(neg) * 0.99)]
    margin = max(MARGIN_FLOOR, (statistics.median(pos) - threshold) / 2)
    return round(threshold, 4), round(margin, 4), len(pos), len(neg)


def run(site, date, data_root, config, progress=None, cancelled=None, embed=None):
    say = progress or (lambda *a, **k: None)
    embed = embed or _embedder(config.get("emb") or {})
    model = (config.get("emb") or {}).get("model_path", "injected")

    evs = aggregate.load_events(data_root, site, date)
    for i, e in enumerate(evs):
        e["i"] = i                      # pair() stamps this too; here we are upstream
    vecs, skipped = {}, 0
    withcrop = [e for e in evs if e.get("crop")]
    for i, e in enumerate(withcrop):
        if cancelled and cancelled():
            raise engine.Cancelled()
        blob, _why = _crop_bytes(data_root, config, e["crop"])
        if blob is None or blob == engine.PLACEHOLDER_JPEG:
            skipped += 1
            continue
        v = embed(blob)
        if v:
            vecs[e["i"]] = v
        if (i + 1) % 50 == 0 or i + 1 == len(withcrop):
            say(msg=None, i=i + 1, n=len(withcrop),
                pct=round((i + 1) / max(len(withcrop), 1) * 100))

    threshold, margin, npos, nneg = _calibrate(vecs, evs)
    rows = [json.dumps({"meta": {"model": model, "threshold": threshold,
                                 "margin": margin, "pos": npos, "neg": nneg,
                                 "at": datetime.now(aggregate.CAT)
                                 .isoformat(timespec="seconds")}})]
    for e in evs:
        if e["i"] in vecs:
            rows.append(json.dumps({
                "cam": e["cam"], "obj_id": e["obj_id"], "line": e["line"],
                "kind": e["kind"], "ts": e["ts"], "v": pack(vecs[e["i"]])}))

    p = aggregate._sidecar(data_root, "emb", site, date)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(r + "\n" for r in rows))
    tmp.replace(p)
    say(msg=f"{len(vecs)} embeddings, threshold {threshold} margin {margin} "
            f"({npos} pos / {nneg} neg), {skipped} skipped")
    return {"proposed": len(vecs), "skipped": skipped,
            "threshold": threshold, "margin": margin}


if __name__ == "__main__":
    import os
    import random
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    os.environ.setdefault("COUNTKIT_ROOT", str(tmp))
    con = engine.connect(tmp, "gerlache")
    d = tmp / "crops" / "gerlache" / "2026-08-04" / "cam1"
    d.mkdir(parents=True)
    # 25 tracked vehicles, each with an entry and exit crop 2 s apart: enough
    # positives, and the adjacent-vehicle stream supplies the negatives.
    rows = []
    for oid in range(1, 26):
        for k, (kind, dt) in enumerate((("entry", 0), ("exit", 2))):
            name = f"{oid}-{kind}.jpg"
            (d / name).write_bytes(f"veh{oid}".encode() + b"\xff\xd8")
            rows.append(("cam1", oid, "GE" if kind == "entry" else "CH", kind,
                         1000.0 + oid * 3 + dt, f"gerlache/2026-08-04/cam1/{name}"))
    for cam, oid, line, kind, t, crop in rows:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                    ("gerlache", "2026-08-04", cam, oid, "car", line, kind, t, crop))
    con.commit()
    con.close()

    rng = random.Random(7)
    def fake_embed(blob):
        # Same vehicle -> nearly identical vector, different vehicle -> distant.
        oid = int(blob.split(b"veh")[1].split(b"-")[0]) if b"-" in blob \
            else int(blob[3:blob.index(b"\xff")])
        rng2 = random.Random(oid)
        v = [rng2.uniform(-1, 1) for _ in range(16)]
        v = [c + rng.uniform(-0.02, 0.02) for c in v]
        n = sum(c * c for c in v) ** 0.5
        return [c / n for c in v]

    res = run("gerlache", "2026-08-04", tmp, {}, embed=fake_embed)
    assert res["proposed"] == 50, res
    lines = (tmp / "emb" / "gerlache-2026-08-04.jsonl").read_text().splitlines()
    meta = json.loads(lines[0])["meta"]
    assert meta["threshold"] < 0.9 and meta["margin"] >= MARGIN_FLOOR, meta
    assert meta["pos"] == 25 and meta["neg"] >= MIN_CAL_PAIRS, meta
    v = json.loads(lines[1])["v"]
    unpacked = struct.unpack("<16e", base64.b64decode(v))
    assert abs(sum(c * c for c in unpacked) - 1.0) < 0.01, "vector not L2-normed"
    print("emb_batch self-check ok")

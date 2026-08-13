#!/usr/bin/env python3
"""Plate reads for a site-day's evidence crops — the definitive cross-camera signal.

Writes data/plate/<site>-<date>.jsonl: one row per event whose crop shows a readable
plate. aggregate.pair() consumes it read-time as the plate-confirmed tier; nothing
here edits an event or a count.

Plate strings are personal data under the Zambia DPA — the same protection class as
the crops they come from. The sidecar stays internal (the export bundle writes only
its own files and tests pin that), and it is wiped with the site-day's re-analysis
custody: a cache, rewritten whole (tmp + rename), never appended.

Dependency: fast-alpr (plate detector + OCR, ONNX/CPU). Deliberately NOT in
requirements.txt — boto3 precedent: optional capability, loud when named but absent.
"""
import json
from datetime import datetime

import aggregate
import engine
from vlm_batch import _crop_bytes


def _ocr():
    try:
        from fast_alpr import ALPR
    except ImportError:
        raise ValueError("plate: fast-alpr missing — pip install fast-alpr "
                         "(ONNX plate detector + OCR, CPU is fine)")
    import cv2
    import numpy as np
    alpr = ALPR(detector_model="yolo-v9-t-384-license-plate-end2end",
                ocr_model="global-plates-mobile-vit-v2-model")

    def read(blob):
        """crop bytes -> (normalized plate, confidence) or (None, 0)."""
        img = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None, 0.0
        best, conf = None, 0.0
        for r in alpr.predict(img):
            o = getattr(r, "ocr", None)
            if o and o.text and o.confidence > conf:
                best, conf = o.text, float(o.confidence)
        if not best:
            return None, 0.0
        # Normalized before storage so edit distance compares signal, not formatting.
        return "".join(ch for ch in best.upper() if ch.isalnum()), round(conf, 3)

    return read


def run(site, date, data_root, config, progress=None, cancelled=None, ocr=None):
    say = progress or (lambda *a, **k: None)
    ocr = ocr or _ocr()
    db = engine.connect(data_root, site)
    rows = db.execute(
        # Both kinds: a plate pair needs the entry AND the exit read.
        "SELECT cam, obj_id, line, kind, corrected_ts, crop FROM events "
        "WHERE site=? AND date=? AND crop IS NOT NULL ORDER BY corrected_ts",
        (site, date)).fetchall()
    db.close()

    out, skipped = [], 0
    for i, (cam, obj_id, line, kind, ts, crop) in enumerate(rows):
        if cancelled and cancelled():
            raise engine.Cancelled()
        blob, _why = _crop_bytes(data_root, config, crop)
        if blob is None or blob == engine.PLACEHOLDER_JPEG:
            skipped += 1
            continue
        plate, conf = ocr(blob)
        if plate and len(plate) >= 4:      # shorter fragments match everything
            out.append({"cam": cam, "obj_id": obj_id, "line": (line or "").strip(),
                        "kind": kind, "ts": ts, "plate": plate, "conf": conf,
                        "at": datetime.now(aggregate.CAT).isoformat(timespec="seconds")})
        if (i + 1) % 50 == 0 or i + 1 == len(rows):
            say(msg=None, i=i + 1, n=len(rows),
                pct=round((i + 1) / max(len(rows), 1) * 100))

    p = aggregate._sidecar(data_root, "plate", site, date)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in out))
    tmp.replace(p)
    say(msg=f"{len(out)} plates read from {len(rows)} crops, {skipped} skipped")
    return {"proposed": len(out), "skipped": skipped, "crops": len(rows)}


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    os.environ.setdefault("COUNTKIT_ROOT", str(tmp))
    con = engine.connect(tmp, "gerlache")
    d = tmp / "crops" / "gerlache" / "2026-08-04" / "cam1"
    d.mkdir(parents=True)
    (d / "a.jpg").write_bytes(b"\xff\xd8has-a-plate")
    (d / "b.jpg").write_bytes(b"\xff\xd8no-plate")
    for oid, kind, crop in ((1, "entry", "a.jpg"), (2, "exit", "b.jpg")):
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                    ("gerlache", "2026-08-04", "cam1", oid, "car", "GE", kind,
                     1000.0 + oid, f"gerlache/2026-08-04/cam1/{crop}"))
    con.commit()
    con.close()

    fake = lambda blob: ("bae 4021".upper().replace(" ", ""), 0.93) \
        if b"has-a-plate" in blob else (None, 0.0)
    res = run("gerlache", "2026-08-04", tmp, {}, ocr=fake)
    assert res == {"proposed": 1, "skipped": 0, "crops": 2}, res
    row = json.loads((tmp / "plate" / "gerlache-2026-08-04.jsonl").read_text())
    assert row["plate"] == "BAE4021" and row["kind"] == "entry", row
    print("plate_batch self-check ok")

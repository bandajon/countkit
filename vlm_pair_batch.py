#!/usr/bin/env python3
"""Gemma 'same vehicle?' suggestions for contests the ambiguity gate refused.

Research verdict this module is built around: VLMs are near-chance at same-instance
judgments on near-identical fleet vehicles (VLM2-Bench; white Hiaces are the
canonical failure). So a verdict here NEVER pairs anything — agreed 'same' verdicts
become badges in the Find-exit drawer, and only the reviewer's Pair click writes to
data/pair/. Three defenses are structural: unsure is the instructed default, a
'same' answer must cite a discriminative detail, and every pair is asked twice with
the images swapped — disagreement collapses to no suggestion (yes-bias and position
bias are documented failure modes).

Nothing is written to disk but the verdict rows (frame_at precedent: composites of
plate-bearing crops stay in memory). Local endpoints only, as with vlm_batch.
"""
import base64
import json
import urllib.request
from datetime import datetime

import aggregate
import engine
from vlm_batch import _crop_bytes, check_local, TIMEOUT_S

VERDICTS = ["same", "different", "unsure"]
PROMPT = (
    "Two crops from two traffic cameras at one Lusaka junction, 1-2 minutes apart, "
    "different angles. Decide if they show the SAME physical vehicle.\n"
    "Rules, in order:\n"
    "1. If both licence plates are readable and differ: answer different.\n"
    "2. List the visible attributes of each vehicle (colour, body type, livery, "
    "roof rack, damage, stickers, wheel trims) and compare.\n"
    "3. Matching make/model/colour alone is NOT enough — commuter minibuses here "
    "are near-identical. Answer same ONLY if you can cite a discriminative detail "
    "(matching plates, dent, decal, aftermarket part) visible in BOTH images. In "
    "the detail field name the TYPE of feature and its location; never write out "
    "plate characters.\n"
    "4. When uncertain, answer unsure. Unsure is the expected answer for most "
    "pairs and is never wrong.\n"
    'Answer with JSON only: {"verdict": "same|different|unsure", '
    '"detail": "<the discriminative detail, required when verdict is same>"}')
SCHEMA = {"type": "object",
          "properties": {"verdict": {"type": "string", "enum": VERDICTS},
                         "detail": {"type": "string"}},
          "required": ["verdict"]}


def _asker(cfg):
    endpoints = cfg.get("endpoints") or ([cfg["endpoint"]] if cfg.get("endpoint") else [])
    model = cfg.get("model") or ""
    if not (endpoints and model):
        raise ValueError("vlm: endpoint(s) and model must be set in config.yaml — "
                         "the tie-break batch uses the same block as the class batch")
    endpoints = [check_local(u) for u in endpoints]
    state = {"i": 0}

    def ask(blob_a, blob_b):
        url = endpoints[state["i"] % len(endpoints)].rstrip("/") + "/api/chat"
        state["i"] += 1
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": PROMPT,
                          "images": [base64.b64encode(blob_a).decode(),
                                     base64.b64encode(blob_b).decode()]}],
            "stream": False, "format": SCHEMA,
            "options": {"temperature": 0},
        }).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            d = json.loads(json.load(r)["message"]["content"])
            return d.get("verdict"), d.get("detail") or ""

    return ask


def run(site, date, data_root, config, progress=None, cancelled=None, ask=None):
    say = progress or (lambda *a, **k: None)
    ask = ask or _asker(config.get("vlm") or {})
    model = (config.get("vlm") or {}).get("model", "")

    evs = aggregate.load_events(data_root, site, date)
    stats = {}
    aggregate.pair(evs, aggregate.class_map(config),
                   aggregate.read_pair_rows(data_root, site, date)["rows"], stats)
    contests = stats.get("ambiguous_pairs") or []

    out, skipped = [], 0
    for n, c in enumerate(contests):
        if cancelled and cancelled():
            raise engine.Cancelled()
        blobs = []
        for end in (c["entry"], c["exit"]):
            b, _why = (_crop_bytes(data_root, config, end["crop"])
                       if end.get("crop") else (None, "missing"))
            blobs.append(None if b == engine.PLACEHOLDER_JPEG else b)
        if blobs[0] is None or blobs[1] is None:
            skipped += 1
            continue
        try:
            v1, d1 = ask(blobs[0], blobs[1])
            v2, d2 = ask(blobs[1], blobs[0])      # order swap: position bias check
        except Exception:
            skipped += 1
            continue
        # Only an agreed, detailed 'same' becomes a suggestion. Everything else —
        # disagreement, unsure, different, a same with no cited detail — is silence:
        # the drawer already shows every candidate, so silence costs nothing.
        if v1 == v2 == "same" and (d1 or d2):
            drop = lambda e: {k: v for k, v in e.items() if k != "crop"}
            out.append({"entry": drop(c["entry"]), "exit": drop(c["exit"]),
                        "verdict": "same", "detail": (d1 or d2)[:200], "model": model,
                        "at": datetime.now(aggregate.CAT).isoformat(timespec="seconds")})
        if (n + 1) % 5 == 0 or n + 1 == len(contests):
            say(msg=None, i=n + 1, n=len(contests),
                pct=round((n + 1) / max(len(contests), 1) * 100))

    p = aggregate._sidecar(data_root, "vlmpair", site, date)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in out))
    tmp.replace(p)
    say(msg=f"{len(out)} suggestions from {len(contests)} refused contests, "
            f"{skipped} skipped")
    return {"proposed": len(out), "skipped": skipped, "contests": len(contests)}


if __name__ == "__main__":
    import os
    import tempfile
    from pathlib import Path

    tmp = Path(tempfile.mkdtemp())
    os.environ.setdefault("COUNTKIT_ROOT", str(tmp))
    con = engine.connect(tmp, "gerlache")
    d = tmp / "crops" / "gerlache" / "2026-08-04" / "cam1"
    d.mkdir(parents=True)
    for name in ("1.jpg", "2.jpg", "51.jpg", "52.jpg"):
        (d / name).write_bytes(b"\xff\xd8" + name.encode())
    # Two same-class vehicles 2 s apart on cam1, two exits on cam2 — the gate refuses.
    rows = [("cam1", 1, "GE", "entry", 1000.0, "1.jpg"),
            ("cam1", 2, "GE", "entry", 1002.0, "2.jpg"),
            ("cam2", 51, "CH", "exit", 1030.0, "51.jpg"),
            ("cam2", 52, "CH", "exit", 1032.0, "52.jpg")]
    for cam, oid, line, kind, t, crop in rows:
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                    ("gerlache", "2026-08-04", cam, oid, "car", line, kind, t,
                     f"gerlache/2026-08-04/cam1/{crop}"))
    con.commit()
    con.close()

    cfg = {"classes": [{"model": "car", "report": "passenger_car"}],
           "vlm": {"model": "t"}}
    # An asker that recognizes the 1<->51 pairing by crop bytes, in either order.
    def fake(a, b):
        pair = {a[2:].decode(), b[2:].decode()}
        return ("same", "red sticker") if pair == {"1.jpg", "51.jpg"} else ("unsure", "")
    res = run("gerlache", "2026-08-04", tmp, cfg, ask=fake)
    assert res["proposed"] == 1 and res["contests"] >= 2, res
    row = json.loads((tmp / "vlmpair" / "gerlache-2026-08-04.jsonl").read_text())
    assert row["verdict"] == "same" and row["entry"]["obj_id"] == 1, row
    assert "crop" not in row["entry"], "crop keys do not belong in the ledger"

    # A yes-biased asker that flips on order swap suggests nothing.
    flip = {"n": 0}
    def biased(a, b):
        flip["n"] += 1
        return ("same", "looks similar") if flip["n"] % 2 else ("different", "")
    res = run("gerlache", "2026-08-04", tmp, cfg, ask=biased)
    assert res["proposed"] == 0, res
    print("vlm_pair_batch self-check ok")

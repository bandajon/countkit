#!/usr/bin/env python3
"""Gemma class proposals for a site-day's evidence crops, via a LOCAL vision model.

Writes data/vlm/<site>-<date>.jsonl — proposals only. Nothing here touches a count:
the refine UI shows a proposal as a highlighted suggestion, and only a human's click
writes the refinement. Local endpoints only — crops carry readable plates (Zambia
DPA internal QA material), so a hosted API is never an option here.

The sidecar is a cache, not a ledger: regenerating it is cheap, so it is rewritten
whole (tmp + rename) rather than appended — an append would mix rows from an old
model with a new one. Rows for events a re-analysis regenerated simply stop
matching, which is correct; coverage is surfaced in the refine panel, not assumed.
"""
import base64
import json
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import aggregate
import engine

TIMEOUT_S = 300           # one crop; a cold model load on the far end counts against it
FIRST_FAILS = 5           # this many consecutive failures at the start = config error


def check_local(url):
    """The DPA guarantee as code, not a docstring: evidence imagery is POSTed only to
    THIS machine or the local network. localhost, mDNS .local names, link-local (the
    Thunderbolt bridge), and RFC1918 addresses pass; any hosted endpoint is refused
    before a single byte of a crop leaves the box."""
    import ipaddress
    import urllib.parse
    host = (urllib.parse.urlparse(url).hostname or "").strip("[]")
    if host == "localhost" or host.endswith(".local"):
        return url
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None and (ip.is_loopback or ip.is_link_local or ip.is_private):
        return url
    raise ValueError(f"vlm: endpoint {url!r} is not on this machine or the local "
                     "network — evidence crops carry readable plates and never go "
                     "to a hosted API")


def _asker(cfg, targets):
    """crop bytes -> proposed class (or None), over Ollama's /api/chat.

    ponytail: speaks Ollama only — the Jetson's llama-server (OpenAI shape) gets an
    `api:` config key when the vlm-jetson profile lands."""
    endpoints = cfg.get("endpoints") or ([cfg["endpoint"]] if cfg.get("endpoint") else [])
    model = cfg.get("model") or ""
    if not (endpoints and model):
        raise ValueError("vlm: endpoint(s) and model must be set in config.yaml — "
                         "e.g. endpoint: http://localhost:11434, model: gemma4:12b")
    endpoints = [check_local(u) for u in endpoints]
    prompt = (
        "You are classifying one vehicle from a Lusaka (Zambia) traffic camera for a "
        "JICA survey. Classify the LARGEST, most central vehicle; ignore background "
        "vehicles, motorcycles and people.\n"
        "Classes: " + ", ".join(sorted(targets)) + "\n"
        "Guide: passenger_car = sedan/hatchback/SUV/wagon. minibus_medium_bus = "
        "Hiace/Quantum commuter minibus AND Coaster/Rosa medium bus (one class). "
        "large_bus = full-size coach or city bus, 40+ seats. lgv = pickup, panel van, "
        "3-wheeler cargo. mgv = 2-axle rigid truck. hgv_mineral = mine/ore haulage. "
        "hgv_non_mineral = articulated truck or tanker, 3+ axles. goods_vehicle = a "
        "goods vehicle you cannot size confidently. other = motorcycle, bicycle, "
        "tuk-tuk.\n"
        'Answer with JSON only: {"class": "<one class id>"}')
    schema = {"type": "object",
              "properties": {"class": {"type": "string", "enum": sorted(targets)}},
              "required": ["class"]}
    state = {"i": 0}

    def ask(crop_bytes):
        url = endpoints[state["i"] % len(endpoints)].rstrip("/") + "/api/chat"
        state["i"] += 1
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": prompt,
                          # image before text is the documented order for gemma vision
                          "images": [base64.b64encode(crop_bytes).decode()]}],
            "stream": False, "format": schema,
            "options": {"temperature": 0},
        }).encode()
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(json.load(r)["message"]["content"]).get("class")

    return ask


def _crop_bytes(data_root, config, crop_key):
    """Evidence bytes for one crop, or (None, why). Offloaded crops (deleted locally
    after a verified R2 upload) are fetched back through the CDN like /api/crop does —
    skipping them silently would read as 'the model saw everything'."""
    p = Path(data_root) / "crops" / crop_key
    if p.is_file():
        return p.read_bytes(), ""
    cdn = ((config.get("r2") or {}).get("cdn_base") or "").rstrip("/")
    if cdn:
        try:
            with urllib.request.urlopen(f"{cdn}/{crop_key}", timeout=30) as r:
                return r.read(), ""
        except Exception:
            return None, "missing"
    return None, "missing"


def run(site, date, data_root, config, progress=None, cancelled=None, ask=None):
    say = progress or (lambda *a, **k: None)
    targets = aggregate.refine_targets(config)
    ask = ask or _asker(config.get("vlm") or {}, targets)
    model = (config.get("vlm") or {}).get("model", "")

    db = engine.connect(data_root, site)
    rows = db.execute(
        # Entries only: the refine UI reviews an entry and its paired exit together,
        # so proposing on exits would double the batch for zero extra review speed.
        "SELECT cam, obj_id, cls, line, kind, corrected_ts, crop FROM events "
        "WHERE site=? AND date=? AND kind='entry' AND crop IS NOT NULL "
        "ORDER BY corrected_ts", (site, date)).fetchall()
    db.close()

    # Reuse standing proposals from the same model: a resumed batch re-asks nothing.
    out_path = aggregate._sidecar(data_root, "vlm", site, date)
    old = {}
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("model") == model:
                old[aggregate._event_key(r)] = r

    # results holds every standing same-model row plus what this pass adds — so a
    # cancel or crash can never truncate work already paid for. Pruning of rows whose
    # events no longer exist happens ONLY on a completed pass, which is the one moment
    # we know the full current event list was walked.
    results = dict(old)
    seen, new_n, skips, fails = set(), 0, {"placeholder": 0, "missing": 0, "error": 0}, 0

    def flush(keys=None):
        keep = results if keys is None else {k: results[k] for k in keys if k in results}
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in keep.values()))
        tmp.replace(out_path)

    for i, (cam, obj_id, cls, line, kind, ts, crop) in enumerate(rows):
        if cancelled and cancelled():
            flush()                     # partial coverage is honest; losing work is not
            raise engine.Cancelled()
        key = (cam, obj_id, (line or "").strip(), kind, round(float(ts), 3))
        seen.add(key)
        if key in old:
            continue                    # standing proposal from the same model
        blob, why = _crop_bytes(data_root, config, crop)
        if blob is None:
            skips[why] += 1
            continue
        if blob == engine.PLACEHOLDER_JPEG:
            skips["placeholder"] += 1   # mock/cropless detector: nothing to look at
            continue
        try:
            proposed = ask(blob)
            fails = 0
        except Exception:
            skips["error"] += 1
            fails += 1
            if fails >= FIRST_FAILS and not new_n:
                raise ValueError("vlm: every request so far failed — is the endpoint "
                                 "up and the model pulled? (ollama list)")
            continue
        if proposed not in targets:     # a hallucinated label proposes nothing
            continue
        results[key] = {"cam": cam, "obj_id": obj_id, "line": (line or "").strip(),
                        "kind": kind, "ts": ts, "proposed": proposed, "model": model,
                        "at": datetime.now(aggregate.CAT).isoformat(timespec="seconds")}
        new_n += 1
        if new_n % 25 == 0:
            flush()                     # a crash mid-batch costs at most 25 answers
        if (i + 1) % 10 == 0 or i + 1 == len(rows):
            say(msg=None, i=i + 1, n=len(rows),
                pct=round((i + 1) / max(len(rows), 1) * 100))
    flush(seen)                         # completed pass: prune rows with no event left
    kept = sum(1 for k in seen if k in results)
    say(msg=f"{kept} proposals ({new_n} new), {sum(skips.values())} skipped")
    return {"proposed": kept, "reused": kept - new_n,
            "skipped": sum(skips.values()), "skips": skips}


if __name__ == "__main__":
    import tempfile

    # Self-check drives run() with an injected asker over a throwaway install.
    tmp = Path(tempfile.mkdtemp())
    con = engine.connect(tmp, "gerlache")
    crops = tmp / "crops" / "gerlache" / "2026-08-04" / "cam1"
    crops.mkdir(parents=True)
    (crops / "real.jpg").write_bytes(b"\xff\xd8fake-but-not-placeholder")
    (crops / "ph.jpg").write_bytes(engine.PLACEHOLDER_JPEG)
    for oid, crop in ((1, "gerlache/2026-08-04/cam1/real.jpg"),
                      (2, "gerlache/2026-08-04/cam1/ph.jpg"),
                      (3, "gerlache/2026-08-04/cam1/gone.jpg")):
        con.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                    ("gerlache", "2026-08-04", "cam1", oid, "car", "GE", "entry",
                     1000.0 + oid, crop))
    con.commit()
    con.close()

    cfg = {"pcu": {"lgv": 1.5}, "classes": [{"model": "car", "report": "passenger_car"}],
           "vlm": {"model": "test"}}
    res = run("gerlache", "2026-08-04", tmp, cfg, ask=lambda b: "lgv")
    assert res["proposed"] == 1 and res["skipped"] == 2, res
    rows = [json.loads(l) for l in
            (tmp / "vlm" / "gerlache-2026-08-04.jsonl").read_text().splitlines()]
    assert rows[0]["proposed"] == "lgv" and rows[0]["obj_id"] == 1, rows
    # Re-run reuses the standing proposal instead of re-asking.
    res2 = run("gerlache", "2026-08-04", tmp, cfg,
               ask=lambda b: (_ for _ in ()).throw(RuntimeError("re-asked")))
    assert res2["proposed"] == 1, res2
    # A hallucinated label proposes nothing.
    res3 = run("gerlache", "2026-08-04", tmp, {**cfg, "vlm": {"model": "other"}},
               ask=lambda b: "spaceship")
    assert res3["proposed"] == 0, res3
    print("vlm_batch self-check ok")

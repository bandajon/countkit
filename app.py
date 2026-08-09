#!/usr/bin/env python3
"""CountKit — junction analysis & reporting console. Run: python app.py"""

import json
import os
import queue
import re
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

import aggregate
import calib
import engine
import offload
import report

PKG = Path(__file__).resolve().parent
# Everything the operator owns (config.yaml, ./data, ./ingest) hangs off ROOT; the
# shipped files (static/, config.example.yaml) always come from PKG. Tests point
# COUNTKIT_ROOT at a tmpdir to get a throwaway install without copying the app.
ROOT = Path(os.environ.get("COUNTKIT_ROOT") or PKG).resolve()
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_PATH = PKG / "config.example.yaml"

CONFIG = {}
DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")   # ingest tree is keyed by YYYY-MM-DD


def load_config():
    """Re-read config.yaml into the module-level CONFIG dict."""
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}   # parse first: a bad file must
    cfg["classes"] = cfg.get("classes") or []             # not leave CONFIG empty, and an
    CONFIG.clear()                                        # empty `classes:` key is None
    CONFIG.update(cfg)
    return CONFIG


def local_ips():
    """Non-loopback IPv4s of this host, stdlib only."""
    ips = set()
    try:
        for *_, addr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(addr[0])
    except OSError:
        pass          # unresolvable hostname is common on a field LAN; not fatal
    # getaddrinfo alone misses the LAN address on macOS/Jetson; ask the routing table too.
    primary = ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))   # no packets sent, just picks the default route
        primary = s.getsockname()[0]
        ips.add(primary)
    except OSError:
        pass
    finally:
        s.close()
    return ([primary] if primary else []) + sorted(
        ip for ip in ips if not ip.startswith("127.") and ip != primary)


def _dir(key, default):
    # `or default`: a null or blank key in a hand-edited config.yaml would otherwise
    # raise on import and brick the next boot before any route can report it.
    d = ROOT / (CONFIG.get(key) or default)
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_root():
    return _dir("data_root", "./data")


def ingest_root():
    """Not created on demand: a missing ingest tree is a real condition to report
    (footage not copied yet), not something CountKit should paper over."""
    return ROOT / (CONFIG.get("ingest_root") or "./ingest")


def gpu_temp_c():
    """Jetson exposes thermal zones in millidegrees; prefer GPU-therm, else the
    hottest readable zone. No such tree off-Jetson — the rail shows an em dash."""
    hottest = None
    for z in sorted(Path("/sys/devices/virtual/thermal").glob("thermal_zone*")):
        try:
            t = int((z / "temp").read_text().strip()) / 1000.0
            kind = (z / "type").read_text().strip().lower()
        except (OSError, ValueError):
            continue
        if "gpu" in kind:
            return round(t, 1)
        hottest = t if hottest is None else max(hottest, t)
    return round(hottest, 1) if hottest is not None else None


if not CONFIG_PATH.exists():      # first boot
    shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
    print(f"created {CONFIG_PATH} from {EXAMPLE_PATH.name}", flush=True)

load_config()
calib.DATA_ROOT = data_root()

SUBSCRIBERS = []     # [queue, last-consumed epoch] per open SSE client
SUBSCRIBER_STALE = 60


def detector():
    """The model is config, not code. `detector:` names the backend outright; with the
    key absent an nvinfer config selects the real pipeline and its absence replays
    fixtures. Same engine either way — and never a quiet swap: a backend that was asked
    for and cannot be had is an error, not a fallback to a different set of numbers."""
    raw = CONFIG.get("detector")
    # YAML reads `detector: off` as False and `yes` as True — neither may reach the
    # legacy branch, where an unrelated nvinfer_config would quietly pick DeepStream.
    want = raw.strip().lower() if isinstance(raw, str) else raw
    if want not in (None, "", "mock", "yolo", "deepstream"):
        raise ValueError(f"detector: {raw!r} is not a detector — set it to mock, yolo "
                         "or deepstream")
    cfg = CONFIG.get("nvinfer_config") or ""
    if want == "mock" or (not want and not cfg):
        return engine.mock_factory(ROOT / "fixtures")
    if want == "yolo":
        import yolo_runner
        # Weights live under data/, not the container: a bare model name must resolve to
        # the same file after the image is rebuilt.
        return yolo_runner.factory(ingest_root(), {**(CONFIG.get("yolo") or {}),
                                                   "weights_dir": str(data_root() / "models")})
    if not cfg:
        raise ValueError("detector: deepstream needs nvinfer_config set — point it "
                         "at your model's nvinfer config file")
    import deepstream_runner
    return deepstream_runner.factory(ingest_root(), cfg)


def run_job(site, date, say, cancelled):
    return engine.analyze(site, date, ingest_root(), data_root(), detector(),
                          progress=say, cancelled=cancelled)


def jobs_changed():
    # Serialise now, not at yield time: the job dicts keep mutating, so a queued
    # reference would deliver the newest state under an older frame's timestamp.
    frame = json.dumps(JOBS.list())
    now = time.time()
    for s in list(SUBSCRIBERS):
        # A dropped connection never reaches the generator's finally, and the field UI
        # reconnects every 5s — anything that stopped consuming is gone, drop it.
        if now - s[1] > SUBSCRIBER_STALE:
            if s in SUBSCRIBERS:
                SUBSCRIBERS.remove(s)
            continue
        try:
            s[0].put_nowait(frame)
        except queue.Full:
            pass     # a stalled client must never slow the analysis down


JOBS = engine.Jobs(data_root(), run_job, jobs_changed)
# Constructed always so /api/status can report it; the sweep thread starts only under
# __main__, so importing app.py (tests, tooling) never touches anyone's crops.
OFFLOAD = offload.Offload(CONFIG, data_root())

app = FastAPI(title="CountKit")
app.mount("/static", StaticFiles(directory=PKG / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(PKG / "static" / "index.html")


@app.get("/api/status")
def status():
    return {
        "hostname": socket.gethostname(),
        "ips": local_ips(),
        "disk_free_gb": round(shutil.disk_usage(data_root()).free / 1e9, 1),
        "gpu": {"temp_c": gpu_temp_c()},
        "active_job": next((f"{j['site']} {j['date']}" for j in JOBS.list()
                            if j["state"] == "RUNNING"), None),
        "offload": OFFLOAD.info(),
        "time": datetime.now().isoformat(timespec="seconds"),
    }


@app.get("/api/ingest")
def ingest_list():
    """The site-days nvr_pull has landed: <ingest_root>/<date>/<site>/<cam>/*.mkv,
    with manifest.json and the .verified marker sitting in <date>/<site>/."""
    root = ingest_root()
    rows = []
    for day in root.glob("*"):
        if not (day.is_dir() and DATE_DIR.match(day.name)):
            continue
        for site in sorted(p for p in day.iterdir() if p.is_dir()):
            rows.append({
                "date": day.name,
                "site": site.name,
                "cams": sorted(c.name for c in site.iterdir() if c.is_dir()),
                "verified": (site / ".verified").exists(),
                "manifest": (site / "manifest.json").exists(),
            })
    rows.sort(key=lambda r: r["site"])
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


@app.get("/api/config")
def config_get():
    return {"text": CONFIG_PATH.read_text()}


@app.post("/api/config")
def config_post(body: dict = Body(default={})):
    text = body.get("text") or ""
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise HTTPException(400, f"YAML parse error: {e}")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "config must be a mapping of keys")
    if cfg.get("classes") is not None and not isinstance(cfg["classes"], list):
        raise HTTPException(400, "'classes' must be a list if present")
    for k in ("pcu", "probe", "r2", "google_routes", "branding", "corridor"):
        if cfg.get(k) is not None and not isinstance(cfg[k], dict):
            raise HTTPException(400, f"'{k}' must be a mapping if present")
    # Blanking one of these used to save fine and then 500 every route — and take the
    # next boot down with it, since the module body resolves them at import.
    for k in ("data_root", "ingest_root", "site_tz"):
        if k in cfg and not (isinstance(cfg[k], str) and cfg[k].strip()):
            raise HTTPException(400, f"'{k}' cannot be blank — give it a value or "
                                     "remove the line to use the default")
    # Write the operator's text verbatim: a safe_dump round-trip would strip every comment.
    CONFIG_PATH.write_text(text)
    load_config()
    return {"ok": True}


def _calib(fn, *a):
    """calib and aggregate speak ValueError for operator error and LookupError for
    absent data; both carry text the UI shows verbatim."""
    try:
        return fn(*a)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


# Declared before /{site}/{cam}: FastAPI matches in declaration order, and 'armmap'
# is a legal camera name — a camera actually called armmap would be shadowed here.
@app.get("/api/calib/{site}/armmap")
def calib_armmap(site: str):
    return _calib(calib.arm_map, site)


@app.get("/api/calib/{site}/{cam}/versions")
def calib_versions(site: str, cam: str):
    return _calib(calib.list_versions, site, cam)


@app.get("/api/calib/{site}/{cam}")
def calib_get(site: str, cam: str, version: int = None):
    return _calib(calib.get_calibration, site, cam, version)


@app.post("/api/calib/{site}/{cam}")
def calib_post(site: str, cam: str, doc: dict = Body(default={})):
    saved = _calib(calib.save_calibration, site, cam, doc)
    # The arm map comes back with the save: a new line can unown or double-own an arm,
    # and the operator must see that without a second round trip.
    return {**saved, "armmap": calib.arm_map(site)}


@app.post("/api/calib/{site}/{cam}/activate")
def calib_activate(site: str, cam: str, body: dict = Body(default={})):
    return _calib(calib.set_active, site, cam, body.get("version"))


@app.get("/api/frame/{site}/{cam}")
def frame_get(site: str, cam: str):
    return Response(_calib(calib.reference_frame, site, cam, ingest_root()),
                    media_type="image/jpeg")


MAX_REFERENCE = 25 << 20


@app.post("/api/frame/{site}/{cam}")
async def frame_post(site: str, cam: str, request: Request):
    # request.body() materialises the whole POST in the Orin's RAM, so refuse before
    # reading — and again after, since a chunked body announces no length at all.
    too_big = HTTPException(413, "reference photo too large — 25 MB max")
    n = request.headers.get("content-length") or ""
    if n.isdigit() and int(n) > MAX_REFERENCE:
        raise too_big
    data = await request.body()
    if len(data) > MAX_REFERENCE:
        raise too_big
    return _calib(calib.save_reference, site, cam, data)


@app.get("/api/analyze/jobs")
def analyze_jobs():
    return JOBS.list()


@app.post("/api/analyze/queue")
def analyze_queue(body: dict = Body(default={})):
    site, date = body.get("site") or "", body.get("date") or ""
    try:
        engine.check_ready(ingest_root(), date, site)   # .verified + every clock offset
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JOBS.add(site, date)


@app.post("/api/analyze/cancel")
def analyze_cancel(body: dict = Body(default={})):
    return {"ok": JOBS.cancel(body.get("job_id"))}


@app.post("/api/analyze/retry")
def analyze_retry(body: dict = Body(default={})):
    return {"ok": JOBS.retry(body.get("job_id"))}


@app.get("/api/analyze/status/{site}/{date}")
def analyze_status(site: str, date: str):
    """Whether this site-day is queueable, already analyzed, or blocked — with the
    blocking copy the row shows verbatim."""
    try:
        engine.check_ready(ingest_root(), date, site)
        blocked = ""
    except ValueError as e:
        blocked = str(e)
    db = engine.connect(data_root(), site)
    n = db.execute("SELECT COUNT(*) FROM events WHERE site=? AND date=?",
                   (site, date)).fetchone()[0]
    db.close()
    return {"blocked": blocked, "events": n}


@app.get("/api/analyze/stream")
def analyze_stream():
    q = queue.Queue(maxsize=100)
    sub = [q, time.time()]        # every successful yield refreshes the timestamp
    SUBSCRIBERS.append(sub)

    def gen():
        try:
            yield f"data: {json.dumps(JOBS.list())}\n\n"     # current truth first
            sub[1] = time.time()
            while True:
                try:
                    frame = f"data: {q.get(timeout=15)}\n\n"
                except queue.Empty:
                    frame = ": ping\n\n"   # keep idle proxies from closing the stream
                yield frame
                sub[1] = time.time()
        finally:
            if sub in SUBSCRIBERS:
                SUBSCRIBERS.remove(sub)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.get("/api/counts/{site}/{date}")
def counts_get(site: str, date: str):
    return _calib(aggregate.counts, site, date, data_root(), ingest_root(), CONFIG)


CDN_BASE = ""     # Task 8 fills this in; empty means local crops only


@app.post("/api/report/{site}/{date}")
def report_build(site: str, date: str):
    """Seconds for one site-day, so it runs inline rather than through the job queue."""
    try:
        return report.build(site, date, data_root(), ingest_root(), CONFIG)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/report/{site}/{date}")
def report_status(site: str, date: str):
    return report.status(data_root(), site, date)


@app.get("/api/report/file/{site}/{date}/{name}")
def report_file(site: str, date: str, name: str):
    # Only names the manifest vouches for — no traversal, no stray files.
    if name not in report.status(data_root(), site, date)["files"]:
        raise HTTPException(404, "not in the export manifest")
    return FileResponse(report.bundle_dir(data_root(), site, date) / name,
                        filename=name)


@app.get("/api/crop/{key:path}")
def crop_get(key: str):
    """Evidence crops are served from disk. A missing crop is a labelled placeholder
    in the UI, never a broken image — so 404 here is an expected state, not an error."""
    if ".." in key or key.startswith("/"):
        raise HTTPException(400, "bad crop key")
    p = data_root() / "crops" / key
    if p.is_file():
        return FileResponse(p, media_type="image/jpeg")
    # Offloaded under disk pressure: the bytes live in R2 now, served through the CDN.
    # The redirect is the provenance signal the drawer reads (response.redirected).
    cdn = (CONFIG.get("r2") or {}).get("cdn_base") or ""
    if cdn:
        return RedirectResponse(cdn.rstrip("/") + "/" + key, status_code=302)
    raise HTTPException(404, "no crop")


CORRIDOR_TTL = 60
_corridor_cache = {"at": 0.0, "road": None, "payload": None}   # in memory only — see corridor_get()
SPEEDS = {"NORMAL": 0, "SLOW": 1, "TRAFFIC_JAM": 2}


def corridor_live(key, road):
    """Google Routes congestion for each adjacent pair of corridor points.

    LIVE ONLY. Google's terms bar storing or caching this series, so it is held for
    CORRIDOR_TTL seconds in memory and never written to disk, never joined to counts,
    never exported. The storable correlation work runs on the licensed TomTom dataset.
    """
    import requests
    out = []
    for i, (a, b) in enumerate(zip(road, road[1:])):
        body = {"origin": {"location": {"latLng": {"latitude": a[0], "longitude": a[1]}}},
                "destination": {"location": {"latLng": {"latitude": b[0], "longitude": b[1]}}},
                "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE",
                "extraComputations": ["TRAFFIC_ON_POLYLINE"]}
        r = requests.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes", json=body,
            headers={"X-Goog-Api-Key": key,
                     "X-Goog-FieldMask": "routes.travelAdvisory.speedReadingIntervals"},
            timeout=10)
        r.raise_for_status()
        routes = (r.json() or {}).get("routes") or [{}]
        spans = (routes[0].get("travelAdvisory") or {}).get("speedReadingIntervals") or []
        # Worst reading wins: a corridor glance should show the jam, not average it away.
        level = max((SPEEDS.get(s.get("speed"), 0) for s in spans), default=0)
        out.append({"i": i, "a": a, "b": b, "level": level})
    return out


@app.get("/api/corridor")
def corridor_get():
    cor = CONFIG.get("corridor") or {}
    key = (CONFIG.get("google_routes") or {}).get("api_key") or ""
    road = cor.get("road") or []
    if not (cor.get("enabled") and key and len(road) > 1):
        return {"configured": False}
    base = {"configured": True, "road": road, "cross": cor.get("cross") or [],
            "junctions": cor.get("junctions") or []}
    now = time.time()
    # Keyed on the road: editing corridor.road would otherwise pair new geometry with
    # the previous road's segments for the rest of the TTL.
    if (_corridor_cache["payload"] and _corridor_cache["road"] == road
            and now - _corridor_cache["at"] < CORRIDOR_TTL):
        return {**base, "segments": _corridor_cache["payload"]}
    try:
        segs = corridor_live(key, road)
    except Exception as e:
        # A dead key or no network is a missing glance, not a broken console.
        return {**base, "segments": [], "error": f"{type(e).__name__}: {e}"}
    _corridor_cache.update(at=now, road=road, payload=segs)
    return {**base, "segments": segs}


@app.get("/corridor")
def corridor_page():
    return FileResponse(PKG / "static" / "corridor-map.html")


@app.get("/api/movements/{site}/{date}")
def movements_get(site: str, date: str, entry: str = None, exit: str = None,
                  bin: int = None, limit: int = 30):
    return _calib(aggregate.movements_detail, site, date, data_root(), CONFIG,
                  entry, exit, bin, limit)


@app.get("/api/flags/{site}/{date}")
def flags_get(site: str, date: str):
    return _calib(aggregate.read_flags, data_root(), site, date)


@app.post("/api/flag")
def flag_post(body: dict = Body(default={})):
    site, date = body.get("site") or "", body.get("date") or ""
    if not (site and date):
        raise HTTPException(400, "site and date are required")
    aggregate.add_flag(data_root(), site, date,
                       {k: body.get(k) for k in ("entry", "exit", "bin", "entry_ts")})
    return aggregate.read_flags(data_root(), site, date)


@app.get("/api/offsets/{site}/{date}")
def offsets_get(site: str, date: str):
    """Every camera of the site-day, with null for the ones still unset."""
    offs = engine.offsets(ingest_root(), date, site)
    return {cam: offs.get(cam) for cam in engine.site_day_cams(ingest_root(), date, site)}


@app.post("/api/offsets")
def offsets_post(body: dict = Body(default={})):
    site, date = body.get("site") or "", body.get("date") or ""
    cam, off = body.get("cam") or "", body.get("offset_s")
    if not (site and date and cam):
        raise HTTPException(400, "site, date and cam are required")
    if off is not None and not isinstance(off, (int, float)):
        raise HTTPException(400, "offset_s must be a number of seconds, or null to unset")
    try:
        return aggregate.set_offset(ingest_root(), date, site, cam, off)
    except ValueError as e:
        raise HTTPException(400, str(e))


if __name__ == "__main__":
    OFFLOAD.start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8090)))

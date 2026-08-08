#!/usr/bin/env python3
"""CountKit — junction analysis & reporting console. Run: python app.py"""

import os
import re
import shutil
import socket
from datetime import datetime
from pathlib import Path

import uvicorn
import yaml
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
    d = ROOT / CONFIG.get(key, default)
    d.mkdir(parents=True, exist_ok=True)
    return d


def data_root():
    return _dir("data_root", "./data")


def ingest_root():
    """Not created on demand: a missing ingest tree is a real condition to report
    (footage not copied yet), not something CountKit should paper over."""
    return ROOT / CONFIG.get("ingest_root", "./ingest")


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
        "active_job": None,       # the Analyze queue fills this in
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
    for k in ("pcu", "probe", "r2", "google_routes", "branding"):
        if cfg.get(k) is not None and not isinstance(cfg[k], dict):
            raise HTTPException(400, f"'{k}' must be a mapping if present")
    # Write the operator's text verbatim: a safe_dump round-trip would strip every comment.
    CONFIG_PATH.write_text(text)
    load_config()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8090)))

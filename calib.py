#!/usr/bin/env python3
"""Gate calibrations: versioned per site+camera, plus the site-level arm-ownership map."""

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

DATA_ROOT = Path("./data")   # app.py repoints this at data_root(); moving it needs a restart

# site/cam names become path segments; \Z not $, or "site1\n" validates and gets its
# own identical-looking tree.
NAME = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
KINDS = ("entry", "exit")
COMPASS = ("N", "S", "E", "W", "")


def _check(label, v):
    if not isinstance(v, str) or not NAME.match(v):
        raise ValueError(f"bad {label} name {v!r}: letters, digits, _ and - only")


def _site_dir(site):
    _check("site", site)
    return DATA_ROOT / "calibrations" / site


def _cam_dir(site, cam):
    _check("camera", cam)
    return _site_dir(site) / cam


def _versions(d):
    return sorted(int(p.stem[1:]) for p in d.glob("v*.json") if p.stem[1:].isdigit())


def _validate(doc):
    if not isinstance(doc, dict):
        raise ValueError("calibration must be an object")
    size = doc.get("image_size")
    if (not isinstance(size, list) or len(size) != 2
            or not all(isinstance(n, int) and not isinstance(n, bool) and n > 0 for n in size)):
        raise ValueError("image_size must be [width, height] in positive pixels")
    lines = doc.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ValueError("a calibration needs at least one gate line")
    for i, ln in enumerate(lines):
        at = f"line {i + 1}"
        if not isinstance(ln, dict):
            raise ValueError(f"{at} must be an object")
        if ln.get("kind") not in KINDS:
            raise ValueError(f"{at}: kind must be 'entry' or 'exit'")
        if not str(ln.get("name") or "").strip():
            raise ValueError(f"{at}: needs an arm name")
        if ln.get("compass", "") not in COMPASS:
            raise ValueError(f"{at}: compass must be N, S, E, W or empty")
        pts = ln.get("points")
        if not isinstance(pts, list) or len(pts) < 2:
            raise ValueError(f"{at}: a gate needs at least 2 points")
        for p in pts:
            if (not isinstance(p, list) or len(p) != 2
                    or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in p)):
                raise ValueError(f"{at}: every point must be [x, y]")
        # Which side counts as crossing IN — the single most error-prone field in the UI.
        if ln.get("dir") not in ("AB", "BA"):
            raise ValueError(f"{at}: dir must be 'AB' or 'BA'")


def save_calibration(site, cam, doc):
    """Always a new version: a camera re-aimed mid-survey must not retro-change
    yesterday's counts, so nothing is ever overwritten."""
    _validate(doc)
    d = _cam_dir(site, cam)
    d.mkdir(parents=True, exist_ok=True)
    # Two saves land in FastAPI's threadpool at once and read the same highest version:
    # "x" makes claiming a number atomic, and the loser simply takes the next one.
    for _ in range(20):
        n = max(_versions(d), default=0) + 1
        try:
            with open(d / f"v{n}.json", "x") as f:
                f.write(json.dumps(doc, indent=2))
            break
        except FileExistsError:
            continue
    else:
        raise ValueError("too many calibration saves at once — try again")
    (d / "active.json").write_text(json.dumps({"active": n}))
    return {"version": n}


def active_version(site, cam):
    try:
        return json.loads((_cam_dir(site, cam) / "active.json").read_text())["active"]
    except (OSError, ValueError, KeyError):
        return None


def get_calibration(site, cam, version=None):
    d = _cam_dir(site, cam)
    n = version if version is not None else active_version(site, cam)
    if n is None:
        raise LookupError(f"no calibration for {site}/{cam} yet")
    p = d / f"v{int(n)}.json"
    if not p.exists():
        raise LookupError(f"no calibration v{n} for {site}/{cam}")
    return json.loads(p.read_text())


def list_versions(site, cam):
    d = _cam_dir(site, cam)
    act = active_version(site, cam)
    return [{"version": n,
             "saved": datetime.fromtimestamp((d / f"v{n}.json").stat().st_mtime)
                              .isoformat(timespec="seconds"),
             "lines": len(json.loads((d / f"v{n}.json").read_text()).get("lines", [])),
             "active": n == act}
            for n in _versions(d)]


def set_active(site, cam, version):
    """Repoints only — the superseded version stays on disk."""
    d = _cam_dir(site, cam)
    try:
        n = int(version)
    except (TypeError, ValueError):
        raise ValueError(f"version must be a number, got {version!r}")
    if n not in _versions(d):
        raise ValueError(f"no calibration v{n} for {site}/{cam}")
    (d / "active.json").write_text(json.dumps({"active": n}))
    return {"version": n}


def arm_map(site):
    """Judges the junction as a whole: each arm's entry and its exit must be owned by
    exactly one camera, or vehicles there are missed (unowned) or counted twice (double)."""
    arms, notes, cams = {}, [], []
    for cd in sorted(p for p in _site_dir(site).glob("*") if p.is_dir()):
        try:
            doc = get_calibration(site, cd.name)
        except LookupError:
            continue          # a directory with versions but no active pointer
        cams.append(cd.name)
        kinds = set()
        for ln in doc["lines"]:
            a = arms.setdefault(ln["name"].strip().lower(),
                                {"arm": ln["name"].strip(), "compass": "", "entry": [], "exit": []})
            a["compass"] = a["compass"] or ln.get("compass", "")
            if cd.name not in a[ln["kind"]]:
                a[ln["kind"]].append(cd.name)
            kinds.add(ln["kind"])
        if "entry" in kinds and "exit" not in kinds:
            notes.append({"cam": cd.name,
                          "note": "volumes only from this view — exits live on other cameras"})
    out = []
    for a in sorted(arms.values(), key=lambda a: a["arm"].lower()):
        a["state"] = ("double" if len(a["entry"]) > 1 or len(a["exit"]) > 1
                      else "unowned" if not (a["entry"] and a["exit"]) else "owned")
        out.append(a)
    return {"arms": out, "notes": notes, "cams": cams}


def reference_path(site, cam):
    return _cam_dir(site, cam) / "reference.jpg"


def save_reference(site, cam, data):
    if data[:2] != b"\xff\xd8":
        raise ValueError("reference frame must be a JPEG")
    p = reference_path(site, cam)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return {"bytes": len(data)}


def reference_frame(site, cam, ingest_root):
    """An uploaded photo wins over footage: a camera may be newly aimed, or have no
    verified footage yet, and labelling must not have to wait for a pull."""
    p = reference_path(site, cam)
    if p.exists():
        return p.read_bytes()
    days = sorted((d for d in Path(ingest_root).glob("*") if d.is_dir()), reverse=True)
    mkvs = [m for d in days for m in sorted((d / site / cam).glob("*.mkv"), reverse=True)]
    if not mkvs:
        raise LookupError(f"no footage for {site}/{cam} yet — upload a reference photo")
    try:
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(mkvs[0]),
                            "-frames:v", "1", "-q:v", "4", "-f", "mjpeg", "-"],
                           capture_output=True, timeout=15)
    except FileNotFoundError:
        raise LookupError("ffmpeg not found on PATH — install it or upload a reference photo")
    except subprocess.TimeoutExpired:
        raise LookupError(f"ffmpeg timed out reading {mkvs[0].name}")
    if r.returncode or not r.stdout.startswith(b"\xff\xd8"):
        raise LookupError(f"could not decode a frame from {mkvs[0].name}: "
                          + (r.stderr.decode(errors="replace").strip()[-200:] or "no output"))
    return r.stdout

#!/usr/bin/env python3
"""Analysis engine: detector -> line crossings -> events + evidence crops in SQLite.

The detector is an interface, not a model: MockDetector replays fixtures off-Jetson,
DeepStreamDetector (deepstream_runner.py) runs the real pipeline. Everything below
this line — geometry, debounce, clock correction, crops, idempotency — is identical
on both, so a fixture that passes here is a genuine test of the Orin behaviour."""

import json
import queue
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

import calib

SEG_NAME = re.compile(r"^(\d{8}-\d{6})\.mkv$")     # FieldKit recorder wallclock segments
RECROSS_S = 5.0            # media seconds a given obj may not re-cross the same line
CROP_QUEUE = 2000          # crops buffered before the writer starts dropping
BLOCKED = "{cam}: time offset unset — set it in Counts → Offsets. Unset is not zero."
# A 1x1 JPEG. The mock has no frames to crop, but the row/file plumbing must be
# exercised exactly as it will be on the Orin.
PLACEHOLDER_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    "ffffffffffffffffffffffffffffffffffffffffffc00011080001000103012200021101031101"
    "ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b51000"
    "02010303020403050504040000017d01020300041105122131410613516107227114328191a108"
    "2342b1c11552d1f02433627282090a161718191a25262728292a3435363738393a434445464748"
    "494a535455565758595a636465666768696a737475767778797a838485868788898a9293949596"
    "9798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9"
    "dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffda0008010100003f00fbfe8a28a2803fff"
    "d9")


def segment_epoch(name):
    """'20260804-070000.mkv' -> epoch seconds of the segment's first frame."""
    m = SEG_NAME.match(Path(name).name)
    if not m:
        raise ValueError(f"segment {name!r} is not a FieldKit wallclock segment")
    return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()


def offsets(ingest_root, date, site):
    """Per-camera clock corrections from the footage manifest. Missing key != 0.0:
    a camera left on China time reads six hours out and would silently mis-pair."""
    p = Path(ingest_root) / date / site / "manifest.json"
    try:
        return json.loads(p.read_text()).get("time_offset_s") or {}
    except (OSError, ValueError):
        return {}


def site_day_cams(ingest_root, date, site):
    d = Path(ingest_root) / date / site
    return sorted(c.name for c in d.iterdir() if c.is_dir()) if d.is_dir() else []


def check_ready(ingest_root, date, site):
    """Raises ValueError with the copy the UI shows verbatim. Analysis of unverified
    footage, or footage whose clock is unknown, is worse than no analysis at all."""
    d = Path(ingest_root) / date / site
    if not (d / ".verified").exists():
        raise ValueError(f"{site} {date}: not verified — pull it with FieldKit first")
    cams = site_day_cams(ingest_root, date, site)
    if not cams:
        raise ValueError(f"{site} {date}: no cameras in the ingest tree")
    offs = offsets(ingest_root, date, site)
    for cam in cams:
        if cam not in offs:
            raise ValueError(BLOCKED.format(cam=cam))
    return cams, offs


# ---------------------------------------------------------------- geometry

def side(a, b, p):
    """Signed side of p about the directed segment a->b. Screen coords (y down), so
    a negative result is the LEFT of travel a->b."""
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])


def crosses(p1, p2, a, b):
    """True when the step p1->p2 actually intersects segment a->b — not merely the
    infinite line, which would fire for traffic passing well beyond the gate's end."""
    d1, d2 = side(a, b, p1), side(a, b, p2)
    d3, d4 = side(p1, p2, a), side(p1, p2, b)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


def gate_crossing(line, prev, cur):
    """None when the step misses the gate; True when it crossed in the gate's counted
    direction, False when against it.

    dir 'AB': a crossing counts when the object ARRIVES on the left of travel along the
    drawn points (side() < 0) — the side the Label chevron points at, so the arrow shows
    the way counted traffic travels. 'BA' is that polyline walked backwards.

    Returning False rather than None matters: an against-direction crossing still arms
    the re-cross debounce, so a vehicle stopped on the line cannot wobble up a count."""
    pts = line["points"]
    inside_is_negative = line.get("dir", "AB") == "AB"
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        if crosses(prev, cur, a, b):
            return (side(a, b, cur) < 0) == inside_is_negative
    return None


# ---------------------------------------------------------------- detectors

class MockDetector:
    """Replays fixtures/<site>/<date>/<cam>.jsonl against segments.json. Each line is
    {"t": seconds into its segment, "objects": [...], "seg": optional filename}."""

    def __init__(self, root, site, date, cam):
        self.dir = Path(root) / site / date
        self.cam = cam
        self.segments = json.loads((self.dir / "segments.json").read_text())[cam]

    def __iter__(self):
        frames = []
        p = self.dir / f"{self.cam}.jsonl"
        if p.exists():
            frames = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
        for seg in self.segments:
            for f in frames:
                # A single-segment fixture may omit "seg" — the common case in tests.
                if f.get("seg", self.segments[0]["file"]) == seg["file"]:
                    yield seg["file"], float(f["t"]), f["objects"]


def mock_factory(root):
    return lambda site, date, cam: MockDetector(root, site, date, cam)


# ---------------------------------------------------------------- crops

class CropWriter:
    """Encoding and disk must never stall inference: a full queue drops the crop and
    the event simply records no image."""

    def __init__(self, root):
        self.root, self.q = Path(root), queue.Queue(maxsize=CROP_QUEUE)
        self.written = self.dropped = 0
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            key, data = item
            try:
                p = self.root / key
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                self.written += 1
            except OSError:
                self.dropped += 1      # a failed write degrades to "crop missing"

    def put(self, key, data):
        try:
            self.q.put_nowait((key, data))
            return key
        except queue.Full:
            self.dropped += 1
            return None

    def close(self):
        self.q.put(None)
        self.t.join(timeout=10)


# ---------------------------------------------------------------- analysis

def db_path(data_root, site):
    p = Path(data_root) / "waves"
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{site}.db"


def connect(data_root, site):
    db = sqlite3.connect(db_path(data_root, site))
    db.execute("""CREATE TABLE IF NOT EXISTS events(
        site TEXT, date TEXT, cam TEXT, obj_id INT, cls TEXT, line TEXT,
        kind TEXT, corrected_ts REAL, crop TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS events_site_date ON events(site, date)")
    return db


def analyze(site, date, ingest_root, data_root, detector, progress=None, cancelled=None):
    """One site-day, every camera. Idempotent: the previous run's events and crops for
    this (site, date) are removed first, so a re-run after a crash is always safe."""
    cams, offs = check_ready(ingest_root, date, site)
    say = progress or (lambda **k: None)
    stop = cancelled or (lambda: False)

    db = connect(data_root, site)
    db.execute("DELETE FROM events WHERE site=? AND date=?", (site, date))
    db.commit()
    crop_root = Path(data_root) / "crops"
    shutil.rmtree(crop_root / site / date, ignore_errors=True)
    crops = CropWriter(crop_root)

    dets = {cam: detector(site, date, cam) for cam in cams}
    total = sum(len(d.segments) for d in dets.values()) or 1
    done_segs, events = 0, 0
    try:
        for cam in cams:
            try:
                lines = calib.get_calibration(site, cam)["lines"]
            except LookupError:
                say(cam=cam, msg=f"{cam}: no calibration — skipped", pct=done_segs * 100 // total)
                done_segs += len(dets[cam].segments)
                continue
            last_seen, last_cross, seg_now = {}, {}, None
            crop_for = getattr(dets[cam], "crop_for", None)
            for seg, t, objects in dets[cam]:
                if stop():
                    raise Cancelled()
                if seg != seg_now:
                    seg_now, base = seg, segment_epoch(seg)
                    done_segs += 1
                    say(cam=cam, segment=seg, i=done_segs, n=total,
                        pct=done_segs * 100 // total, events=events,
                        msg=f"{cam}: {seg}")
                ts = base + t + offs[cam]
                for o in objects:
                    oid = o["id"]
                    cur = o.get("centroid") or _bbox_centre(o["bbox"])
                    prev = last_seen.get(oid)
                    last_seen[oid] = cur
                    if prev is None:
                        continue
                    for ln in lines:
                        hit = gate_crossing(ln, prev, cur)
                        if hit is None:
                            continue
                        key = (oid, ln["name"], ln["kind"])
                        if ts - last_cross.get(key, -1e9) < RECROSS_S:
                            continue     # one decision per object per gate per 5 s
                        last_cross[key] = ts
                        if not hit:
                            continue     # crossed against the gate's direction
                        crop = crops.put(_crop_key(site, date, cam, oid, ln["name"], ts),
                                         _crop_bytes(crop_for, o))
                        db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)",
                                   (site, date, cam, oid, o["cls"], ln["name"],
                                    ln["kind"], ts, crop))
                        events += 1
        db.commit()
    finally:
        crops.close()
        db.commit()
        db.close()
    say(pct=100, events=events, msg=f"{events} events · {crops.written} crops"
        + (f" · {crops.dropped} dropped" if crops.dropped else ""))
    return {"events": events, "crops": crops.written, "dropped": crops.dropped,
            "cams": cams}


class Cancelled(Exception):
    pass


def _crop_bytes(crop_for, o):
    """A detector that holds the frame may cut a real crop; one that cannot, or that
    fails on a given object, degrades to the placeholder. A crop is evidence, not a
    count — a broken encoder must never take down the site-day it illustrates."""
    if crop_for is None:
        return PLACEHOLDER_JPEG
    try:
        return crop_for(o) or PLACEHOLDER_JPEG
    except Exception:
        return PLACEHOLDER_JPEG


def _bbox_centre(b):
    return [b[0] + b[2] / 2, b[1] + b[3] / 2]


def _crop_key(site, date, cam, oid, line, ts):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", line)
    return f"{site}/{date}/{cam}/{oid}-{safe}-{int(ts * 1000)}.jpg"


# ---------------------------------------------------------------- job queue

class Jobs:
    """One job at a time — the Orin has one GPU. Queue survives restart; a job caught
    RUNNING by a reboot goes back to QUEUED, which idempotency makes safe."""

    def __init__(self, data_root, run, on_change=None):
        self.path = Path(data_root) / "jobs.json"
        self.run, self.on_change = run, on_change or (lambda: None)
        self.lock = threading.Lock()
        self.jobs = []
        self.cancel_ids = set()
        self._load()
        self.wake = threading.Event()
        threading.Thread(target=self._worker, daemon=True).start()
        self.wake.set()

    def _load(self):
        try:
            self.jobs = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        for j in self.jobs:
            if j["state"] == "RUNNING":
                j["state"], j["progress"] = "QUEUED", {}
                j["log"] = (j.get("log") or []) + ["restarted after a restart"]

    def _save(self):
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self.jobs))
            tmp.replace(self.path)
        except OSError:
            pass       # a read-only data dir must not kill a running analysis

    def _changed(self):
        self._save()
        self.on_change()

    def add(self, site, date):
        with self.lock:
            jid = max([j["id"] for j in self.jobs], default=0) + 1
            job = {"id": jid, "site": site, "date": date, "state": "QUEUED",
                   "queued": time.time(), "attempts": 0, "progress": {}, "log": [],
                   "result": None}
            self.jobs.append(job)
        self._changed()
        self.wake.set()
        return job

    def cancel(self, jid):
        with self.lock:
            for j in self.jobs:
                if j["id"] == jid and j["state"] in ("QUEUED", "RUNNING"):
                    self.cancel_ids.add(jid)
                    if j["state"] == "QUEUED":
                        j["state"] = "CANCELLED"
                    self._changed()
                    return True
        return False

    def retry(self, jid):
        with self.lock:
            for j in self.jobs:
                if j["id"] == jid and j["state"] in ("FAILED", "CANCELLED"):
                    j.update(state="QUEUED", progress={}, log=[])
                    self.cancel_ids.discard(jid)
                    self._changed()
                    self.wake.set()
                    return True
        return False

    def _next(self):
        with self.lock:
            for j in self.jobs:
                if j["state"] == "QUEUED":
                    j["state"], j["attempts"] = "RUNNING", j["attempts"] + 1
                    j["started"] = time.time()
                    return j
        return None

    def _worker(self):
        while True:
            job = self._next()
            if not job:
                self.wake.wait(timeout=2)
                self.wake.clear()
                continue
            self._changed()

            def say(msg=None, **kw):
                job["progress"] = {**job["progress"], **kw}
                if msg:
                    job["log"] = (job["log"] + [msg])[-200:]
                self._changed()

            try:
                job["result"] = self.run(job["site"], job["date"], say,
                                         lambda: job["id"] in self.cancel_ids)
                job["state"] = "DONE"
            except Cancelled:
                job["state"] = "CANCELLED"
            except Exception as e:
                job["state"] = "FAILED"
                job["log"] = (job["log"] + [f"{type(e).__name__}: {e}"])[-200:]
            job["finished"] = time.time()
            self.cancel_ids.discard(job["id"])
            self._changed()

    def list(self):
        return list(self.jobs)

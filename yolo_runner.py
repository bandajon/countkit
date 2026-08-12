#!/usr/bin/env python3
"""The portable detector: OpenCV decode + an ultralytics tracking model.

DeepStream ties the real pipeline to a Jetson with JetPack on it. This runner is the
same detector contract over parts that exist on any machine with CUDA (or a CPU and
patience), so the counting logic in engine.py — crossing geometry, debounce, clock
correction, crop keys — can be exercised against real footage anywhere: a laptop, the
RTX rig, a cloud box. Model is still CONFIG, not code (yolo.model in config.yaml).

cv2 and ultralytics are imported inside methods on purpose: importing this module must
work on a machine that has neither, so app.py can dispatch on detector: and the tests
can run without torch. The model loads eagerly at CONSTRUCTION, though — a misconfigured
backend must fail before engine.analyze deletes the previous run's events, not after."""

import os
from pathlib import Path

import engine

MISSING = ("detector: yolo needs ultralytics and OpenCV — pip install ultralytics "
           "opencv-python-headless, or run the countkit yolo Docker image")


def weights_path(name, weights_dir):
    """Bare model names resolve under weights_dir. Ultralytics downloads official
    weights next to wherever you point it, and the default is the working directory —
    inside a container that is an ephemeral layer re-downloaded on every recreate.
    An explicit path is the operator's business and passes through untouched."""
    if not weights_dir or os.sep in name or "/" in name:
        return name
    d = Path(weights_dir)
    d.mkdir(parents=True, exist_ok=True)
    return str(d / name)


def _num(cfg, key, cast, default, ok, why):
    """Config is never quietly substituted: yolo.conf: 0 means 0, and yolo.stride: 0
    is an error rather than a modulo-by-zero twenty minutes into a run."""
    v = cfg.get(key, default)
    try:
        v = cast(v)
    except (TypeError, ValueError):
        raise ValueError(f"yolo.{key}: {v!r} is not a number — {why}")
    if not ok(v):
        raise ValueError(f"yolo.{key}: {v!r} out of range — {why}")
    return v


class YoloDetector:
    """Same contract as engine.MockDetector and DeepStreamDetector: iterating yields
    (segment_file, t seconds into that segment, objects), objects being
    [{"id", "cls", "bbox", "centroid"}] in image pixels.

    One instance per camera, so one tracker: persist=True carries track ids ACROSS
    segment boundaries, which is what keeps a vehicle mid-junction at 07:15:00 from
    being counted twice."""

    def __init__(self, site, date, cam, ingest_root, cfg):
        self.site, self.date, self.cam = site, date, cam
        cfg = cfg or {}
        self.model_name = cfg.get("model", "yolo11s.pt")
        if not self.model_name:
            raise ValueError("yolo.model: empty — name a model, e.g. yolo11s.pt")
        self.weights_dir = cfg.get("weights_dir") or ""
        self.stride = _num(cfg, "stride", int, 3, lambda v: v >= 1,
                           "frames between inferences, a whole number from 1 up")
        self.imgsz = _num(cfg, "imgsz", int, 960, lambda v: v >= 32,
                          "inference size in pixels, 32 or more")
        self.conf = _num(cfg, "conf", float, 0.3, lambda v: 0.0 <= v <= 1.0,
                         "detection confidence floor, between 0.0 and 1.0")
        # None = ultralytics' own choice (CUDA when present, else CPU). Set it where
        # autodetection guesses wrong — e.g. "mps" on an Apple-silicon host.
        self.device = cfg.get("device") or None
        self.frame = None                  # last decoded frame, for crop_for
        d = Path(ingest_root) / date / site / cam
        # Wallclock order: engine.segment_epoch turns each name back into a real time.
        files = sorted(p for p in d.glob("*.mkv") if engine.SEG_NAME.match(p.name))
        self.segments = [{"file": p.name, "duration": None} for p in files]
        self.dir = d
        self.model = self.load_model()

    def load_model(self):
        try:
            import cv2                     # noqa: F401
            from ultralytics import YOLO
        except ImportError:
            raise ValueError(MISSING)
        return YOLO(weights_path(self.model_name, self.weights_dir))

    def __iter__(self):
        import cv2

        for seg in self.segments:
            cap = cv2.VideoCapture(str(self.dir / seg["file"]))
            # finally, not a release at the end: a cancel raised through the yield or a
            # model failure in _objects would otherwise leak the capture and its decoder.
            try:
                fps = cap.get(cv2.CAP_PROP_FPS)
                if not fps or fps != fps or fps <= 0:      # 0, or NaN on a broken header
                    raise ValueError(f'{seg["file"]}: unreadable fps — is this a FieldKit '
                                     "segment?")
                i = 0
                # ponytail: decode every frame and infer on every stride-th. Seeking would
                # skip the decode too, but frame-accurate seeking in mkv is its own bug
                # farm; revisit if the Nano can't keep up.
                while True:
                    ok, frame = cap.read()
                    if not ok:
                        break
                    # Real PTS of the frame just decoded. CAP_PROP_FPS on an NVR's matroska
                    # is a header estimate, and a wrong one mis-bins every event and
                    # stretches the re-cross debounce; index/fps is only the fallback for a
                    # container that reports no position at all.
                    msec = cap.get(cv2.CAP_PROP_POS_MSEC)
                    if i % self.stride == 0:
                        self.frame = frame
                        t = msec / 1000.0 if msec > 0 or i == 0 else i / fps
                        yield seg["file"], t, self._objects(frame)
                    i += 1
            finally:
                cap.release()

    def _objects(self, frame):
        r = self.model.track(frame, persist=True, verbose=False,
                             imgsz=self.imgsz, conf=self.conf, device=self.device)[0]
        b = r.boxes
        # boxes.id is None until the tracker has confirmed tracks — those early frames
        # have detections but nothing engine can follow across a line, so they count
        # as an empty frame rather than as objects with made-up ids.
        if b is None or b.id is None:
            return []
        objs = []
        for tid, cls_i, xyxy in zip(b.id, b.cls, b.xyxy):
            x1, y1, x2, y2 = (float(v) for v in xyxy)
            objs.append({"id": int(tid), "cls": self.model.names[int(cls_i)],
                         "bbox": [x1, y1, x2 - x1, y2 - y1],
                         "centroid": [(x1 + x2) / 2, (y1 + y2) / 2]})
        return objs

    def crop_for(self, obj):
        """Evidence JPEG off the live frame, 15% context each side. Returns None rather
        than raising: engine falls back to the placeholder and the count still lands."""
        import cv2

        try:
            frame = self.frame
            if frame is None:
                return None
            h, w = frame.shape[:2]
            left, top, bw, bh = (float(v) for v in obj["bbox"])
            px, py = bw * 0.15, bh * 0.15
            x1, y1 = max(0, int(left - px)), max(0, int(top - py))
            x2, y2 = min(w, int(left + bw + px)), min(h, int(top + bh + py))
            if x2 <= x1 or y2 <= y1:
                return None
            ok, buf = cv2.imencode(".jpg", frame[y1:y2, x1:x2],
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return buf.tobytes() if ok else None
        except Exception:
            return None


def factory(ingest_root, yolo_cfg):
    return lambda site, date, cam: YoloDetector(site, date, cam, ingest_root, yolo_cfg)

#!/usr/bin/env python3
"""The portable detector: OpenCV decode + an ultralytics tracking model.

DeepStream ties the real pipeline to a Jetson with JetPack on it. This runner is the
same detector contract over parts that exist on any machine with CUDA (or a CPU and
patience), so the counting logic in engine.py — crossing geometry, debounce, clock
correction, crop keys — can be exercised against real footage anywhere: a laptop, the
RTX rig, a cloud box. Model is still CONFIG, not code (yolo.model in config.yaml).

cv2 and ultralytics are imported inside methods on purpose: importing this module must
work on a machine that has neither, so app.py can dispatch on detector: and the tests
can run without torch."""

from pathlib import Path

import engine

MISSING = ("detector: yolo needs ultralytics — pip install ultralytics, or run the "
           "countkit yolo Docker image")


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
        self.model_name = cfg.get("model") or "yolo11s.pt"
        self.stride = int(cfg.get("stride") or 3)
        self.imgsz = int(cfg.get("imgsz") or 960)
        self.conf = float(cfg.get("conf") or 0.3)
        self.model = None                  # loaded on first iteration; injectable
        self.frame = None                  # last decoded frame, for crop_for
        d = Path(ingest_root) / date / site / cam
        # Wallclock order: engine.segment_epoch turns each name back into a real time.
        files = sorted(p for p in d.glob("*.mkv") if engine.SEG_NAME.match(p.name))
        self.segments = [{"file": p.name, "duration": None} for p in files]
        self.dir = d

    def load_model(self):
        try:
            from ultralytics import YOLO
        except ImportError:
            raise ValueError(MISSING)
        return YOLO(self.model_name)

    def __iter__(self):
        import cv2

        if self.model is None:
            self.model = self.load_model()
        for seg in self.segments:
            cap = cv2.VideoCapture(str(self.dir / seg["file"]))
            fps = cap.get(cv2.CAP_PROP_FPS)
            if not fps or fps != fps or fps <= 0:      # 0, or NaN on a broken header
                cap.release()
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
                if i % self.stride == 0:
                    self.frame = frame
                    yield seg["file"], i / fps, self._objects(frame)
                i += 1
            cap.release()

    def _objects(self, frame):
        r = self.model.track(frame, persist=True, verbose=False,
                             imgsz=self.imgsz, conf=self.conf)[0]
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

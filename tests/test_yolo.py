#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_yolo.py — exits non-zero on failure.

Runs with no ultralytics and no torch: a fake model is injected, so what is under test
is OUR code — segment listing, fps/timing maths, stride, id-skipping, object shape,
crop geometry — against a real decoded video."""

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    import cv2                                   # noqa: F401
except ImportError:
    print("skip: no cv2 on this machine (pip install opencv-python-headless)")
    sys.exit(0)
if not shutil.which("ffmpeg"):
    print("skip: no ffmpeg to build the synthetic segment")
    sys.exit(0)

import yolo_runner                               # noqa: E402

FAILS = []
SITE, DATE, CAM = "gerlache", "2026-08-04", "cam1"
SEG = "20260804-070000.mkv"
FPS = 10
TMP = Path(tempfile.mkdtemp(prefix="countkit-yolo-")).resolve()


def check(name, fn):
    try:
        fn()
        print(f"ok   {name}")
    except Exception as e:
        FAILS.append(name)
        print(f"FAIL {name}: {e}")


class FakeBoxes:
    """Shaped like ultralytics Boxes: parallel sequences, id None until tracked."""

    def __init__(self, ids, cls, xyxy):
        self.id, self.cls, self.xyxy = ids, cls, xyxy


class FakeModel:
    """Frame 0 has no track ids yet; every later frame has two tracked boxes."""

    names = {2: "car", 5: "bus"}

    def __init__(self):
        self.calls = []

    def track(self, frame, **kw):
        self.calls.append(kw)
        tracked = len(self.calls) > 1
        boxes = FakeBoxes(
            [7, 8] if tracked else None, [2, 5],
            [[100.0, 200.0, 140.0, 260.0], [300.0, 100.0, 400.0, 180.0]])
        return [type("R", (), {"boxes": boxes})()]


def build():
    d = TMP / "ingest" / DATE / SITE / CAM
    d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size=640x480:rate={FPS}:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / SEG)],
                   check=True)
    # A stray file the runner must ignore: not a FieldKit wallclock name.
    (d / "notes.mkv").write_bytes(b"")
    det = yolo_runner.YoloDetector(SITE, DATE, CAM, TMP / "ingest", {"stride": 5})
    det.model = FakeModel()
    return det


DET = build()
FRAMES = list(DET)


def segments_only_wallclock():
    assert [s["file"] for s in DET.segments] == [SEG], DET.segments
    assert DET.segments[0]["duration"] is None


def empty_dir_is_fine():
    d = TMP / "ingest" / DATE / SITE / "cam9"
    d.mkdir()
    assert yolo_runner.YoloDetector(SITE, DATE, "cam9", TMP / "ingest", {}).segments == []


def stride_and_timing():
    # 2s at 10fps = 20 frames, every 5th -> 4 yields at t = 0.0, 0.5, 1.0, 1.5.
    assert len(FRAMES) == 4, len(FRAMES)
    assert [f[0] for f in FRAMES] == [SEG] * 4
    assert [round(f[1], 3) for f in FRAMES] == [0.0, 0.5, 1.0, 1.5], FRAMES


def untracked_frame_yields_nothing():
    assert FRAMES[0][2] == [], FRAMES[0][2]


def object_shape():
    objs = FRAMES[1][2]
    assert objs == [
        {"id": 7, "cls": "car", "bbox": [100.0, 200.0, 40.0, 60.0],
         "centroid": [120.0, 230.0]},
        {"id": 8, "cls": "bus", "bbox": [300.0, 100.0, 100.0, 80.0],
         "centroid": [350.0, 140.0]}], objs


def model_args_passed():
    kw = DET.model.calls[0]
    assert kw == {"persist": True, "verbose": False, "imgsz": 960, "conf": 0.3}, kw


def crop_is_jpeg():
    b = DET.crop_for({"bbox": [100, 200, 40, 60]})
    assert b and b[:2] == b"\xff\xd8", b[:8] if b else b


def crop_clips_to_frame():
    # Box hanging off the top-left corner still crops what is inside the frame.
    assert DET.crop_for({"bbox": [-50, -50, 100, 100]})[:2] == b"\xff\xd8"


def degenerate_box_is_none():
    assert DET.crop_for({"bbox": [100, 200, 0, 0]}) is None
    assert DET.crop_for({"bbox": [9000, 9000, 20, 20]}) is None
    assert DET.crop_for({"bbox": ["x", 1, 2, 3]}) is None


def no_frame_is_none():
    d = yolo_runner.YoloDetector(SITE, DATE, CAM, TMP / "ingest", {})
    assert d.crop_for({"bbox": [0, 0, 10, 10]}) is None


def unreadable_fps_names_the_file():
    d = TMP / "ingest" / DATE / SITE / "cam8"
    d.mkdir()
    (d / SEG).write_bytes(b"not a video")
    det = yolo_runner.YoloDetector(SITE, DATE, "cam8", TMP / "ingest", {})
    det.model = FakeModel()
    try:
        list(det)
    except ValueError as e:
        assert SEG in str(e) and "fps" in str(e), e
    else:
        raise AssertionError("a non-video segment must not yield frames silently")


def missing_ultralytics_copy():
    # ultralytics is genuinely absent here, which is the case operators hit.
    try:
        import ultralytics                       # noqa: F401
    except ImportError:
        try:
            yolo_runner.YoloDetector(SITE, DATE, CAM, TMP / "ingest", {}).load_model()
        except ValueError as e:
            assert "pip install ultralytics" in str(e), e
            return
        raise AssertionError("missing ultralytics must raise the operator ValueError")
    print("     (ultralytics installed — error-copy check skipped)")


def config_defaults_and_overrides():
    d = yolo_runner.YoloDetector(SITE, DATE, CAM, TMP / "ingest", {})
    assert (d.model_name, d.stride, d.imgsz, d.conf) == ("yolo11s.pt", 3, 960, 0.3)
    d = yolo_runner.YoloDetector(SITE, DATE, CAM, TMP / "ingest",
                                 {"model": "best.pt", "stride": "2", "imgsz": "640",
                                  "conf": "0.5"})
    assert (d.model_name, d.stride, d.imgsz, d.conf) == ("best.pt", 2, 640, 0.5)


def factory_binds_root():
    det = yolo_runner.factory(TMP / "ingest", {})(SITE, DATE, CAM)
    assert [s["file"] for s in det.segments] == [SEG]


check("segments are the wallclock .mkv files, in order", segments_only_wallclock)
check("a camera with no segments is not an error", empty_dir_is_fine)
check("stride skips frames and t stays frame_index/fps", stride_and_timing)
check("boxes without track ids yield an empty frame", untracked_frame_yields_nothing)
check("objects carry id, mapped class, bbox and centroid", object_shape)
check("imgsz/conf/persist reach the model", model_args_passed)
check("crop_for returns a JPEG off the live frame", crop_is_jpeg)
check("a crop hanging off the frame is clipped, not dropped", crop_clips_to_frame)
check("a degenerate or off-frame box returns None", degenerate_box_is_none)
check("crop_for before any frame returns None", no_frame_is_none)
check("an unreadable segment fails loudly, naming the file", unreadable_fps_names_the_file)
check("missing ultralytics gives the install instruction", missing_ultralytics_copy)
check("config defaults and overrides are coerced", config_defaults_and_overrides)
check("factory binds ingest_root and config", factory_binds_root)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

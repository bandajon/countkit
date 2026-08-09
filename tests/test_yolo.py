#!/usr/bin/env python3
"""Plain stdlib test runner: python3 tests/test_yolo.py — exits non-zero on failure.

Runs with no ultralytics and no torch: load_model is patched classwide before any
detector is constructed, so what is under test is OUR code — segment listing, PTS
timing, stride, id-skipping, object shape, config validation, crop geometry — against
a real decoded video."""

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
REAL_LOAD = yolo_runner.YoloDetector.load_model


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


# Construction now loads the model, so the fake goes on the class, not the instance.
yolo_runner.YoloDetector.load_model = lambda self: FakeModel()


def make(cam=CAM, cfg=None):
    return yolo_runner.YoloDetector(SITE, DATE, cam, TMP / "ingest", cfg or {})


def build():
    d = TMP / "ingest" / DATE / SITE / CAM
    d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size=640x480:rate={FPS}:duration=2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / SEG)],
                   check=True)
    # A stray file the runner must ignore: not a FieldKit wallclock name.
    (d / "notes.mkv").write_bytes(b"")
    return make(cfg={"stride": 5})


DET = build()
FRAMES = list(DET)


def segments_only_wallclock():
    assert [s["file"] for s in DET.segments] == [SEG], DET.segments
    assert DET.segments[0]["duration"] is None


def empty_dir_is_fine():
    (TMP / "ingest" / DATE / SITE / "cam9").mkdir()
    assert make("cam9").segments == []


def stride_and_timing():
    # 2s at 10fps = 20 frames, every 5th -> 4 yields. t comes from each frame's PTS,
    # which on this CFR clip agrees with index/fps to the millisecond.
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


def zero_conf_reaches_the_model():
    det = make(cfg={"conf": 0, "stride": 20})
    assert det.conf == 0.0
    assert list(det)[0][2] == []                 # first frame: tracker has no ids yet
    assert det.model.calls[0]["conf"] == 0.0, det.model.calls[0]


def bad_config_is_refused():
    for cfg, word in (({"stride": 0}, "stride"), ({"stride": "0"}, "stride"),
                      ({"imgsz": 16}, "imgsz"), ({"conf": 1.5}, "conf"),
                      ({"conf": "loose"}, "conf"), ({"model": ""}, "model")):
        try:
            make(cfg=cfg)
        except ValueError as e:
            assert f"yolo.{word}" in str(e), (cfg, str(e))
        else:
            raise AssertionError(f"{cfg} must not be silently replaced by a default")


def config_defaults_and_overrides():
    d = make()
    assert (d.model_name, d.stride, d.imgsz, d.conf) == ("yolo11s.pt", 3, 960, 0.3)
    d = make(cfg={"model": "best.pt", "stride": "2", "imgsz": "640", "conf": "0.5"})
    assert (d.model_name, d.stride, d.imgsz, d.conf) == ("best.pt", 2, 640, 0.5)


def weights_dir_holds_bare_names():
    wd = TMP / "models"
    assert yolo_runner.weights_path("yolo11s.pt", wd) == str(wd / "yolo11s.pt")
    assert wd.is_dir(), "the download target must exist before ultralytics writes to it"
    # An explicit path, and the no-weights_dir case, pass through untouched.
    assert yolo_runner.weights_path("/opt/w/best.pt", wd) == "/opt/w/best.pt"
    assert yolo_runner.weights_path("sub/best.pt", wd) == "sub/best.pt"
    assert yolo_runner.weights_path("yolo11s.pt", "") == "yolo11s.pt"


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
    assert make().crop_for({"bbox": [0, 0, 10, 10]}) is None


def unreadable_fps_names_the_file():
    d = TMP / "ingest" / DATE / SITE / "cam8"
    d.mkdir()
    (d / SEG).write_bytes(b"not a video")
    try:
        list(make("cam8"))
    except ValueError as e:
        assert SEG in str(e) and "fps" in str(e), e
    else:
        raise AssertionError("a non-video segment must not yield frames silently")


def missing_ultralytics_fails_at_construction():
    # Eager load is the point: engine constructs detectors before it deletes the
    # previous run's events, so a missing backend must raise here, not mid-analysis.
    yolo_runner.YoloDetector.load_model = REAL_LOAD
    try:
        import ultralytics                       # noqa: F401
        print("     (ultralytics installed — error-copy check skipped)")
    except ImportError:
        try:
            make()
        except ValueError as e:
            assert "pip install ultralytics" in str(e), e
        else:
            raise AssertionError("missing ultralytics must raise at construction")
    finally:
        yolo_runner.YoloDetector.load_model = lambda self: FakeModel()


def the_capture_is_released_on_every_exit():
    # A cancelled analysis closes the generator mid-segment and a model failure raises
    # through it; either leaking the capture holds the decoder open for the whole run.
    real, released = cv2.VideoCapture, []

    class Watched:
        def __init__(self, path):
            self.cap = real(path)

        def __getattr__(self, n):
            return getattr(self.cap, n)

        def release(self):
            released.append(1)
            self.cap.release()

    cv2.VideoCapture = Watched
    try:
        it = iter(make(cfg={"stride": 5}))
        next(it)
        it.close()                                  # what a cancelled job does
        assert released == [1], "closing the iterator mid-segment leaked the capture"

        det = make(cfg={"stride": 5})
        det.model.track = lambda *a, **kw: 1 / 0     # CUDA OOM, in effect
        try:
            list(det)
        except ZeroDivisionError:
            pass
        assert len(released) == 2, "a model failure leaked the capture"
    finally:
        cv2.VideoCapture = real


def factory_binds_root():
    det = yolo_runner.factory(TMP / "ingest", {})(SITE, DATE, CAM)
    assert [s["file"] for s in det.segments] == [SEG]


check("segments are the wallclock .mkv files, in order", segments_only_wallclock)
check("a camera with no segments is not an error", empty_dir_is_fine)
check("stride skips frames and t is the frame's own PTS", stride_and_timing)
check("boxes without track ids yield an empty frame", untracked_frame_yields_nothing)
check("objects carry id, mapped class, bbox and centroid", object_shape)
check("imgsz/conf/persist reach the model", model_args_passed)
check("conf: 0 reaches the model as 0.0, not the default", zero_conf_reaches_the_model)
check("out-of-range config is refused, never defaulted", bad_config_is_refused)
check("config defaults and overrides are coerced", config_defaults_and_overrides)
check("a bare model name downloads into weights_dir", weights_dir_holds_bare_names)
check("crop_for returns a JPEG off the live frame", crop_is_jpeg)
check("a crop hanging off the frame is clipped, not dropped", crop_clips_to_frame)
check("a degenerate or off-frame box returns None", degenerate_box_is_none)
check("crop_for before any frame returns None", no_frame_is_none)
check("an unreadable segment fails loudly, naming the file", unreadable_fps_names_the_file)
check("missing ultralytics fails at construction", missing_ultralytics_fails_at_construction)
check("factory binds ingest_root and config", factory_binds_root)
check("the capture is released on cancel and on model failure",
      the_capture_is_released_on_every_exit)

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'all passed'}")
sys.exit(1 if FAILS else 0)

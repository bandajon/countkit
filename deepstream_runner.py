#!/usr/bin/env python3
"""The real detector: DeepStream nvinfer + nvdcf tracker, driven through pyds.

Isolated on purpose. Nothing here can be imported or tested off the Jetson, so it
holds only the pipeline plumbing — every decision that affects a count (crossing
geometry, debounce, clock correction, crop keys, idempotency) lives in engine.py and
runs identically behind MockDetector. Wiring this in is a one-line swap of the
detector factory once JetPack is on the box.

The model is CONFIG, not code: nvinfer_config in config.yaml points at the
TrafficCamNet ONNX now and the fine-tuned RT-DETR later, and at the RTX rig's own
config with no change here (PRD acceptance #10)."""

from pathlib import Path

import engine

MISSING = ("DeepStream not available on this machine — install JetPack, or leave "
           "nvinfer_config empty to replay fixtures with the mock detector")


def available():
    try:
        import gi                        # noqa: F401
        import pyds                      # noqa: F401
        return True
    except ImportError:
        return False


class DeepStreamDetector:
    """Same contract as engine.MockDetector: iterating yields (segment_file, t,
    objects) with objects as [{"id", "cls", "bbox", "centroid"}] in image pixels.

    filesrc(segments in order) -> qtdemux -> h264parse -> nvv4l2decoder -> nvstreammux
    -> nvinfer(nvinfer_config) -> nvtracker(nvdcf) -> fakesink, with a pad probe on
    the tracker src pad turning NvDsObjectMeta into the dicts above."""

    def __init__(self, site, date, cam, ingest_root, nvinfer_config):
        if not available():
            raise RuntimeError(MISSING)
        self.site, self.date, self.cam = site, date, cam
        self.config = nvinfer_config
        d = Path(ingest_root) / date / site / cam
        # Wallclock order is the whole point: engine.segment_epoch turns each name
        # back into the true start time of its first frame.
        files = sorted(p for p in d.glob("*.mkv") if engine.SEG_NAME.match(p.name))
        self.segments = [{"file": p.name, "duration": None} for p in files]
        self.dir = d

    def __iter__(self):
        import gi
        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
        import pyds

        Gst.init(None)
        for seg_index, seg in enumerate(self.segments):
            out = []          # the probe appends; the pipeline runs to EOS per segment
            pipe = Gst.parse_launch(
                f'filesrc location="{self.dir / seg["file"]}" ! matroskademux ! '
                "h264parse ! nvv4l2decoder ! m.sink_0 nvstreammux name=m batch-size=1 "
                "width=1920 height=1080 ! "
                f'nvinfer config-file-path="{self.config}" ! '
                "nvtracker ll-lib-file=/opt/nvidia/deepstream/deepstream/lib/"
                "libnvds_nvmultiobjecttracker.so ! fakesink name=sink sync=0")

            def probe(pad, info):
                batch = pyds.gst_buffer_get_nvds_batch_meta(hash(info.get_buffer()))
                fl = batch.frame_meta_list
                while fl:
                    fm = pyds.NvDsFrameMeta.cast(fl.data)
                    # Gates are calibrated in the camera's NATIVE pixels, but nvstreammux
                    # scaled every frame to 1920x1080 — scale the boxes back, or every
                    # count on a non-1080p camera uses displaced gates. (0 = unknown.)
                    sx = (fm.source_frame_width or 1920) / 1920
                    sy = (fm.source_frame_height or 1080) / 1080
                    objs, ol = [], fm.obj_meta_list
                    while ol:
                        om = pyds.NvDsObjectMeta.cast(ol.data)
                        r = om.rect_params
                        left, top = r.left * sx, r.top * sy
                        w, h = r.width * sx, r.height * sy
                        # The tracker genuinely restarts with each segment's pipeline, so
                        # ids recycle; cross-segment continuity would be fictional. Give
                        # engine's per-id state a fresh id space per segment.
                        objs.append({"id": seg_index * 1_000_000_000 + om.object_id,
                                     "cls": om.obj_label,
                                     "bbox": [left, top, w, h],
                                     "centroid": [left + w / 2, top + h / 2]})
                        ol = ol.next
                    # pts is nanoseconds from the segment start — engine adds the
                    # segment's wallclock epoch and the camera's clock offset.
                    out.append((seg["file"], fm.buf_pts / 1e9, objs))
                    fl = fl.next
                return Gst.PadProbeReturn.OK

            pipe.get_by_name("sink").get_static_pad("sink").add_probe(
                Gst.PadProbeType.BUFFER, probe)
            pipe.set_state(Gst.State.PLAYING)
            # Bounded, and the message is read: a broken pipeline (wrong nvinfer config,
            # h265 footage) otherwise yields zero frames and the run "succeeds" with no
            # events after wiping the previous one, and a stall would wedge the
            # one-at-a-time queue forever. Both must fail the job instead.
            msg = pipe.get_bus().timed_pop_filtered(
                15 * 60 * Gst.SECOND, Gst.MessageType.EOS | Gst.MessageType.ERROR)
            pipe.set_state(Gst.State.NULL)
            if msg is None:
                raise RuntimeError(f"{seg['file']}: DeepStream produced no end-of-stream "
                                   "in 15 minutes — pipeline stalled")
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                raise RuntimeError(f"{seg['file']}: DeepStream pipeline failed — "
                                   f"{err.message} ({debug})")
            for frame in out:
                yield frame

    # ponytail: crops here should come off the NvBufSurface for this frame rather than
    # engine's placeholder — one map/encode call in the probe, wired when the box exists.


def factory(ingest_root, nvinfer_config):
    return lambda site, date, cam: DeepStreamDetector(
        site, date, cam, ingest_root, nvinfer_config)

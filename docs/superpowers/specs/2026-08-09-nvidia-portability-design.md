# CountKit on any NVIDIA box — design

**Date:** 2026-08-09 · **Status:** approved (design), pending spec review

## Goal

Run CountKit's real-detector path seamlessly on three targets, for imminent
testing: the Jetson Orin Nano (PRD primary), the RTX 5080 x86 rig (PRD
acceptance #10), and any other NVIDIA-equipped server. "Seamless" = host needs
only the NVIDIA driver + container toolkit; first run works without hand-built
models.

Scope decision (user): build **both** the portable YOLO backend and the
DeepStream container packaging now.

## Constraints found during research

- RTX 5080 is Blackwell: native DeepStream support starts at **DeepStream
  9.x** (CUDA 12.8+). The 7.x containers cannot drive it.
- Orin Nano on JetPack 6.x pairs with the **DeepStream 7.1**
  `triton-multiarch` container — the supported combination.
- So the DeepStream path necessarily spans two major versions; Docker
  profiles absorb that. All counting logic already lives in `engine.py`
  behind the detector seam and is untouched by this work.

## 1. Detector selection

`config.yaml` gains `detector: mock | yolo | deepstream`.

- Key absent → today's behaviour exactly (`nvinfer_config` set → deepstream,
  else mock). No migration needed.
- A selected backend that is unavailable (missing import, missing model)
  **fails the job loudly** with operator-facing install copy, via the
  existing `ValueError` convention. CountKit never silently degrades to a
  different detector.
- `app.detector()` is the only dispatch point.

## 2. `yolo_runner.py` — portable backend (~100 lines)

Same contract as `MockDetector` / `DeepStreamDetector`: `.segments` list and
iteration yielding `(segment_file, t_seconds, objects)` with objects as
`[{"id", "cls", "bbox", "centroid"}]` in image pixels.

- OpenCV `VideoCapture` over the wallclock-named `.mkv` segments in order.
- ultralytics `model.track(frame, persist=True)` — ByteTrack gives stable
  per-camera track ids (what tier-1 pairing needs).
- `t = frame_index / fps` (fps read from the container; unreadable fps is an
  error naming the file).
- Frame stride configurable (`yolo.stride`, default tuned for ~10 effective
  fps) so the Nano keeps up.
- Model from `yolo.model` (default `yolo11s.pt`), auto-downloaded by
  ultralytics into a cached volume. COCO labels (car, bus, truck, motorcycle,
  bicycle) flow through the existing `classes` mapping table; unmapped labels
  already count as `other` in QA. `config.example.yaml` gains commented COCO
  rows.
- `ultralytics`/`torch` stay **out of** `requirements.txt` (optional, like
  boto3); the Docker images carry them. Bare-metal users get the install hint
  from the loud failure.
- No GPU → ultralytics runs CPU; allowed, with a progress-log line saying so
  (slow is acceptable for a smoke test; wrong is not).

### Evidence-crop seam

Detector protocol gains one **optional** method: `crop_for(obj) -> bytes`
(JPEG of the object's bbox from the current frame). `engine.analyze` calls it
only at crossing time and falls back to `PLACEHOLDER_JPEG` when the detector
doesn't provide it. The YOLO backend implements it by cropping the last
decoded frame; the DeepStream runner's existing `ponytail:` note (crops off
`NvBufSurface`) plugs into the same seam later. Crop keys, the async
`CropWriter`, and R2 offload are unchanged.

## 3. Docker packaging

`docker-compose.yaml` at the repo root with four profiles; each service
mounts `config.yaml`, `./ingest`, `./data` (paths overridable by env),
exposes 8090, and requests the GPU (`gpus: all`; `runtime: nvidia` on
Jetson).

| Profile | Base image | Runs on |
|---|---|---|
| `yolo` | official ultralytics GPU image | any x86 NVIDIA server incl. RTX 5080 |
| `yolo-jetson` | ultralytics JetPack 6 image | Orin Nano |
| `ds-x86` | `nvcr.io/nvidia/deepstream:9.x` (exact tag pinned at implementation) | x86 incl. Blackwell |
| `ds-jetson` | `nvcr.io/nvidia/deepstream:7.1-triton-multiarch` | Orin Nano (JetPack 6) |

- Two Dockerfiles (`docker/Dockerfile.yolo`, `docker/Dockerfile.deepstream`)
  parameterised by base image; each adds `requirements.txt` + `ffmpeg`
  (reference frames need it) + for DeepStream, the `pyds` wheel matching the
  container's version.
- `docker/fetch_model.sh` (run automatically on first DeepStream boot):
  downloads TrafficCamNet from NGC (public artifact), writes labels file,
  and generates the nvinfer config from a template with container-correct
  paths. Overriding `nvinfer_config` in `config.yaml` to point at your own
  model skips all of it — the model stays config, not code.
- Model/weights caches live in named volumes so re-creates don't re-download.

## 4. Error handling

- Backend unavailable, model fetch failed, unreadable fps, DeepStream absent:
  all surface as FAILED jobs whose log carries the operator-facing copy
  (existing convention — messages are shown verbatim in the UI).
- The corridor page, R2 offload, and probe join are untouched; they already
  degrade independently.

## 5. Testing

House style: stdlib assert runners, no framework.

- `tests/test_yolo.py`: generates a tiny synthetic clip with ffmpeg, runs the
  backend on CPU, asserts the iterator contract (segment order, timing math,
  object dict shape, stable ids) and the `crop_for` seam. Skips gracefully
  with a printed reason when ultralytics isn't installed — same pattern as
  the DeepStream guarded-seam asserts in the acceptance suite.
- Engine-level check (in `tests/test_engine.py`): a detector offering
  `crop_for` gets real bytes written; one that doesn't still gets the
  placeholder.
- `docker/smoke.sh <profile>`: builds the image, boots it, polls
  `/api/status`, runs a fixture site-day through the real job queue, and
  asserts events landed. This is the "does it run on this box" test for each
  target.
- Full on-hardware validation stays `tests/test_acceptance.py`, which already
  runs anywhere.

## Out of scope

- Fine-tuned RT-DETR model (later, via the same `nvinfer_config` /
  `yolo.model` keys).
- Real crops from the DeepStream runner (seam is ready; wiring is the
  existing `ponytail:` follow-up).
- Multi-GPU / multi-job concurrency (the one-job queue is a product
  decision, not a limitation).

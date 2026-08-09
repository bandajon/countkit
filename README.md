# CountKit

Junction analysis & reporting on the Jetson — companion to
[FieldKit](https://github.com/bandajon/fieldkit). FieldKit records the
junction; CountKit counts it: per-approach, per-class vehicle counts in
15-minute bins, turning-movement matrices with per-vehicle evidence imagery,
exported as Excel + PDF.

Spec: `docs/COUNTKIT_PRD.md` · UI design: `docs/COUNTKIT_DESIGN_PROMPT.md` ·
probe-data licensing research: `docs/CONGESTION_FUSION_RESEARCH.md`.

## Run

```
pip install -r requirements.txt
python app.py            # http://<host>:8090
python3 tests/test_acceptance.py   # the PRD acceptance list, end to end
```

First boot copies `config.example.yaml` to `config.yaml`. Point `ingest_root`
at the tree FieldKit's nvr_pull produces (`<date>/<site>/<cam>/*.mkv` with a
`.verified` marker). Off-Jetson, analysis replays `fixtures/`; on the Orin,
set `nvinfer_config` and the same engine drives DeepStream
(`deepstream_runner.py`). The RTX 5080 rig is that one config key, not a port.

## Run in Docker

The host needs only the NVIDIA driver and the container toolkit — the detector
stack is in the image.

| Profile | Hardware |
| --- | --- |
| `yolo` | any x86 NVIDIA GPU, incl. RTX 5080 (Blackwell) |
| `yolo-jetson` | Orin Nano, JetPack 6 |
| `ds-x86` | x86 via DeepStream 9.1 (native Blackwell) |
| `ds-jetson` | Orin Nano via DeepStream 7.1 (the JetPack 6 pairing) |

```
cp config.example.yaml config.yaml && mkdir -p ingest data
docker compose --profile yolo up -d          # http://<host>:8090
docker/smoke.sh yolo                         # build, run the suite in-image, boot, poll /api/status
```

Set `detector: yolo` in `config.yaml` for the yolo profiles. The `ds-*` profiles
set up TrafficCamNet on boot and print the `nvinfer_config:` line to paste into
`config.yaml`.

## The four tabs

**Label** — draw ENTRY/EXIT gates over a camera's reference frame; the chevron
points the way counted traffic travels. Calibrations are versioned and never
overwritten (a re-aimed camera gets a new version). The site arm map above the
canvas judges the whole junction: every arm's entry and exit must be owned by
exactly one camera — unowned arms can't be counted, double-owned arms count
vehicles twice.

**Analyze** — queue verified site-days; one job at a time (the Orin is small);
the queue survives restarts and re-runs are idempotent. A camera whose clock
offset is unset **blocks** the site-day: unset is not zero. Offsets live in
the footage manifest (seconds to ADD to reach true Africa/Lusaka time — a
camera left on China time needs `-21600`).

**Counts** — read-only by design. QA sits above the numbers: the pairing rate
split into same-camera and cross-camera-inferred tiers (tier 2 pairs by class
+ transit time across cameras — no visual re-ID), coverage with gaps hatched
(a gap is never a zero), offsets, probe sample sizes. The verification drawer
shows each movement's evidence crops side by side — inferred pairs first,
that's what they're for. Flagging records a QA note; nothing anywhere edits a
count.

**Report** — Excel workbook + PDF with the junction arrow diagram (arrows
kerb-first for left-hand traffic), bundled with a sha256 manifest. Gap bins
export as the literal `no footage`.

## Evidence crops, R2 and privacy

Every crossing event carries a ~10–25 KB crop, written locally by an async
thread that never stalls inference. With `r2.enabled`, crops upload to
Cloudflare R2 (sha256-verified); the local copy is deleted **only** after a
verified upload *and* only under disk pressure (`r2.min_free_gb`). Set
`r2.cdn_base` and the UI falls back to the CDN for offloaded crops. Crops
contain readable plates: they are internal QA material under the Zambia Data
Protection Act — client-facing export is an explicit opt-in.

## Probe data (corroboration only)

`probe.dataset` points at a CSV (`site,arm,bin_start_iso,delay_s,speed_kmh,sample_n`)
from a licensed provider (TomTom). It renders as a secondary series with its
sample size, never merged into counts. Google traffic data is never stored —
see the research doc for why.

## Corridor map (optional)

`/corridor` shows a live congestion glance for a configured corridor using
Google Routes `speedReadingIntervals` — display-only, attributed, held in
memory for 60 s and never written to disk (Google ToS). Set
`google_routes.api_key` (restrict the key by referrer + API in the Google
console) and the `corridor:` block. Leaflet is vendored under
`static/vendor/`; if map tiles are unreachable the page falls back to a
schematic view.

## Tests

`for t in tests/test_*.py; do python3 $t; done` — plain stdlib assert
runners, no framework. `tests/test_acceptance.py` maps one-to-one onto the
PRD's acceptance list.

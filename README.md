# CountKit

Junction analysis & reporting on the Jetson — companion to
[FieldKit](https://github.com/bandajon/fieldkit). FieldKit records the
junction; CountKit counts it: per-approach, per-class vehicle counts in
15-minute bins, turning-movement matrices with per-vehicle evidence imagery,
exported as Excel + PDF.

Spec: `docs/COUNTKIT_PRD.md` · UI design: `docs/COUNTKIT_DESIGN_PROMPT.md` ·
probe-data licensing research: `docs/CONGESTION_FUSION_RESEARCH.md`.

## From download to your first count

The whole path, in order. Docker is the recommended way to run on a Jetson
(the GPU stack ships in the image); the bare-metal notes are inline where the
two differ.

**1 · Install.**

```
git clone https://github.com/bandajon/countkit.git && cd countkit
docker/smoke.sh yolo-jetson              # Docker path — see "First boot on a Jetson"
```

Bare metal instead: `pip install -r requirements.txt`, and for real detection
also `pip install ultralytics opencv-python-headless`. On a Jetson that pip
torch is CPU-only — analysis works but is many times slower than the
`yolo-jetson` image, which carries NVIDIA's CUDA torch. Fine for a first
short test; use the image for real site-days.

**2 · Point it at the footage drive.** Mount the drive (by UUID — see the
fstab block below), then:

- Docker: a `.env` file next to `docker-compose.yaml`:
  `INGEST_DIR=/media/<drive>/ingest` and `DATA_DIR=/media/<drive>/countkit-data`.
  Leave the paths in `config.yaml` alone.
- Bare metal: absolute paths directly in `config.yaml` —
  `ingest_root: /media/<drive>/ingest`, `data_root: /media/<drive>/countkit-data`.
  A `data_root` change needs an app restart.

**3 · Lay the footage out.** Analysis reads
`<ingest_root>/<date>/<site>/<cam>/*.mkv` — e.g.
`/media/ssd/ingest/2026-08-11/gerlache/cam1/20260811-070000.mkv`. FieldKit's
nvr_pull produces this tree ready-made. Footage from anywhere else, build it
by hand:

- One folder per camera; each video segment named by the **wallclock time of
  its first frame**: `YYYYMMDD-HHMMSS.mkv` (H.264 in Matroska — what NVRs and
  FieldKit record). The filename IS the timestamp; nothing else supplies it.
- `manifest.json` beside the camera folders:
  `{"time_offset_s": {"cam1": 0.0, "cam2": 0.0}}` — seconds to ADD to reach
  true Africa/Lusaka time. Write `0.0` explicitly when the filenames are
  already true local time: an absent camera **blocks** analysis, because
  unset is not zero.
- `touch .verified` beside the manifest, last — it marks the site-day
  complete and analyzable.

No drive at the box? `r2_ingest.py push/pull` moves the same tree through the
R2 bucket — see "Where the footage comes from".

**4 · Pick the detector.** In `config.yaml`: `detector: yolo` (the `yolo:`
block tunes model/stride/imgsz/conf; defaults are sensible). The example
ships `detector: ""` = mock, which replays test fixtures and will fail on
real footage. DeepStream is the other real backend — see the `ds-*` notes.

**5 · Start it.** `docker compose --profile yolo-jetson up -d` (or bare
metal: `python3 app.py`), then open `http://<nano>:8090`.

**6 · Label** — per camera: grab the reference frame, draw ENTRY and EXIT
gates, name each gate's arm, check the chevron points the way counted
traffic travels. The arm map above the canvas must show every arm **owned**
— unowned arms can't count, double-owned arms count twice.

**7 · Analyze** — the site-day appears once `.verified` exists; unset clock
offsets block it (set them in Counts → Offsets or in the manifest). Queue
it and watch progress. The first yolo run downloads the model weights
(~20 MB, once, into `data/models/`) — the box needs internet that one time,
or copy a `.pt` there yourself.

**8 · Counts** — QA first (pairing tiers, coverage with gaps hatched,
offsets), then bins, O-D matrix, and the verification drawer with per-vehicle
evidence crops. Read-only by design; flag anything doubtful.

**9 · Report** — export the Excel + PDF bundle with its sha256 manifest.
Figures summed over no-footage bins are starred, never silently zeroed.

## Run

```
pip install -r requirements.txt
python app.py            # http://<host>:8090
python3 tests/test_acceptance.py   # the PRD acceptance list, end to end
```

First boot copies `config.example.yaml` to `config.yaml`. Point `ingest_root`
at the tree FieldKit's nvr_pull produces (`<date>/<site>/<cam>/*.mkv` with a
`.verified` marker) — an absolute path reaches an external drive. Real
detection bare-metal needs `pip install ultralytics opencv-python-headless`
and `detector: yolo`; with `detector` empty, analysis replays `fixtures/`.
On the Orin, set `nvinfer_config` and the same engine drives DeepStream
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
docker/smoke.sh yolo                 # build, suite in-image, boot, poll /api/status, down again
docker compose --profile yolo up -d  # http://<host>:8090
```

One profile at a time — they all publish 8090.

Smoke first, then up. Smoke tears down only a stack it started itself: find the
service already running and it is left untouched and merely polled, because both
`down` and a post-build `up -d` would kill a live analysis. It also creates
`config.yaml` (from the example) plus `ingest/` and `data/` when they're
missing. Going straight to `up` instead, create them yourself first — the bind
mounts refuse to invent a missing source, so `up` fails outright rather than
quietly filling a root-owned directory on the wrong disk.

Set `detector: yolo` in `config.yaml` for the yolo profiles. The `ds-*` profiles
set up TrafficCamNet on boot and print an `nvinfer_config:` line — paste it into
`config.yaml` **and** set `detector: deepstream`:

```
detector: deepstream
nvinfer_config: /countkit/data/models/trafficcamnet/config_infer_trafficcamnet.txt
```

The example ships both keys empty, which selects the mock detector; the first
Analyze then dies on the `fixtures/` tree, which is not in the image.

### First boot on a Jetson

JetPack 6 ships Docker with the NVIDIA runtime; what's usually missing is the
compose plugin (`sudo apt install docker-compose-plugin`) and your user in the
`docker` group (`sudo usermod -aG docker $USER`, then log out and in). Then:

```
git clone https://github.com/bandajon/countkit.git && cd countkit
docker/smoke.sh yolo-jetson          # suite output, then the /api/status JSON = healthy
```

Set `detector: yolo` in `config.yaml` and `up -d`. The first analysis downloads
the model weights (~20 MB, once, into `data/models/` — so a field box needs a
connection that once, or copy the `.pt` file there yourself). When something is
off, `docker compose --profile yolo-jetson logs` is where it says so.

### Where the footage comes from

`docker-compose.yaml` mounts `./ingest` and `./data` from the checkout. Footage
on an external drive: put `INGEST_DIR=/media/<drive>/ingest` (and
`DATA_DIR=/media/<drive>/countkit-data` — crops are the bulky part) in a `.env`
file next to the compose file. Leave `ingest_root: ./ingest` in `config.yaml`
alone; inside the container the path doesn't change. (Bare metal has no
mounts: absolute `ingest_root`/`data_root` paths in `config.yaml` do the same
job.)

Mount that drive by UUID (`/dev/sda1` moves between boots) and before Docker
wants it. `blkid` prints the UUID; in `/etc/fstab`:

```
UUID=<uuid>  /media/ssd  ext4  defaults,nofail,x-systemd.automount  0  2
```

`nofail` keeps the box booting with the drive out, `x-systemd.automount` mounts
on first access instead of blocking boot. If the drive is absent anyway the `up`
fails loudly and nothing is written to the boot device. To recover: mount the
drive, then `docker compose --profile <p> up -d` again — a container that
started before the mount cannot see it and must be restarted.

No drive at the box? The R2 bucket doubles as footage transport. On the machine
that has the footage, `python3 r2_ingest.py push <date> <site>`; on the
analyzing box, `pull` the same site-day (in Docker:
`docker compose --profile yolo-jetson run --rm countkit-yolo-jetson python3
r2_ingest.py pull <date> <site>`). Needs the `r2:` credentials in `config.yaml`;
`list` shows what the bucket holds. The `.verified` marker travels last in both
directions, so a half-arrived site-day never analyzes.

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

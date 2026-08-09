# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CountKit: junction traffic analysis & reporting on a Jetson Orin. FieldKit (sibling project) records the junction; CountKit counts it — per-approach, per-class vehicle counts in 15-minute bins, turning-movement matrices with per-vehicle evidence crops, exported as Excel + PDF. Spec: `docs/COUNTKIT_PRD.md`.

## Commands

```sh
pip install -r requirements.txt
python app.py                              # serve on http://<host>:8090 (PORT env overrides)
python3 tests/test_engine.py               # run one test file
for t in tests/test_*.py; do python3 $t; done   # run all
python3 tests/test_acceptance.py           # end-to-end; maps 1:1 to the PRD acceptance list
python3 offload.py                         # offload.py's inline self-check
```

Tests are plain stdlib assert runners — no pytest, no framework, exit non-zero on failure. Each test file sets `COUNTKIT_ROOT` to a tmpdir **before** importing `app`, giving a throwaway install; follow that pattern in new tests. `boto3` and DeepStream (`pyds`) are intentionally absent from requirements: boto3 only when `r2.enabled`, pyds comes from the JetPack image.

## Architecture

Single FastAPI process (`app.py`, HTTP layer only — all logic lives in the modules it imports), static single-page UI in `static/index.html`, four tabs: Label → Analyze → Counts → Report.

Data flow: FieldKit's nvr_pull lands footage at `<ingest_root>/<date>/<site>/<cam>/*.mkv` (with `manifest.json` + `.verified` marker per site-day) → `engine.analyze()` turns detector output into gate-crossing **events** in SQLite (`data/waves/<site>.db`, one DB per site) plus evidence crops → `aggregate.py` derives everything else (bins, pairing, QA, peaks) **read-only at request time** → `report.py` builds the Excel/PDF/sha256 bundle.

- `engine.py` — the core. Detector **interface** (iterate → `(segment_file, t_seconds, objects)`), crossing geometry, re-cross debounce, clock correction, async `CropWriter`, `Jobs` queue (one at a time, persisted to `jobs.json`, survives restart). `MockDetector` replays `fixtures/`; because all counting logic lives here, a fixture test genuinely tests Orin behaviour.
- `deepstream_runner.py` — the real detector, deliberately import-isolated: nothing that affects a count lives here. Selected purely by config: `nvinfer_config` set → DeepStream; empty → mock. The RTX 5080 rig is that one config key, not a port.
- `calib.py` — versioned gate calibrations per site/cam (`v1.json`, `v2.json`… + `active.json` pointer) and the site-level arm-ownership map. `app.py` repoints `calib.DATA_ROOT` at startup.
- `aggregate.py` — two-tier movement pairing (tier 1: same-camera tracker id; tier 2: cross-camera by class + transit time, no visual re-ID, always reported separately), coverage/gaps, probe-CSV join, QA flags (JSONL under `data/flags/`).
- `offload.py` — optional R2 upload sweep for crops.
- `report.py` — Excel + LHT junction-diagram PDF + sha256 manifest.

Segment filenames (`YYYYMMDD-HHMMSS.mkv`) are wallclock and load-bearing: `segment_epoch()` + per-camera clock offset from the manifest = true event time.

## Invariants — do not soften these

- **Unset is not zero.** A camera without a clock offset in `manifest.json` **blocks** analysis of the whole site-day (`engine.check_ready`). Offsets are seconds to ADD to reach Africa/Lusaka time.
- **A gap is never a zero.** Missing footage renders/exports as `no footage`, not 0.
- **Nothing edits a count.** Counts tab is read-only; QA flags are notes; the remedy for bad data is re-running analysis. Hand-edited numbers destroy the warranty argument.
- **Calibrations are never overwritten.** Every save is a new version; a re-aimed camera must not retro-change yesterday's counts.
- **Analysis is idempotent.** A run deletes its own (site, date) events and crops first; re-run after crash/restart is always safe. The job queue relies on this.
- **Crops are evidence.** Local copy deleted only after a sha256-verified R2 upload AND under disk pressure (`r2.min_free_gb`). Crops contain readable plates — internal QA material under the Zambia Data Protection Act; client export is explicit opt-in.
- **Google Routes data is never stored** — 60 s in-memory only, never on disk, never joined to counts (ToS). Storable correlation uses the licensed TomTom probe CSV, which is corroboration only, never a second count.
- **Config text is written verbatim** — no yaml round-trip, it would strip operator comments.

## Conventions

- Time: Africa/Lusaka fixed UTC+2, no DST (`aggregate.CAT`). Bins are clock quarters via epoch floor to 900 s.
- Errors: modules raise `ValueError` (operator error → 400) and `LookupError` (absent data → 404) with messages the UI shows **verbatim** — write them as operator-facing copy.
- Unmapped detector classes count as `"other"` and surface in QA — never silently dropped.
- FastAPI routes match in declaration order: `/api/calib/{site}/armmap` must stay declared before `/api/calib/{site}/{cam}`.
- `ponytail:` comments mark deliberate simplifications with a known ceiling and upgrade path — keep the convention.
- Left-hand traffic throughout (report arrows kerb-first, `turn_of()` in report.py).

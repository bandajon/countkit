# Manual movement pairing + ambiguity-gated tier-2

2026-08-12. Design approved in conversation with Jonah; implementation
scheduled next session. Companion roadmap items (DeepStream setup, VLM
refiner pilot) are at the end — designed at shape level only.

## Problem

Turning movements come from pairing an entry event with an exit event.
Tier-2 (cross-camera) pairing matches by report class + transit time and
assigns greedily by smallest dt — with two similar vehicles in flight it
makes confident wrong matches (observed live: a red-box tuk-tuk paired with
a brown van in one row and a white Hilux in the next). 99.5% of kalambo's
pairs are tier-2. Humans reviewing the movement list can see correct matches
the heuristic missed; today they have no way to record that judgment.

## Decisions (made with Jonah)

- **Spot-fix workflow**, not a full verification queue: reviewers fix pairs
  they catch while browsing. The overlay design leaves room for a queue
  panel later; none is built now.
- **Manual pairs fold into the observed numbers in exports** (no third
  per-cell annotation). Auditability is preserved by (a) the overlay file
  itself — every manual action is a timestamped row on disk — and (b) one
  disclosure line in the Method sheet: "N movements manually paired /
  M unpaired by reviewer". QA's pairing block also reports manual counts.
- Tier-2 gets an **ambiguity gate**: fewer inferred pairs, truthful ones.

## Part 1 — Ambiguity gate in `aggregate.pair()`

Tier-2 currently sorts all candidates by dt ascending and assigns greedily.
Change: a candidate (entry, exit) is only assigned when it is unambiguous —
at assignment time, if the winning candidate for an entry has a competing
candidate (same entry, different free exit — or same exit, different free
entry) whose dt is within `AMBIGUITY_S` of the winner, NEITHER is assigned;
both events stay unpaired.

- `AMBIGUITY_S = 15` module constant next to `PAIR_WINDOW`, with a comment
  naming the trade (pairing rate down, truthfulness up) and the tuk-tuk
  incident as the motivating case.
- Implementation stays within the existing candidate list: group candidates
  by entry index and by exit index; when consuming the sorted list, skip and
  mark-ambiguous instead of assigning when the runner-up is inside the
  margin. Keep it O(candidates); no new data structures beyond dicts.
- Tier-1 (same tracker id) is untouched — it is evidence, not inference.
- QA: `pairing_qa` gains `"ambiguous": <count>` (entries left unpaired
  specifically by the gate) so the cost of honesty is visible.

## Part 2 — Manual pair overlay

### Storage

`data/pair/<site>/<date>.jsonl`, append-only, one JSON row per action —
same lifecycle and idioms as `data/refine/` (`aggregate.add_refinements`):

```json
{"action": "pair",
 "entry": {"cam": "cam1", "obj_id": 731, "line": "Kalambo E", "kind": "entry", "ts": 1786021257.2},
 "exit":  {"cam": "cam2", "obj_id": 402, "line": "Lumumba S", "kind": "exit",  "ts": 1786021262.9},
 "at": "2026-08-12T15:40:11+02:00"}
{"action": "unpair",
 "entry": {"cam": "cam1", "obj_id": 655, "line": "Kalambo E", "kind": "entry", "ts": 1786021190.0},
 "at": "2026-08-12T15:41:02+02:00"}
```

- Events are identified by the refine key: (cam, obj_id, line, kind, ts).
- Later rows win over earlier rows for the same entry (append-only log,
  last-writer-wins at read time — matches refine).
- **Re-analysis wins by no longer matching**: after a re-run the obj_ids
  change, stale rows bind to nothing, and the QA block counts them as
  evaporated (mirror refine's evaporated-rows line).
- `unpair` with no `exit` detaches whatever pairing (tier-1, tier-2, or an
  earlier manual row) currently holds that entry; both halves return to the
  unpaired pool for this read.

### Read-time application (in `pair()`)

Precedence: **manual → tier-1 → gated tier-2.**

1. Load overlay rows; resolve last-writer-wins per entry key.
2. Bind manual pairs first: both events marked used, move emitted with
   `tier: 0` internally ("manual"). A manual pair may join any two events
   regardless of camera, class, or window — the human saw the footage; the
   only hard rule kept is entry.kind == "entry", exit.kind == "exit", and
   exit.ts > entry.ts (reject and surface anything else as an invalid row
   in QA, never crash).
3. Entries with a manual `unpair` are excluded from tier-1 and tier-2
   assignment for that read.
4. Tier-1 and gated tier-2 run over the remainder exactly as today.

### API (app.py)

- `POST /api/pair/{site}/{date}` body `{"action": "pair"|"unpair", "entry": {...}, "exit": {...}}`
  → appends the row, returns the refreshed movement row(s). Validation
  errors are `ValueError` with operator-facing copy (house rule: shown
  verbatim).
- `GET /api/pair/candidates/{site}/{date}?cam=..&obj_id=..&line=..&kind=entry&ts=..`
  → unpaired exit events within `PAIR_WINDOW` of that entry, same report
  class first then others, each with its crop reference — the Find-exit list.
- `GET /api/frame-at/{site}/{date}/{cam}?ts=<epoch>` → one JPEG: locate the
  segment via segment filename epochs + the camera's manifest offset
  (inverse of the engine's forward math), extract with ffmpeg
  (`-ss <offset> -i <segment> -frames:v 1`). 404 (LookupError) when ts
  falls in a coverage gap. Nothing is written to disk.

### UI (Counts tab, movements list)

- Paired row → **Unpair** button.
- Unpaired entry row (and a just-unpaired row) → **Find exit** opens a
  drawer: candidate exits with crops (same class first), plus the **frame
  scrubber** — the other camera's full frame at the entry's ts, controls
  −5s / −1s / +1s / +5s and a 1 fps autoplay toggle, backed by
  `/api/frame-at`. Clicking a candidate records the pair and closes.
- Manual rows render with a small "manual" tag in the UI list (the UI is
  internal QA; only the exports fold).

### Exports & QA

- `_move(en, ex, 0)` manual moves count with tier-1 in every export table
  (Turns "(inf)" annotation logic treats tier 0 as observed).
- Method sheet: one line, only when the overlay is non-empty:
  "Movements manually reviewed: N paired, M unpaired (reviewer overlay)".
- `pairing_qa` gains `"manual": {"paired": N, "unpaired": M, "stale": K}`.

## Tests (stdlib assert, house pattern)

- Gate: two candidate exits 3s apart for one entry → both unpaired,
  `ambiguous` counted; runner-up outside margin → paired as today.
- Overlay: pair row binds and beats tier-2; unpair detaches a tier-1 pair;
  last-writer-wins; stale row (wrong obj_id) evaporates into QA; invalid row
  (exit before entry) surfaces in QA and binds nothing.
- API: POST validates kinds/order with operator-facing messages; candidates
  endpoint filters by window and sorts same-class first; frame-at returns
  404 inside a gap, JPEG inside coverage (fixture-sized test file).
- Export: Method disclosure line appears iff overlay rows exist; manual
  pairs absent from "(inf)" annotations.

## Out of scope now / roadmap (next sessions)

1. **DeepStream on the Orin Nano** (`ds-jetson` profile): build/pull the
   DeepStream 7.1 image, YOLO11 → TensorRT engine, `nvinfer_config`, NvDCF
   tracker. Motivation: 3–5× throughput for the 8-intersection survey AND
   far stronger tier-1 tracking — DeepStream shrinks the unpaired pool this
   whole feature exists to mop up. Do this before judging how much manual
   pairing labor remains.
2. **VLM refiner pilot** (Gemma multimodal): classify a few hundred crops
   that a human already refined; measure per-class agreement. ≥~90% on the
   JICA classes → wire as an automated refiner writing `data/refine/` rows
   attributed `by: <model>` (never mixed with human rows); below → labeling
   assistant only. Known-hard case: lgv/mgv/hgv is a size call a tight crop
   may not support.
3. Full verification-queue panel over the same overlay (only if the TOR
   needs an "all movements human-verified" claim).

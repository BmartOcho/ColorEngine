# ColorEngine — Voom proofing engine

Renders a customer PDF the way Voom's presses will print it and flags every reason the render differs from what the customer sees on screen. Full spec: [`docs/Proofing_Engine_Spec.md`](docs/Proofing_Engine_Spec.md).

**Status: Phase 1** (inventory + checks, CLI `analyze`). Rendering, remap, approval, `report.html` are phase 2; LAN API, queue, device-mode proofs are phase 3.

## Setup

```bash
pip install -e ".[dev]"
python scripts/fetch_profiles.py      # GRACoL 2013 CRPC6 + SWOP 2013 from the ICC registry, SHA-256 verified
pytest
```

LittleCMS 2 is called directly (double precision) through `ctypes`. On Linux/macOS the copy bundled with Pillow is found automatically. On Windows put `lcms2.dll` on `PATH`, or set `PROOF_LCMS2` to its path.

Profiles are looked up in `profiles/icc/` and then in `C:\Windows\System32\spool\drivers\color`.

## Use

```bash
proof analyze J218500-02.pdf                    # default press condition: gracol2013
proof analyze J218500-02.pdf --profile swop2013 --device offset-1
proof merge a.pdf b.pdf --order 1,2,10,3 --out merged.pdf
proof profiles list
proof profiles add fiery-gloss VoomGloss.icc --tac 320 --condition "Voom Gloss"
```

Each run writes `jobs/<job-id>/`: `input/` (untouched upload), `job.json` (metadata, tool versions, timestamps), `inventory.json`, `findings.json`, `report.json`. The command exits 1 when any error-severity finding exists.

## Layout

| Module | Role |
| --- | --- |
| `ingest.py` | hashing, metadata, output intents, per-page PieceInfo, XMP swatches, merge |
| `inventory.py` | content-stream walker (forms, patterns, shadings, inline images), colour-space resolver, image sampler |
| `functions.py` | PDF function types 0/2/3/4 (tint transforms, shadings) |
| `cms.py` | profile store, LittleCMS transforms, ΔE00, TAC: the only module that touches LittleCMS |
| `checks/` | one rule per function, registered by id with `@rule`; `run_all(...)` |
| `swatches.py`, `report.py` | swatch clustering (ΔE00 ≤ 2.0) and `report.json` |
| `pipeline.py`, `cli.py` | stage orchestration and the `proof` command |

## Implementation notes

- **Drift coverage.** A colour on a page counts toward `cross-page-color-drift` when it is used in text, or when all objects in that colour together cover at least 0.5% of the trim area. The Brightspot gold accent is 52 thin bars of about 81 pt² each, so no single bar reaches 0.5%; summed, they cover 10%.
- **Colour versions.** Two pages are in the same version when the area-weighted mean ΔE00 between their palettes is under the drift upper bound (15). Drift is checked only within a version.
- **mixed-creator** compares each page's `/PieceInfo` fingerprint (the producing app and its keys) with the job majority.
- **Fixtures.** The Brightspot PDF is a customer file. It is gitignored and never committed, because this repo is public. To run its tests, copy it to `tests/fixtures/brightspot_badges_64.pdf` or set `BRIGHTSPOT_PDF`; without it those tests skip. Fixtures 2, 4 and 5 are built by `tests/fixtures/build.py`.
- **SWOP.** The SWOP entry uses SWOP 2013 C3 (CRPC5) as the stand-in for the file's `CGATS TR 001` intent.

## Acceptance tests (phase 1)

| # | Status |
| --- | --- |
| 1 Ingest, 2 Inventory, 3 Drift, 4 Versions, 5 Fingerprint, 6 Intent, 7 RGB-in-X-1a, 11 Determinism | ✅ |
| 13 TAC, 14 Spots (inventory and duplicate rule; Lab rendering is phase 2) | ✅ (ahead of phase) |
| 8–10, 12, 15 | phase 2/3 |

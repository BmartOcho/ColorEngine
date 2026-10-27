# ColorEngine — Voom proofing engine

Renders a customer PDF the way Voom's presses will print it and flags every reason the render differs from what the customer sees on screen. Full spec: [`docs/Proofing_Engine_Spec.md`](docs/Proofing_Engine_Spec.md). Status, decisions and open items: [`docs/HANDOFF.md`](docs/HANDOFF.md).

**Status: Phases 1–3.**
- Phase 1: inventory, checks, `proof analyze`.
- Phase 2: render, remap, change note, approval hashing, `report.html`, `proof run`.
- Phase 3: LAN API with job queue, Windows service, engine-controlled merge, spot rendering from Lab with spot/white plates, device-mode proofs.

Still to collect: the Large Fuji and Indigo colour settings, the wide-format RIP ICCs, the exported Fiery device profile, and fixture 3 (the original Brightspot sources).

## Setup

```bash
pip install -e ".[dev]"
python scripts/fetch_profiles.py      # GRACoL 2013 CRPC6 + SWOP 2013 from the ICC registry, SHA-256 verified
pytest
```

Ghostscript renders the proofs: `gs` on Linux, or `gswin64c` on the Windows station (or set `PROOF_GS`). LittleCMS 2 is called directly (double precision) through `ctypes`. On Linux/macOS the copy bundled with Pillow is found automatically. On Windows put `lcms2.dll` on `PATH`, or set `PROOF_LCMS2` to its path.

Profiles are looked up in `profiles/icc/` and then in `C:\Windows\System32\spool\drivers\color`.

## Use

```bash
proof run J218500-02.pdf                        # analyze, auto-remap drift, render, report.html
proof run J218500-02.pdf --remap none --bleed --separations --ppi 300
proof run J218500-02.pdf --display-profile EIZO_CG2700X.icc   # monitor soft proof (default output is sRGB)
proof approve jobs/<job-id> --by "customer@example.com"       # records the SHA-256 of normalized.pdf
proof approve jobs/<job-id> --by "..." --reject-remap         # approves the original as supplied
proof verify  jobs/<job-id> to_plate.pdf                      # exit 1 + unapproved-file on mismatch
proof analyze J218500-02.pdf                    # stages 1-3 only; default press condition: gracol2013
proof analyze J218500-02.pdf --profile swop2013 --device offset-1
proof merge a.pdf b.pdf --order 1,2,10,3 --out merged.pdf
proof profiles list
proof profiles add fiery-gloss VoomGloss.icc --tac 320 --condition "Voom Gloss"
proof run job.pdf --device little-fuji --mode device             # proof through the device ICC (once in devices.json)
PROOF_API_TOKENS=printssistant:<token> proof serve --host 0.0.0.0  # LAN API on :8750
```

## LAN API

`proof serve` (FastAPI) exposes the same pipeline. Every route except `/health` needs `Authorization: Bearer <token>`, with one token per calling system set in `PROOF_API_TOKENS` (`name:token,…`).

| Route | |
| --- | --- |
| `POST /jobs` | multipart `files` (one or more PDFs, merged in `order`), `profile`, `ppi`, `remap` (`auto`/`none`/table JSON), `separations`, `bleed`, `device`, `mode`, `display` (`srgb`/`station`), `ink_only`, `callback_url` → `202 {job_id, status}` |
| `GET /jobs/{id}` | `job.json` + `status` (queued/running/done/failed), `progress` 0–1, `stage` |
| `GET /jobs/{id}/findings?severity=` · `/inventory?page=` | filtered JSON |
| `GET /jobs/{id}/proof/{page}.png?sep=cyan` | proof or plate (spot plates by slug, e.g. `pantone-1795-c`) |
| `GET /jobs/{id}/report.html` · `/normalized.pdf` | |
| `POST /jobs/{id}/remap` | remap table JSON; re-runs stages 2–5 on the original inputs |
| `POST /jobs/{id}/approve` · `/verify` | `{approver, accept_remap}` · multipart `file` → `{ok, findings}` |
| `GET /profiles` · `/health` | press + device manifests · versions |

Jobs run on an in-process queue (`PROOF_WORKERS`, default 1). Each job's `status.json` lives in its folder, so a service restart re-queues anything that was queued or running. When the job finishes, `callback_url` receives a POST with `{job_id, status, counts, blocking}`.

**Windows station.** Install Python 3.12, Ghostscript (`gswin64c`), `lcms2.dll` and NSSM. Run `pip install -e ".[api]"` and `python scripts/fetch_profiles.py`, then `scripts/windows/install_service.ps1` from an elevated PowerShell. The script registers an auto-start service with restart-on-exit, sets the environment, and adds a firewall rule limited to the local subnet.

## Device mode

`profiles/devices.json` has one entry per device and stock. When an entry has a `device_profile_file`, `--mode device` converts the press CMYK to device CMYK the way the RIP does (relative colorimetric + BPC), then converts device CMYK to the display. The proof then shows the device's own gamut and paper. Without an ICC the proof stays in reference mode, `device-profile-missing` is raised, and `report.json` notes the fallback. Today no device has an ICC on file: export "Voom Default CMYK Gloss Coated" from Command WorkStation and set it on `little-fuji`.

Each run writes `jobs/<job-id>/`:

- `input/`: the original upload, untouched
- `job.json`: metadata, tool versions, approval and timestamps
- `inventory.json`, `findings.json`, `report.json`, `report.html`
- `proof/pNNN.png`: sRGB-tagged, with `thumbs/`
- `compare/pAAA_vs_pBBB[.after].png`
- `normalized.pdf` and `before/`: only when a remap ran; the top-level files then describe the normalized file

`analyze` and `run` exit 1 when any error-severity finding remains.

## Layout

| Module | Role |
| --- | --- |
| `ingest.py` | hashing, metadata, output intents, per-page PieceInfo, XMP swatches, merge |
| `inventory.py` | content-stream walker (forms, patterns, shadings, inline images), colour-space resolver, image sampler |
| `functions.py` | PDF function types 0/2/3/4 (tint transforms, shadings) |
| `cms.py` | profile store, LittleCMS transforms, ΔE00, TAC: the only module that touches LittleCMS |
| `checks/` | one rule per function, registered by id with `@rule`; `run_all(...)` |
| `swatches.py`, `report.py` | swatch clustering (ΔE00 ≤ 2.0), change note, `report.json` and `report.html` (Jinja2, self-contained) |
| `render.py` | Ghostscript CMYK raster with overprint, then press profile to display through LittleCMS; separations, bleed/trim framing, ΔE00 heat compare |
| `remap.py` | DeviceCMYK operand rewriting that leaves untouched pages' content streams byte-identical |
| `approval.py` | approval hash, reject-remap, `unapproved-file` on submission |
| `api.py`, `jobqueue.py` | FastAPI LAN service, bearer auth, in-process queue with restart recovery and webhooks |
| `pipeline.py`, `cli.py` | stage orchestration and the `proof` command |

## Implementation notes

- **Drift coverage.** A colour on a page counts toward `cross-page-color-drift` when it is used in text, or when all objects in that colour together cover at least 0.5% of the trim area. The Brightspot gold accent is 52 thin bars of about 81 pt² each, so no single bar reaches 0.5%; summed, they cover 10%.
- **Colour versions.** Two pages are in the same version when the area-weighted mean ΔE00 between their palettes is under the drift upper bound (15). Drift is checked only within a version.
- **mixed-creator** compares each page's `/PieceInfo` fingerprint (the producing app and its keys) with the job majority.
- **Fixtures.** The Brightspot PDF is a customer file. It is gitignored and never committed, because this repo is public. To run its tests, copy it to `tests/fixtures/brightspot_badges_64.pdf` or set `BRIGHTSPOT_PDF`; without it those tests skip. Fixtures 2, 4 and 5 are built by `tests/fixtures/build.py`.
- **Remap targets.** A drift outlier is still detected against the nearest colour (ΔE00). The proposed target, though, is the colour that the same layout objects (same kind and position) carry on the other pages, falling back to the nearest colour. With nearest-only matching, p10's logo cream would have gone to the text cream and stayed visibly different.
- **Renderer.** Proofs come from Ghostscript (`tiff32nc`, overprint native on a CMYK device). PyMuPDF's `get_pixmap` does not simulate overprint, so it is not used for proofs. The press ICC is both Ghostscript's default CMYK and its output profile. DeviceCMYK therefore passes through unchanged, while RGB, Lab and spots with Lab alternates convert to press CMYK (relative + BPC), matching the inventory's LittleCMS values within 1/255. Separations come from `tiffsep`, with one plate per colorant including spots and white.
- **Queue.** The spec suggested RQ/Celery with Redis. One station runs one process, so the queue is in-process and recovers state from the job folders; no Redis is needed on Windows.
- **Test 8.** p9 and p10 differ in both names and text ink, so "identical text pixels" can't hold. The test instead checks the region both pages paint with identical ink (the 0/0/0/0 elements).
- **SWOP.** The SWOP entry uses SWOP 2013 C3 (CRPC5) as the stand-in for the file's `CGATS TR 001` intent.

## Acceptance tests

| # | Status |
| --- | --- |
| 1–7, 11 (phase 1) | ✅ |
| 8 Render, 9 Auto remap, 10 Approval, 11 Determinism incl. proofs, 13 TAC, 15 Framing (phase 2) | ✅ |
| 12 Merge: synthetic sources, plus Brightspot split and re-merged in Acrobat order | ✅ (re-run on fixture 3 once obtained) |
| 14 Spots: inventory, Lab rendering, plates, duplicate rule | ✅ |

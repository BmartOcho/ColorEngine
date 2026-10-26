# Proofing Engine Spec

Oct 21, 2026 · @Benjamin

## Purpose

Build a proofing engine that renders a customer PDF the way Voom's presses will print it and flags, before a proof is sent, every reason the render differs from what the customer sees on their screen.

The motivating case is Brightspot job J218500-02: 64 name-badge PDFs merged in Acrobat. Page 10 was added last and came from a differently built source. All 64 pages are DeviceCMYK inside a PDF/X-1a wrapper with a SWOP output intent, yet page 10 carries different ink values for the same brand colors:

| Element | Pages 1–9, 11–64 (CMYK %) | Page 10 (CMYK %) | Cause |
| --- | --- | --- | --- |
| Orange background | 14.0 / 68.3 / 79.7 / 22.6 | 3.6 / 75.9 / 97.9 / 15.2 | Different source color, both converted at export |
| Gold accent | 9.4 / 30.8 / 66.1 / 7.2 | 14 / 32 / 73 / 0 | RGB converted vs native CMYK swatch |
| Cream text | 0 / 4.3 / 12.2 / 0 | 0 / 4 / 10 / 0 | RGB converted vs native CMYK swatch |

Acrobat, with its default "output intent overrides working spaces" setting, showed the job through SWOP and hid the gap until the hard proof. Nothing in the current workflow would have caught it. The engine exists so that this class of problem is detected by rule, shown on screen through the real press profile, and fixed by a repeatable remap.

## Goals and non-goals

The engine does three things and nothing else in v1: inventory every color in a PDF, render each page through a chosen press profile, and report what will not print as the customer expects.

**Goals**

- Render a soft proof of each page through a named press condition (default GRACoL 2013 CRPC6), not through the file's own output intent.
- Display that proof accurately on the calibrated EIZO CG2700X so it matches the press sheet in the GTI booth beside it.
- Extract a per-page color inventory: every fill, stroke, image and spot, with its source color space and its final CMYK.
- Detect cross-page inconsistency: near-miss colors between pages, mixed source applications, mixed output intents, RGB inside PDF/X-1a.
- Produce a report a CSR can forward to a customer and a prepress operator can act on.
- Apply a remap (source value → target value) across all pages and write a corrected PDF, with the original untouched; the customer approves the corrected file through the proof.
- Be deterministic: the same file and profile produce byte-identical proof images and reports.

**Non-goals for v1**

- Replacing the RIP or imposition; the engine never trims, imposes, or traps.
- Hard-proof device calibration or press calibration (Fiery, Indigo, offset curves). The engine reads the profiles those processes produce.
- Pantone library lookup. Voom holds no Pantone license; spots render from the values embedded in the PDF.
- A customer-facing approval UI. v1 produces the proof and records the approval; the customer-facing screen rides on Printssistant later.
- Editing anything other than color values: no text fixes, no font embedding, no bleed addition.

## Pipeline

A job runs five stages in order; each stage writes its output to the job folder so any stage can be re-run alone.

1. **Ingest** — accept one PDF (or several to merge in a given order). Record SHA-256, page count, page boxes, Creator/Producer, PDF/X conformance, output intent(s), fonts. Never modify the upload; all work happens on a copy.
2. **Analyze** — walk every page's content stream and XObjects (recursing into Forms, patterns, shadings, inline images). Emit one record per color-bearing object: page, object type, source color space family (DeviceCMYK, ICCBased N=3/4, DeviceRGB, Lab, Separation, DeviceN, Indexed, pattern), raw components, resolved CMYK under the chosen press profile, overprint flags, area in points². Images are sampled (dominant colors + histogram), not walked per pixel.
3. **Check** — run the rule set in the Checks section against the inventory and the document metadata. Each finding carries severity, page(s), object ids, and the exact values involved.
4. **Render** — rasterize each page with overprint simulation through the press profile to the proof's display space (sRGB, or the monitor profile when supplied), with paper-white simulation on by default. Also render per-channel separations on request. Output PNG per page, tagged with the sRGB profile.
5. **Report + Remap** — write `report.json` and a human-readable `report.html`. If a remap table is supplied (or auto-derived from the majority values), rewrite the affected operands in the content streams, save `<name>_normalized.pdf`, and re-run stages 2–4 on it so the report shows before and after.

Merging happens inside Ingest, under the engine's control, so the output intent, metadata, and page order are set by the engine and recorded — not inherited from whichever file was dropped into Acrobat first.

## Color management requirements

Every proof is rendered through one explicit press condition chosen by the operator; the file's own output intent is reported, never trusted for display.

**Profile store**

| Role | Default | Notes |
| --- | --- | --- |
| Reference press condition | Coated GRACoL 2013 (CRPC6) | Confirmed: the Little Fuji Fiery uses GRACoL2013 CRPC6 (EFI) as its CMYK source. Install the Idealliance ICC on the station. |
| Untagged RGB source | sRGB IEC61966-2.1 | Matches the Fiery's RGB source setting "sRGB (PC)". |
| Untagged CMYK source | The reference press condition | Matches Acrobat once "Output Intent overrides working spaces" is off. |
| Untagged Gray source | Dot Gain 20% | Rarely matters; documented for completeness. |
| Display | ColorNavigator 7 profile of the EIZO CG2700X | Read from the Windows color store at render time; sRGB only for proofs exported to customers. |
| Viewing booth | GTI PDV-3e/LED with side walls, D50 | Monitor is calibrated to match the booth: 5000K white point, about 120 cd/m², gamma 2.2, minimum black, booth dimmer set to match. |

**Device manifest** — `profiles/devices.json`, one entry per device and stock. The engine proofs in **reference mode** (CRPC6) unless a device has its own ICC, then it can also proof in **device mode**.

| Device | Simulation target | Device profile | Status |
| --- | --- | --- | --- |
| Little Fuji (Fiery) | GRACoL 2013 CRPC6 | Voom Default CMYK Gloss Coated, calibrated 2026-06-18 | Confirmed |
| Large Fuji | CRPC6 assumed | To collect from its Fiery | Open |
| HP Indigo | CRPC6 emulation assumed | To collect | Open |
| Offset ×2 | CRPC6 | None: house curves, no ICC | Reference mode only; report flags `device-profile-missing` |
| Flatbed UV (white ink) | Native | RIP profile to collect | Device mode; white shown as its own separation |
| HP Latex ×2 | Native | RIP profile to collect | Device mode |

**Source color space handling**

| PDF color space | Resolution to press CMYK | Flag |
| --- | --- | --- |
| DeviceCMYK | Pass through unchanged (values are the ink) | none |
| ICCBased N=4 | Pass through unless profile ≠ press profile; then CMYK→CMYK via Lab with relative colorimetric + BPC, and flag | `cmyk-retagged` |
| DeviceRGB / ICCBased N=3 | RGB → press CMYK, relative colorimetric, BPC on | `rgb-object` (error inside PDF/X-1a, warning otherwise) |
| Lab / CalRGB / CalGray | → press CMYK, relative colorimetric | `lab-object` |
| Separation / DeviceN | Use the alternate space's tint transform; if a Lab alternate exists prefer it for display; list each colorant by name | `spot-<name>` |
| Indexed | Resolve base space, then as above | inherits base |
| Pattern / Shading | Sample the function at 16 stops; treat each stop as an object | inherits |

**Rendering rules**

- Overprint simulation always on (OPM 0 and 1 honoured; knockout vs overprint of 100K text shown correctly).
- Rendering intent for the final CMYK → display step: relative colorimetric with paper-white simulation (absolute colorimetric look) by default; operator toggle for "ink only".
- Black point compensation on for every conversion into CMYK.
- Total area coverage computed per object and per page from the resolved CMYK; TAC limit comes from the press profile manifest (GRACoL default 320%).
- Spot simulation uses the colorant's Lab when present; otherwise the alternate tint transform, labelled `simulated-from-alternate` in the report.
- All conversions use the same CMM (LittleCMS 2) so results match across CLI, API, and tests.

**Explicit stance on output intents**

The output intent is metadata about what the file *claims*. The engine records it, compares it to the chosen press condition, and raises `intent-mismatch` when they differ. This matters on the Fiery: a SWOP-tagged file is treated as GRACoL with no correction. The engine never uses the intent to render unless the operator selects "proof as supplied".

**Spot colors** — no Pantone library. A Separation or DeviceN colorant renders from its Lab alternate when present (current Adobe apps embed one), otherwise from its CMYK alternate and is labelled `simulated-from-alternate`. A house spot registry measured from Voom press sheets is a v2 item.

## Checks

Checks run against the color inventory and metadata; each is a pure function returning findings, so new rules are added without touching the pipeline. Severity: **error** blocks proofing, **warn** goes on the report, **info** is logged.

| Rule id | Severity | What it detects | Why it matters |
| --- | --- | --- | --- |
| `cross-page-color-drift` | error | First group pages into **color versions** by palette (Brightspot has an orange set and a green set; different palettes are deliberate, never errors). Then, for each object covering ≥ 0.5% of the page area, plus all text, find the nearest color used elsewhere in the job: a ΔE00 above 1.0 but below 15.0 (both configurable) is a near miss and is flagged. Smaller objects stay in the inventory but cannot raise this rule. | The Brightspot page 10 case. The core rule. |
| `mixed-source-fingerprint` | warn | A page mixes "round" CMYK values (all components integer %) with non-round ones, or pages differ in roundness pattern. | Non-round values mean color-managed conversion at export; a mix means different swatch definitions or export settings. |
| `mixed-creator` | warn | Pages whose content was produced by different applications or versions (from object metadata, font subsets, or merge history when the engine did the merge). | Flags merged files early. |
| `intent-mismatch` | warn | File output intent ≠ chosen press condition. | The Fiery treats SWOP values as GRACoL without correcting them. |
| `device-profile-missing` | info | The target device has no ICC in the manifest (offset today). | The proof shows the CRPC6 target; the press match depends on house curves. |
| `rgb-in-pdfx1a` | error | Any RGB/Lab/ICC object in a file claiming PDF/X-1a. | The claim is false; the RIP will convert unpredictably. |
| `multiple-output-intents` | warn | More than one output intent, or none, on a merged job. | Acrobat merge inherits the first file's intent silently. |
| `tac-over-limit` | error | Any resolved CMYK object over the profile's TAC. | Set-off, drying. |
| `rich-black-inconsistent` | warn | Distinct rich-black recipes (K ≥ 90 plus other inks) within one job. | Visible mismatch on large solids. |
| `registration-color-used` | error | Registration color on non-mark objects. | Prints in every plate. |
| `spot-vs-process-duplicate` | warn | A spot colorant whose name matches a process-defined color elsewhere in the job (text match; no library needed). | "PANTONE 1795 C" on one page, a CMYK build on another. |
| `small-text-multi-ink` | warn | Text under 8 pt in two or more inks, or white text under 6 pt reversed out of a multi-ink solid. | Registration-sensitive. |
| `unapproved-file` | error | The file sent to press or plate has a different SHA-256 from the one the customer approved. | Guarantees approved = printed. |
| `image-colorspace` | info | Image source spaces and effective resolution per image. | Context for the proof. |

Each finding includes a `fix` object when the engine can propose one: for `cross-page-color-drift` the fix is a remap table mapping each outlier value to its nearest majority value, which the Remap stage can apply directly.

## Outputs

Every run writes one job folder; nothing is returned only in memory.

```
jobs/<job-id>/
  input/            original upload(s), untouched
  job.json          ingest metadata, profile chosen, engine version, timestamps
  inventory.json    one record per color object (page, type, space, raw, resolved CMYK, Lab, area, overprint)
  findings.json     check results with severity, pages, object ids, proposed fix
  proof/
    p001.png …      per-page soft proof, 150 ppi default (300 on request), sRGB-tagged
    p001.cyan.png … separations when requested
    thumbs/         200 px wide, for the report
  compare/
    p009_vs_p010.png  side-by-side with ΔE heat overlay for any pair named in findings
  normalized.pdf    only when a remap ran
  report.html       self-contained, offline, printable; links to proof images
  report.json       machine-readable summary (counts by severity, swatch table, remap applied)
```

**Swatch table** — the headline of the report: each distinct resolved color across the job, clustered by ΔE2000 ≤ 2.0, with its CMYK, Lab, which pages carry it, its source space, and a 24 px chip rendered through the press profile. Outliers from `cross-page-color-drift` sit in their own rows with the ΔE to their nearest majority color.

**Determinism** — identical input, profile set, and engine version produce identical `inventory.json`, `findings.json`, and proof PNGs (pixel-exact). Timestamps live only in `job.json`.

**Proof framing** — the customer proof shows the page cropped to TrimBox. A toggle shows bleed to the BleedBox with the trim line drawn, for prepress use.

## Approval and file integrity

The customer's proof approval is the only sign-off; the engine makes sure what they approved is exactly what prints.

1. When a remap runs, the customer proof is generated from `normalized.pdf`, never the original.
2. The proof carries a plain-language change note above the pages, e.g. "Page 10: background, accent and text colors adjusted to match the other 63 badges", with original and new values side by side.
3. The SHA-256 of the approved file is recorded in `job.json` with the approval time and approver.
4. Any later submission to press or plate is hashed and compared; a mismatch raises `unapproved-file`.
5. If the customer rejects the remap, the job proceeds on the original file and `job.json` records that the difference was approved as supplied.

## Architecture and tool choices

Python 3.12 package `proofing_engine/`, with a CLI and a FastAPI layer over the same functions. It runs on one dedicated Windows 11 Pro proofing station inside the Voom building; customer files never leave the network. The same station drives the EIZO CG2700X and sits beside the GTI booth.

- **Station hardware (minimum):** 8-core CPU, 32 GB RAM, 1 TB NVMe SSD, wired network, and a graphics card with DisplayPort or USB-C that drives 4K at 60 Hz with 10-bit output (NVIDIA with Studio drivers is simplest).
- **Runs as:** a Windows service (via NSSM or the built-in service wrapper) exposing the API on the LAN only.
- **Profiles:** read from `C:\Windows\System32\spool\drivers\color`, shared with Acrobat and ColorNavigator.
- **Job folder:** an SMB share so other workstations can drop PDFs in and open reports.

| Concern | Tool | Why this one |
| --- | --- | --- |
| PDF object model, content-stream walking, remap writes | pikepdf (qpdf) | Exact operand access, lossless rewrite, handles PDF/X files without re-serializing untouched objects. Already proven on the Brightspot file. |
| Rasterization with overprint + ICC output | MuPDF via `pymupdf` (`mutool draw` equivalent) with a CMYK output profile, fallback Ghostscript `-dOverprint=/simulate -sOutputICCProfile` | MuPDF is fast and deterministic; Ghostscript covers edge cases (DeviceN with 5+ colorants, odd shadings). Both are pinned by version in `job.json`. |
| Color transforms, ΔE, Lab | LittleCMS 2 via `pillow`'s `ImageCms` plus `colour-science` for ΔE2000 | One CMM for inventory and render so numbers agree. |
| Image sampling | Pillow + numpy | Dominant colors and histograms without per-pixel inventory records. |
| Report | Jinja2 → single-file HTML with inlined CSS and base64 thumbs | Opens anywhere, emails as one attachment. |
| API | FastAPI, background job queue (RQ or Celery, Redis) | Long renders run off the request thread; Printssistant polls or receives a webhook. |
| Storage | Local job folder in v1; S3/R2 adapter behind one interface | Same folder layout either way. |
| Tests | pytest with golden files | Fixture PDFs plus expected `inventory.json`/`findings.json`; proofs compared pixel-exact. |

**Module layout**

- `ingest.py` — hashing, merge, metadata, output-intent capture
- `inventory.py` — content-stream walker, color-space resolver, image sampler
- `cms.py` — profile store, transforms, ΔE, TAC; the only module that touches LittleCMS
- `checks/` — one file per rule, registered by id; `run_all(inventory, meta, profile)`
- `render.py` — page rasterization and separations
- `remap.py` — operand rewriting, normalized PDF output
- `report.py` — JSON and HTML
- `api.py`, `cli.py` — thin wrappers

Rendering and inventory are independent: a check can run without a single page rendered, and a render never depends on findings.

## API and data model

The CLI and the HTTP API call the same five functions; the API adds a job queue and nothing else.

**CLI**

```
proof analyze  <in.pdf> [--profile gracol2013] [--out jobs/]        → inventory + findings
proof render   <in.pdf> [--profile …] [--ppi 150] [--separations]     → proof PNGs
proof run      <in.pdf> [--profile …] [--remap auto|<table.json>]    → everything, incl. report
proof merge    <a.pdf> <b.pdf> … --order 1,2,10,3 --out merged.pdf   → engine-controlled merge
proof profiles list|add <name> <file.icc> --tac 320 --paper-lab …
```

**HTTP**

| Method, path | Body / params | Returns |
| --- | --- | --- |
| `POST /jobs` | multipart PDF(s), `profile`, `ppi`, `remap` (`none`/`auto`/table), `separations` | `{job_id, status}` 202 |
| `GET /jobs/{id}` | — | `job.json` plus status (`queued`/`running`/`done`/`failed`) and progress 0–1 |
| `GET /jobs/{id}/findings` | `?severity=error` | `findings.json` |
| `GET /jobs/{id}/inventory` | `?page=10` | `inventory.json` filtered |
| `GET /jobs/{id}/proof/{page}.png` | `?sep=cyan` | image |
| `GET /jobs/{id}/report.html` | — | report |
| `GET /jobs/{id}/normalized.pdf` | — | corrected PDF when a remap ran |
| `POST /jobs/{id}/remap` | remap table | re-runs stages 2–5 on the normalized output |
| `GET /profiles` | — | manifest |

Auth is a bearer token per calling system in v1; Printssistant holds one.

**Core records**

```json
// inventory.json — one per color object
{"page": 10, "obj": "p10.fill.3", "kind": "fill", "space": "DeviceCMYK",
 "raw": [0.14, 0.32, 0.73, 0.0], "cmyk": [14.0, 32.0, 73.0, 0.0],
 "lab": [71.2, 8.9, 53.4], "round": true, "overprint": false, "area_pt2": 812.5,
 "cluster": "c3"}

// findings.json — one per finding
{"rule": "cross-page-color-drift", "severity": "error", "pages": [10],
 "detail": "Gold accent 14/32/73/0 on p10 vs 9.4/30.8/66.1/7.2 on 63 pages (ΔE00 6.8)",
 "objects": ["p10.fill.3"],
 "fix": {"type": "remap", "map": [{"from": [14,32,73,0], "to": [9.4,30.8,66.1,7.2]}]}}

// remap table
{"profile": "gracol2013", "tolerance_de": 1.0,
 "map": [{"from": [3.6,75.9,97.9,15.2], "to": [14.0,68.3,79.7,22.6], "pages": [10]}]}
```

All CMYK values are percentages with one decimal; raw operands are kept exactly as they appear in the PDF.

## Acceptance tests

The Brightspot file (`J218500-02_Brightspot_BSIE2601_Name_Badges_64.pdf`) is fixture 1 and the engine is not done until every row below passes.

| # | Test | Pass condition |
| --- | --- | --- |
| 1 | Ingest Brightspot | 64 pages; Creator "Adobe InDesign 21.6 (Windows)"; PDF/X-1a:2003; one output intent, CGATS TR 001; no RGB/ICC/Lab objects. |
| 2 | Inventory p9 and p10 | p9 contains 14.0/68.3/79.7/22.6, 9.4/30.8/66.1/7.2, 0/4.3/12.2/0; p10 contains 3.6/75.9/97.9/15.2, 14/32/73/0, 0/4/10/0; `round` is false for the first set and true for p10's gold and cream. |
| 3 | `cross-page-color-drift` | Exactly one finding, severity error, pages \[10\], three outlier values each mapped to its nearest value elsewhere in the job. |
| 4 | Color versions | The green/olive pages (17–21, 48–64, using 25.3/13.2/37.8/3.8 and 60.2/41.5/67.2/46.2) are reported as a separate color version and raise no drift finding. |
| 5 | `mixed-source-fingerprint` | Fires on p10 only. |
| 6 | `intent-mismatch` | Fires when the press condition is CRPC6; silent when it is SWOP. |
| 7 | `rgb-in-pdfx1a` | Silent on Brightspot; fires on fixture 2 (an InDesign export with "No Color Conversion" containing sRGB fills). |
| 8 | Render p9 vs p10 | Proof PNGs differ; mean ΔE00 over the background region > 4.0; identical pixels in the text region. |
| 9 | Auto remap | `normalized.pdf` re-analyzed shows zero drift findings; p10's three fills equal the matching values; untouched pages' content streams byte-identical. |
| 10 | Approval integrity | Approving `normalized.pdf` records its SHA-256; submitting the original afterwards raises `unapproved-file`. |
| 11 | Determinism | Running test 9 twice yields identical `inventory.json`, `findings.json`, and proof PNG hashes. |
| 12 | Merge | `proof merge` of the source PDFs (fixture 3, once obtained) in Acrobat's order reproduces the page sequence and writes one CRPC6 output intent; `multiple-output-intents` reports what each source carried. |
| 13 | TAC | Fixture 4 (a 400% rich-black test page) raises `tac-over-limit` with the page and value. |
| 14 | Spot | Fixture 5 (a two-spot job with Lab alternates) lists both colorants, renders them from Lab, and `spot-vs-process-duplicate` fires when a CMYK build of the same name is added. |
| 15 | Proof framing | Default render is cropped to TrimBox; the bleed toggle renders the BleedBox with the trim line. |

Fixtures 2, 4, and 5 are built by the engine's own test suite from InDesign-style constructs with pikepdf, so the repository needs no third-party files beyond the Brightspot PDF.

## Phases

Three phases; each ends with the acceptance tests it unlocks passing in CI.

1. **Phase 1 — Inventory and checks (tests 1–7, 11).** `ingest`, `inventory`, `cms`, `checks`, CLI `analyze`. No rendering yet. This alone would have caught the Brightspot page. Runs on the proofing station as a CLI.
2. **Phase 2 — Render, remap, approval (tests 8–10, 13, 15).** `render` through CRPC6 to the EIZO's display profile, `remap`, change note, approval hash, `report.html`, CLI `run`.
3. **Phase 3 — Service, merge, devices (tests 12, 14).** Windows service and LAN API, job queue, `merge`, spot handling, device manifest with device-mode proofs for the Fiery, Indigo and wide-format RIP profiles. Printssistant and the Zoho Desk widget can call it.

v2 candidates: a house spot registry measured with the spectrophotometer used for the Fiery calibration, measured-data verification of hard proofs, a customer brand-color registry that seeds remap tables automatically, and G7 press profiles for the offset presses.

## Decisions

Settled with Benjamin on 2026-10-22; the build follows these.

| Question | Decision |
| --- | --- |
| Press condition | GRACoL 2013 CRPC6 is the reference, matching the Little Fuji Fiery's CMYK source. Each device and stock gets a manifest entry. |
| Offset presses | Run on house curves with no ICC. Reference mode only, flagged `device-profile-missing`. |
| Where it runs | A dedicated Windows 11 Pro station in the building; files stay on the LAN. |
| Display and viewing | EIZO CG2700X calibrated in ColorNavigator to match the GTI PDV-3e D50 booth. Monitor soft proofing is in v1. |
| RIP behaviour | The Fiery converts incoming CMYK as CRPC6 to its device profile, so `intent-mismatch` stays a warning. |
| Pantone | No license. Spots render from embedded values only. |
| Remap sign-off | Customer approval of the proof is the sign-off, enforced by file hash. |
| Drift threshold | Objects ≥ 0.5% of page area plus all text; different palettes are versions, near misses are errors. |
| Proof framing | TrimBox by default; bleed toggle for prepress. |

**Still to collect before phase 3:** the Large Fuji and Indigo color settings, and the ICC profiles from the wide-format RIPs.

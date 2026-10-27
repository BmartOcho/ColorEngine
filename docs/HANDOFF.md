# Handoff log

Status as of 2026-10-27. Phases 1–3 of the [spec](Proofing_Engine_Spec.md) are merged to `main` ([#1](https://github.com/BmartOcho/ColorEngine/pull/1), [#2](https://github.com/BmartOcho/ColorEngine/pull/2)). CI is green.

## Where things stand

| Area | State |
| --- | --- |
| Inventory, 14 checks, `proof analyze` | done |
| Render (Ghostscript + LittleCMS), remap, change note, approval hash, `report.html`, `proof run` | done |
| LAN API (`proof serve`), job queue, merge, spot/white plates, device mode | done in code, never run on Windows |
| Windows service (`scripts/windows/install_service.ps1`) | written, untested |
| Device-mode proofs | waiting on device ICCs |

Acceptance tests 1–15 pass, with one caveat: test 12 (merge) uses stand-in sources until fixture 3 arrives. That's 39 tests locally; CI runs 24 and skips the 15 that need the Brightspot PDF.

## Blocked on Benjamin (collect, then resume)

1. **Fiery device profile.** Export "Voom Default CMYK Gloss Coated" from Command WorkStation. Copy it to `profiles/icc/` or the Windows colour store, and set `device_profile_file` on `little-fuji` in `profiles/devices.json`.
2. **Large Fuji and HP Indigo** colour settings and ICCs. Add them the same way.
3. **Wide-format RIP ICCs** for the Flatbed UV and both HP Latex printers. Set the flatbed's white-ink handling once its profile is in hand.
4. **Fixture 3:** the original Brightspot source PDFs, in the order Acrobat merged them. Re-run test 12 against them (`tests/test_phase3.py::test_12_*`).
5. **Proofing station setup:**
   - Install Python 3.12, Ghostscript (`gswin64c`), `lcms2.dll` and NSSM.
   - Run `pip install -e ".[api]"` and `python scripts/fetch_profiles.py`.
   - Find the ColorNavigator profile path for the EIZO CG2700X; it becomes `PROOF_DISPLAY_PROFILE`.
   - Create an SMB share for the job folder.
   - Create one API token per calling system (Printssistant, Zoho Desk).
   - Run `install_service.ps1` from an elevated PowerShell, then check `GET /health`.
6. **Brightspot PDF for local tests.** It is a customer file and is never committed (public repo). Copy it to `tests/fixtures/brightspot_badges_64.pdf` or set `BRIGHTSPOT_PDF`.

## Decisions taken (and where they differ from the spec)

- **Drift coverage is per colour per page.** All objects in one colour are summed against the 0.5% threshold. Per object, the Brightspot gold bars (~81 pt² each) would never count.
- **p10 has four outliers, not three.** The fourth is 9 pt text at 0/3/9/0 (ΔE00 1.8).
- **Remap targets prefer layout counterparts.** The target is what the same objects carry on other pages, falling back to the nearest ΔE. Nearest-only mapped the p10 logo cream to the text cream.
- **Test 8** checks identical-ink regions. Identical text pixels can't hold because the names and text ink differ between pages.
- **Renderer is Ghostscript, not MuPDF.** PyMuPDF does not simulate overprint. The press ICC serves as both Ghostscript's default CMYK and its output profile, so DeviceCMYK passes through unchanged.
- **LittleCMS via ctypes in double precision.** Pillow's ImageCms is 8-bit, too coarse for a ΔE 1.0 threshold.
- **In-process job queue instead of RQ/Celery + Redis.** One station runs one process. Per-job `status.json` gives restart recovery.
- **SWOP stand-in.** The `swop2013` profile (SWOP 2013 C3, CRPC5) stands in for `CGATS TR 001`.
- **`main` started as an empty "Initial commit".** The remote had no branches, and the auto-mode safety check blocked the force-push that would have rewritten the first branch onto it.

## Resuming

Start from `main`. Run `pip install -e ".[dev]"`, then `python scripts/fetch_profiles.py`, then `pytest`. The README covers usage, the API routes, device mode and module layout.

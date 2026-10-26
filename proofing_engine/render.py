"""Page rasterization with overprint simulation, separations and comparisons.

Ghostscript renders each page to a CMYK raster (tiff32nc) with the press ICC
as both its default CMYK and its output profile. DeviceCMYK values therefore
pass through unchanged (same profile in and out), while RGB, Lab and spot
colours with Lab alternates convert into press CMYK (relative colorimetric
with BPC). Overprint (OP/op, OPM 0 and 1) is honoured natively on a CMYK
device. The ink raster is then converted to the display space through the
press profile, and optionally through a device profile first, by LittleCMS
(cms.ImageCMS). PyMuPDF's get_pixmap does not simulate overprint, so it is
not used for proofs.
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

from .cms import ImageCMS, PressProfile, delta_e00_matrix, get_press_profile

THUMB_WIDTH = 200


def ghostscript() -> str:
    for name in (os.environ.get("PROOF_GS"), "gs", "gswin64c", "gswin32c"):
        if name and shutil.which(name):
            return shutil.which(name)
    raise FileNotFoundError("Ghostscript not found; install it or set PROOF_GS")


def gs_version() -> str:
    return subprocess.run([ghostscript(), "--version"], capture_output=True, text=True, check=True).stdout.strip()


def _gs(pdf: Path, device: str, out_dir: Path, ppi: int, box: str, pages: list[int] | None,
        press: PressProfile | None) -> int:
    """Run Ghostscript; returns the page-number offset of the output files."""
    press = press or get_press_profile()
    icc = str(press.path())
    cmd = [ghostscript(), "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", f"-sDEVICE={device}", f"-r{ppi}",
           "-dOverprint=/enable", "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
           f"-sDefaultCMYKProfile={icc}", f"-sOutputICCProfile={icc}", "-dRenderIntent=1", "-dBlackPtComp=1"]
    flag = {"trim": "-dUseTrimBox", "bleed": "-dUseBleedBox", "media": None}[box]
    if flag:
        cmd.append(flag)
    first = min(pages) if pages else None
    if first:
        cmd += [f"-dFirstPage={first}", f"-dLastPage={max(pages)}"]
    cmd += ["-o", str(out_dir / "p%04d.tif"), str(pdf)]
    subprocess.run(cmd, check=True, capture_output=True)
    return first - 1 if first else 0


def render_cmyk(pdf: Path, ppi: int = 150, box: str = "trim", pages: list[int] | None = None,
                press: PressProfile | None = None) -> dict[int, np.ndarray]:
    """Rasterize pages to HxWx4 uint8 ink arrays (0 = no ink, 255 = solid)."""
    out: dict[int, np.ndarray] = {}
    with tempfile.TemporaryDirectory() as tmp:
        offset = _gs(pdf, "tiff32nc", Path(tmp), ppi, box, pages, press)
        for f in sorted(Path(tmp).glob("p*.tif")):
            n = int(f.stem[1:]) + offset
            if pages and n not in pages:
                continue
            with Image.open(f) as im:
                out[n] = np.asarray(im.convert("CMYK")).copy()
    return out


def plate_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def render_separations(pdf: Path, ppi: int = 150, box: str = "trim", pages: list[int] | None = None,
                       press: PressProfile | None = None) -> dict[int, dict[str, np.ndarray]]:
    """One grey plate per colorant (process, spot, white), 255 = no ink, via Ghostscript tiffsep."""
    out: dict[int, dict[str, np.ndarray]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        offset = _gs(pdf, "tiffsep", Path(tmp), ppi, box, pages, press)
        for f in sorted(Path(tmp).glob("p*(*).tif")):
            m = re.match(r"p(\d+)\((.+)\)$", f.stem)
            n = int(m.group(1)) + offset
            if pages and n not in pages:
                continue
            with Image.open(f) as im:
                out.setdefault(n, {})[m.group(2)] = np.asarray(im.convert("L")).copy()
    return out


def _save_png(path: Path, rgb: np.ndarray, icc: bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.fromarray(rgb, "RGB") if rgb.ndim == 3 else Image.fromarray(rgb, "L")
    img.save(path, format="PNG", icc_profile=icc, optimize=False)


def page_boxes(meta_page: dict) -> tuple[list[float], list[float]]:
    trim = meta_page.get("trim_box") or meta_page.get("crop_box") or meta_page["media_box"]
    bleed = meta_page.get("bleed_box") or meta_page.get("media_box")
    return trim, bleed


def draw_trim(rgb: np.ndarray, meta_page: dict, ppi: int) -> np.ndarray:
    trim, bleed = page_boxes(meta_page)
    s = ppi / 72.0
    h = rgb.shape[0]
    x0, x1 = (trim[0] - bleed[0]) * s, (trim[2] - bleed[0]) * s
    y0, y1 = h - (trim[3] - bleed[1]) * s, h - (trim[1] - bleed[1]) * s
    img = Image.fromarray(rgb)
    ImageDraw.Draw(img).rectangle([round(x0), round(y0), round(x1) - 1, round(y1) - 1], outline=(0, 160, 255), width=1)
    return np.asarray(img)


def render_proofs(pdf: Path, out_dir: Path, press: PressProfile, meta: dict, ppi: int = 150,
                  separations: bool = False, bleed: bool = False, display_icc: bytes | None = None,
                  paper_white: bool = True, pages: list[int] | None = None, device: dict | None = None) -> dict:
    """Write proof/pNNN.png (+ separations, thumbs). Returns render metadata.

    With a ``device`` whose manifest entry has an ICC (device mode), the press
    CMYK is first converted to the device's CMYK the way its RIP does
    (relative colorimetric + BPC), so the proof shows the device's gamut and paper.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    device_icc = device_profile_bytes(device)
    cms = ImageCMS(press, display_icc, paper_white, device_icc=device_icc)
    box = "bleed" if bleed else "trim"
    rasters = render_cmyk(pdf, ppi, box, pages, press)
    plates = render_separations(pdf, ppi, box, pages, press) if separations else {}
    meta_pages = {p["page"]: p for p in meta["pages"]}
    written = []
    for n, cmyk in sorted(rasters.items()):
        rgb = cms.to_rgb(cmyk)
        if bleed:
            rgb = draw_trim(rgb, meta_pages[n], ppi)
        _save_png(out_dir / f"p{n:03d}.png", rgb, cms.display_bytes)
        thumb = Image.fromarray(rgb)
        thumb = thumb.resize((THUMB_WIDTH, max(1, round(rgb.shape[0] * THUMB_WIDTH / rgb.shape[1]))), Image.LANCZOS)
        _save_png(out_dir / "thumbs" / f"p{n:03d}.png", np.asarray(thumb), cms.display_bytes)
        for name, plate in sorted(plates.get(n, {}).items()):
            _save_png(out_dir / f"p{n:03d}.{plate_slug(name)}.png", plate, None)
        written.append(n)
    plate_names = sorted({name for p in plates.values() for name in p})
    return {"pages": written, "ppi": ppi, "box": box, "intent": cms.intent,
            "display": "custom" if display_icc else "sRGB IEC61966-2.1", "separations": separations,
            "plates": {plate_slug(n): n for n in plate_names},
            "mode": "device" if device_icc else "reference",
            "device": device["id"] if device else None,
            "renderer": "ghostscript tiff32nc" + (" + tiffsep" if separations else ""), "overprint": "simulated"}


def device_profile_bytes(device: dict | None) -> bytes | None:
    """ICC bytes for a device in device mode; None means reference mode."""
    if not device or not device.get("device_profile_file"):
        return None
    from .cms import PROFILE_DIR, WINDOWS_COLOR_DIR

    for d in (PROFILE_DIR / "icc", WINDOWS_COLOR_DIR):
        p = d / device["device_profile_file"]
        if p.exists():
            return p.read_bytes()
    raise FileNotFoundError(f"device profile {device['device_profile_file']} for {device['id']} not found")


def delta_e_map(cmyk_a: np.ndarray, cmyk_b: np.ndarray, cms: ImageCMS) -> np.ndarray:
    h = min(cmyk_a.shape[0], cmyk_b.shape[0])
    w = min(cmyk_a.shape[1], cmyk_b.shape[1])
    la = cms.to_lab(np.ascontiguousarray(cmyk_a[:h, :w])).reshape(-1, 3)
    lb = cms.to_lab(np.ascontiguousarray(cmyk_b[:h, :w])).reshape(-1, 3)
    # Pixel-wise ΔE00 on unique pairs (flat badge art has few distinct colours).
    pairs = np.concatenate([la, lb], axis=1)
    uniq, inv = np.unique(pairs.round(3), axis=0, return_inverse=True)
    de = np.array([delta_e00_matrix(u[None, :3], u[None, 3:])[0, 0] for u in uniq])
    return de[inv.ravel()].reshape(h, w)


def _heat(de: np.ndarray, cap: float = 10.0) -> np.ndarray:
    t = np.clip(de / cap, 0, 1)
    r = (255 * np.clip(t * 2, 0, 1)).astype(np.uint8)
    g = (255 * np.clip(2 - t * 2, 0, 1) * (t > 0.1)).astype(np.uint8)
    b = np.zeros_like(r)
    heat = np.stack([r, g, b], -1)
    heat[de < 1.0] = (255, 255, 255)
    return heat


def compare_pages(pdf: Path, a: int, b: int, out: Path, press: PressProfile, ppi: int = 150,
                  pdf_b: Path | None = None) -> dict:
    """Side-by-side proof of two pages plus a ΔE00 heat panel (white < 1, green→red up to 10)."""
    cms = ImageCMS(press)
    ra = render_cmyk(pdf, ppi, "trim", [a], press)[a]
    rb = render_cmyk(pdf_b or pdf, ppi, "trim", [b], press)[b]
    de = delta_e_map(ra, rb, cms)
    h, w = de.shape
    gap = 8
    canvas = np.full((h, 3 * w + 2 * gap, 3), 255, np.uint8)
    canvas[:, :w] = cms.to_rgb(np.ascontiguousarray(ra[:h, :w]))
    canvas[:, w + gap:2 * w + gap] = cms.to_rgb(np.ascontiguousarray(rb[:h, :w]))
    canvas[:, 2 * (w + gap):] = _heat(de)
    _save_png(out, canvas, cms.display_bytes)
    return {"pages": [a, b], "file": out.name, "mean_de": round(float(de.mean()), 2),
            "max_de": round(float(de.max()), 2), "pct_over_1": round(float((de > 1).mean() * 100), 1)}


def png_bytes(path: Path) -> bytes:
    buf = io.BytesIO()
    with Image.open(path) as im:
        im.save(buf, format="PNG")
    return buf.getvalue()

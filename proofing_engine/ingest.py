"""Ingest: hashing, metadata, output-intent capture, engine-controlled merge."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import pikepdf

from .inventory import _icc_description

SWATCH_NS = "http://ns.adobe.com/xap/1.0/t/pg/"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _box(page, name):
    v = page.obj.get(name)
    return [float(x) for x in v] if v is not None else None


def _str(v):
    if v is None:
        return None
    s = str(v)
    return s[1:] if s.startswith("/") else s


def output_intents(pdf: pikepdf.Pdf) -> list[dict]:
    out = []
    for oi in pdf.Root.get("/OutputIntents", []) or []:
        entry = {
            "subtype": _str(oi.get("/S")),
            "output_condition_identifier": _str(oi.get("/OutputConditionIdentifier")),
            "output_condition": _str(oi.get("/OutputCondition")),
            "registry_name": _str(oi.get("/RegistryName")),
            "info": _str(oi.get("/Info")),
            "embedded_profile": None,
        }
        prof = oi.get("/DestOutputProfile")
        if prof is not None:
            data = prof.read_bytes()
            entry["embedded_profile"] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "description": _icc_description(data),
                "bytes": len(data),
            }
        out.append(entry)
    return out


def _pdfx_version(pdf: pikepdf.Pdf) -> str | None:
    info = pdf.docinfo.get("/GTS_PDFXVersion") if pdf.trailer.get("/Info") is not None else None
    if info is not None:
        return str(info)
    try:
        meta = pdf.open_metadata()
        return meta.get("pdfxid:GTS_PDFXVersion")
    except Exception:
        return None


def _docinfo(pdf: pikepdf.Pdf, key: str) -> str | None:
    if pdf.trailer.get("/Info") is None:
        return None
    v = pdf.docinfo.get(key)
    return str(v) if v is not None else None


def _fonts(pdf: pikepdf.Pdf) -> list[dict]:
    seen: dict[tuple, dict] = {}
    for i, page in enumerate(pdf.pages, start=1):
        fonts = page.Resources.get("/Font") if "/Resources" in page.obj else None
        for _, f in (fonts or {}).items():
            base = _str(f.get("/BaseFont")) or "?"
            key = (base, f.objgen)
            if key not in seen:
                desc = f.get("/FontDescriptor")
                embedded = desc is not None and any(k in desc for k in ("/FontFile", "/FontFile2", "/FontFile3"))
                if f.get("/Subtype") == "/Type0":
                    embedded = True
                seen[key] = {"name": base, "subtype": _str(f.get("/Subtype")), "embedded": embedded,
                             "subset": bool(re.match(r"^[A-Z]{6}\+", base)), "pages": []}
            if i not in seen[key]["pages"]:
                seen[key]["pages"].append(i)
    return sorted(seen.values(), key=lambda d: (d["name"], d["pages"][0]))


def page_sources(pdf: pikepdf.Pdf) -> list[dict]:
    """Per-page producer fingerprint from /PieceInfo (merge history)."""
    out = []
    for i, page in enumerate(pdf.pages, start=1):
        pi = page.obj.get("/PieceInfo")
        apps = {}
        for app, data in (pi or {}).items():
            keys = sorted(str(k)[1:] for k in data.keys()) if isinstance(data, pikepdf.Dictionary) else []
            apps[str(app)[1:]] = {
                "keys": keys,
                "document_id": _str(data.get("/DocumentID")) if isinstance(data, pikepdf.Dictionary) else None,
                "last_modified": _str(data.get("/LastModified")) if isinstance(data, pikepdf.Dictionary) else None,
            }
        out.append({"page": i, "apps": apps})
    return out


def xmp_swatches(pdf: pikepdf.Pdf) -> list[dict]:
    """Named swatches InDesign writes to XMP (xmpTPg:SwatchGroups)."""
    try:
        meta = pdf.open_metadata()
        xml = str(meta)
    except Exception:
        return []
    out = []
    for m in re.finditer(r"<rdf:li[^>]*>((?:(?!<rdf:li).)*?)</rdf:li>", xml, re.S):
        block = m.group(1)
        name = re.search(r"<xmpG:swatchName>(.*?)</xmpG:swatchName>", block)
        if not name:
            continue

        def g(tag):
            v = re.search(rf"<xmpG:{tag}>(.*?)</xmpG:{tag}>", block)
            return v.group(1) if v else None

        sw = {"name": name.group(1), "mode": g("mode"), "type": g("type")}
        if sw["mode"] == "CMYK":
            sw["cmyk"] = [float(g(k) or 0) for k in ("cyan", "magenta", "yellow", "black")]
        out.append(sw)
    return out


def inspect(pdf: pikepdf.Pdf) -> dict:
    pages = []
    for i, page in enumerate(pdf.pages, start=1):
        pages.append({
            "page": i,
            "media_box": _box(page, "/MediaBox"),
            "crop_box": _box(page, "/CropBox"),
            "bleed_box": _box(page, "/BleedBox"),
            "trim_box": _box(page, "/TrimBox"),
            "art_box": _box(page, "/ArtBox"),
            "rotate": int(page.obj.get("/Rotate", 0)),
        })
    return {
        "page_count": len(pdf.pages),
        "pdf_version": pdf.pdf_version,
        "creator": _docinfo(pdf, "/Creator"),
        "producer": _docinfo(pdf, "/Producer"),
        "title": _docinfo(pdf, "/Title"),
        "pdfx_version": _pdfx_version(pdf),
        "output_intents": output_intents(pdf),
        "fonts": _fonts(pdf),
        "page_sources": page_sources(pdf),
        "swatches": xmp_swatches(pdf),
        "pages": pages,
    }


def ingest(sources: list[Path], job_dir: Path, order: list[int] | None = None) -> tuple[Path, dict]:
    """Copy uploads into job_dir/input untouched; return the working file and metadata."""
    input_dir = job_dir / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    inputs = []
    for src in sources:
        dst = input_dir / Path(src).name
        if Path(src).resolve() != dst.resolve():
            shutil.copyfile(src, dst)
        inputs.append({"name": dst.name, "sha256": sha256_file(dst), "bytes": dst.stat().st_size})
    work_dir = job_dir / "work"
    work_dir.mkdir(exist_ok=True)
    if len(sources) == 1:
        working = work_dir / inputs[0]["name"]
        shutil.copyfile(input_dir / inputs[0]["name"], working)
        merge = None
    else:
        working = work_dir / "merged.pdf"
        merge = merge_pdfs([input_dir / i["name"] for i in inputs], working, order=order)
    with pikepdf.open(working) as pdf:
        meta = inspect(pdf)
    meta["inputs"] = inputs
    meta["working_file"] = str(working.relative_to(job_dir))
    meta["working_sha256"] = sha256_file(working)
    if merge:
        meta["merge"] = merge
    return working, meta


def merge_pdfs(sources: list[Path], out: Path, order: list[int] | None = None,
               output_intent: dict | None = None) -> dict:
    """Engine-controlled merge: page order, output intent and metadata are set here.

    ``order`` lists 1-based page numbers into the concatenation of all sources.
    """
    merged = pikepdf.new()
    flat: list[tuple[int, int]] = []
    source_intents = []
    opened = [pikepdf.open(s) for s in sources]
    try:
        for si, src in enumerate(opened):
            source_intents.append({"source": Path(sources[si]).name, "output_intents": output_intents(src)})
            flat.extend((si, pi) for pi in range(len(src.pages)))
        seq = [flat[n - 1] for n in order] if order else flat
        for si, pi in seq:
            merged.pages.append(opened[si].pages[pi])
        if output_intent:
            oi = pikepdf.Dictionary(
                Type=pikepdf.Name.OutputIntent, S=pikepdf.Name.GTS_PDFX,
                OutputConditionIdentifier=output_intent["identifier"],
                RegistryName=output_intent.get("registry", "http://www.color.org"),
                Info=output_intent.get("info", output_intent["identifier"]),
            )
            if output_intent.get("profile_path"):
                oi.DestOutputProfile = merged.make_stream(Path(output_intent["profile_path"]).read_bytes(), N=4)
            merged.Root.OutputIntents = merged.make_indirect(pikepdf.Array([oi]))
        merged.save(out, deterministic_id=True)
    finally:
        for o in opened:
            o.close()
    return {"sources": [Path(s).name for s in sources], "order": order,
            "page_map": [{"source": Path(sources[si]).name, "page": pi + 1} for si, pi in seq],
            "source_output_intents": source_intents}

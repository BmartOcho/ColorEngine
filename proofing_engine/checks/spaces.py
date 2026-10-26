from . import finding, fmt_cmyk, rule

NON_CMYK = ("rgb-object", "lab-object", "cmyk-retagged", "gray-icc")


@rule("rgb-in-pdfx1a", "error")
def rgb_in_pdfx1a(ctx):
    pdfx = (ctx.meta.get("pdfx_version") or "")
    if not pdfx.startswith("PDF/X-1"):
        return []
    hits: dict[int, list[dict]] = {}
    for r in ctx.inventory:
        if any(f in r.get("flags", []) for f in NON_CMYK) or r["space"].startswith(("ICCBased", "Lab", "Cal", "DeviceRGB")):
            hits.setdefault(r["page"], []).append(r)
    return [finding("rgb-in-pdfx1a", [p], f"p{p}: {len(rs)} object(s) in {', '.join(sorted({r['space'] for r in rs}))} "
                    f"inside a file claiming {pdfx}", objects=[r["obj"] for r in rs]) for p, rs in sorted(hits.items())]


@rule("spot-vs-process-duplicate", "warn")
def spot_vs_process_duplicate(ctx):
    spots: dict[str, set] = {}
    for r in ctx.inventory:
        for name in r.get("colorants", []):
            if name not in ("Cyan", "Magenta", "Yellow", "Black", "All", "None"):
                spots.setdefault(name, set()).add(r["page"])
    process = {s["name"]: s for s in ctx.meta.get("swatches", []) if s.get("type") == "PROCESS"}
    out = []
    for name, pages in sorted(spots.items()):
        match = process.get(name)
        if match:
            out.append(finding("spot-vs-process-duplicate", pages,
                               f"Spot '{name}' also defined as a process build {fmt_cmyk(match.get('cmyk', []))}",
                               colorant=name))
    return out


@rule("image-colorspace", "info")
def image_colorspace(ctx):
    seen = {}
    for r in ctx.inventory:
        if r["kind"] != "image":
            continue
        key = (r["page"], r.get("image"), r.get("bbox") and tuple(r["bbox"]))
        seen.setdefault(key, r)
    return [finding("image-colorspace", [r["page"]], f"p{r['page']} image {r.get('image')}: {r['space']}, "
                    f"{r['pixels'][0]}x{r['pixels'][1]} px, {r['effective_ppi']:g} ppi effective",
                    objects=[r["obj"]], space=r["space"], effective_ppi=r["effective_ppi"])
            for r in seen.values()]

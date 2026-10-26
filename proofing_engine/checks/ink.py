from . import finding, fmt_cmyk, rule


@rule("tac-over-limit", "error")
def tac_over_limit(ctx):
    limit = ctx.press.tac
    worst: dict[int, tuple[float, dict]] = {}
    hits: dict[int, list[str]] = {}
    for r in ctx.inventory:
        t = sum(r["cmyk"])
        if t > limit + 0.05:
            hits.setdefault(r["page"], []).append(r["obj"])
            if r["page"] not in worst or t > worst[r["page"]][0]:
                worst[r["page"]] = (t, r)
    return [finding("tac-over-limit", [p], f"p{p}: {worst[p][1]['kind']} {fmt_cmyk(worst[p][1]['cmyk'])} = "
                    f"{worst[p][0]:.1f}% over {limit:g}% TAC", objects=hits[p], tac=round(worst[p][0], 1), limit=limit)
            for p in sorted(worst)]


@rule("rich-black-inconsistent", "warn")
def rich_black_inconsistent(ctx):
    k_min = ctx.config["rich_black_k"]
    recipes: dict[tuple, set] = {}
    for r in ctx.inventory:
        c = r["cmyk"]
        if c[3] >= k_min and any(v > 0 for v in c[:3]):
            recipes.setdefault(tuple(c), set()).add(r["page"])
    if len(recipes) <= 1:
        return []
    parts = [f"{fmt_cmyk(c)} (p{','.join(map(str, sorted(p)))})" for c, p in sorted(recipes.items())]
    return [finding("rich-black-inconsistent", [p for ps in recipes.values() for p in ps],
                    f"{len(recipes)} rich-black recipes: " + "; ".join(parts))]


@rule("registration-color-used", "error")
def registration_color_used(ctx):
    trims = {p["page"]: (p.get("trim_box") or p["media_box"]) for p in ctx.meta["pages"]}
    hits: dict[int, list[str]] = {}
    for r in ctx.inventory:
        if "registration" not in r.get("flags", []) or not r.get("bbox"):
            continue
        t, b = trims[r["page"]], r["bbox"]
        inside = b[0] < t[2] and b[2] > t[0] and b[1] < t[3] and b[3] > t[1]
        if inside:
            hits.setdefault(r["page"], []).append(r["obj"])
    return [finding("registration-color-used", [p], f"p{p}: {len(o)} object(s) inside the trim use Registration "
                    "(prints on every plate)", objects=o) for p, o in sorted(hits.items())]


@rule("small-text-multi-ink", "warn")
def small_text_multi_ink(ctx):
    small, reverse = ctx.config["small_text_pt"], ctx.config["small_reverse_text_pt"]
    by_page: dict[int, list[dict]] = {}
    for r in ctx.inventory:
        by_page.setdefault(r["page"], []).append(r)
    out = []
    for p, recs in sorted(by_page.items()):
        objs, notes = [], []
        for i, r in enumerate(recs):
            if r["kind"] != "text":
                continue
            size = r.get("font_size_pt", 99)
            inks = sum(1 for v in r["cmyk"] if v > 0.5)
            if size < small and inks >= 2:
                objs.append(r["obj"])
                notes.append(f"{size:g}pt text in {inks} inks ({fmt_cmyk(r['cmyk'])})")
            elif size < reverse and inks == 0 and r.get("bbox"):
                under = _underlying(recs[:i], r["bbox"])
                if under and sum(1 for v in under["cmyk"] if v > 0.5) >= 2:
                    objs.append(r["obj"])
                    notes.append(f"{size:g}pt white text reversed out of {fmt_cmyk(under['cmyk'])}")
        if objs:
            out.append(finding("small-text-multi-ink", [p], f"p{p}: " + "; ".join(sorted(set(notes))), objects=objs))
    return out


def _underlying(previous, bbox):
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    for r in reversed(previous):
        b = r.get("bbox")
        if r["kind"] in ("fill", "shading-stop", "image") and b and b[0] <= cx <= b[2] and b[1] <= cy <= b[3]:
            return r
    return None

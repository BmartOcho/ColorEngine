from collections import Counter

import numpy as np

from ..cms import delta_e00, delta_e00_matrix
from . import finding, fmt_cmyk, rule


@rule("cross-page-color-drift", "error")
def cross_page_color_drift(ctx):
    """Near-miss colours between pages of one colour version.

    Detection uses the nearest colour elsewhere in the version (ΔE00 within
    (min, max)). The proposed target prefers the colour that the *same layout
    objects* (same kind and position) carry on the other pages, so a logo maps
    to the logo colour rather than to whichever cream is numerically closest;
    it falls back to the nearest colour when no counterpart exists.
    """
    lo, hi = ctx.config["drift_min_de"], ctx.config["drift_max_de"]
    layout = _layout_index(ctx)
    obj_slot = {r["obj"]: _slot(r) for r in ctx.inventory}
    out = []
    for version, pages in ctx.versions().items():
        sig = {p: ctx.significant(p) for p in pages}
        usage: dict[tuple, set] = {}
        for p in pages:
            for c in sig[p]:
                usage.setdefault(c, set()).add(p)
        page_set = set(pages)
        for p in pages:
            others = sorted({c for q in pages if q != p for c in sig[q]})
            if not others:
                continue
            other_labs = np.array([_lab(ctx, c, usage) for c in others])
            outliers = []
            for c, info in sorted(sig[p].items()):
                if c in others:
                    continue
                d = delta_e00_matrix(np.array([info["lab"]]), other_labs)[0]
                j = int(d.argmin())
                if not lo < d[j] < hi:
                    continue
                target, de, match = others[j], float(d[j]), "nearest"
                counterpart = _counterpart(info["objects"], obj_slot, layout, p, page_set, c)
                if counterpart and counterpart in usage:
                    cde = delta_e00(info["lab"], _lab(ctx, counterpart, usage))
                    if cde < hi:
                        target, de, match = counterpart, cde, "layout"
                outliers.append((c, target, de, info, match))
            if not outliers:
                continue
            parts = []
            for c, target, de, _, match in outliers:
                n = len(usage[target])
                where = "same objects" if match == "layout" else "nearest"
                parts.append(f"{fmt_cmyk(c)} on p{p} vs {fmt_cmyk(target)} ({where}) on {n} page{'s' * (n != 1)} "
                             f"(ΔE00 {de:.1f})")
            main = max(outliers, key=lambda o: o[3]["area"])
            out.append(finding(
                "cross-page-color-drift", [p], "; ".join(parts),
                objects=[o for *_, info, _ in outliers for o in info["objects"]],
                fix={"type": "remap", "map": [
                    {"from": list(c), "to": list(t), "pages": [p], "delta_e": round(de, 2),
                     "target_pages": len(usage[t]), "match": match} for c, t, de, _, match in outliers]},
                version=version,
                reference_page=min(usage[main[1]], key=lambda q: (abs(q - p), q)),
            ))
    return out


def _slot(r):
    b = r.get("bbox")
    return (r["kind"], tuple(round(v * 2) / 2 for v in b)) if b else None


def _layout_index(ctx):
    idx: dict[tuple, dict[int, tuple]] = {}
    for r in ctx.inventory:
        s = _slot(r)
        if s:
            idx.setdefault(s, {})[r["page"]] = tuple(r["cmyk"])
    return idx


def _counterpart(objects, obj_slot, layout, page, page_set, own):
    """Most common colour the same-slot objects have on other pages of the version."""
    votes: Counter = Counter()
    for o in objects:
        s = obj_slot.get(o)
        for q, c in (layout.get(s) or {}).items():
            if q != page and q in page_set and c != own:
                votes[c] += 1
    if not votes:
        return None
    (best, n), = votes.most_common(1)
    return best if n >= max(1, len(objects) // 2) else None


def _lab(ctx, cmyk, usage):
    p = min(usage[cmyk])
    return ctx.page_colors[p][cmyk]["lab"]

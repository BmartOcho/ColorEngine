import numpy as np

from ..cms import delta_e00_matrix
from . import finding, fmt_cmyk, rule


@rule("cross-page-color-drift", "error")
def cross_page_color_drift(ctx):
    lo, hi = ctx.config["drift_min_de"], ctx.config["drift_max_de"]
    versions = ctx.versions()
    out = []
    for version, pages in versions.items():
        sig = {p: ctx.significant(p) for p in pages}
        usage: dict[tuple, set] = {}
        for p in pages:
            for c in sig[p]:
                usage.setdefault(c, set()).add(p)
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
                if lo < d[j] < hi:
                    target = others[j]
                    outliers.append((c, target, float(d[j]), info))
            if not outliers:
                continue
            parts = []
            for c, target, de, info in outliers:
                n = len(usage[target])
                parts.append(f"{fmt_cmyk(c)} on p{p} vs {fmt_cmyk(target)} on {n} page{'s' * (n != 1)} (ΔE00 {de:.1f})")
            out.append(finding(
                "cross-page-color-drift", [p], "; ".join(parts),
                objects=[o for _, _, _, info in outliers for o in info["objects"]],
                fix={"type": "remap", "map": [
                    {"from": list(c), "to": list(t), "pages": [p], "delta_e": round(de, 2),
                     "target_pages": len(usage[t])} for c, t, de, _ in outliers]},
                version=version,
            ))
    return out


def _lab(ctx, cmyk, usage):
    p = min(usage[cmyk])
    return ctx.page_colors[p][cmyk]["lab"]

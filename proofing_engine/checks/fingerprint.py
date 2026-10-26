from collections import Counter

from ..inventory import is_trivial
from . import finding, fmt_cmyk, rule


@rule("mixed-source-fingerprint", "warn")
def mixed_source_fingerprint(ctx):
    """Typed-in (integer %) swatches next to converted (fractional) values."""
    per_page: dict[int, dict[bool, set]] = {}
    for r in ctx.inventory:
        if r["kind"] == "image" or "DeviceCMYK" not in r["space"] or is_trivial(r["cmyk"]):
            continue
        per_page.setdefault(r["page"], {True: set(), False: set()})[r["round"]].add(tuple(r["cmyk"]))
    patterns = {p: (bool(v[True]), bool(v[False])) for p, v in per_page.items()}
    majority = Counter(patterns.values()).most_common(1)[0][0] if patterns else None
    out = []
    for p, pat in sorted(patterns.items()):
        mixed = pat == (True, True)
        if not mixed and pat == majority:
            continue
        rounds = sorted(per_page[p][True])
        detail = (f"p{p} mixes integer-% values ({', '.join(fmt_cmyk(c) for c in rounds)}) with converted values"
                  if mixed else f"p{p} roundness pattern differs from the job majority")
        out.append(finding("mixed-source-fingerprint", [p], detail, pattern={"round": pat[0], "converted": pat[1]}))
    return out

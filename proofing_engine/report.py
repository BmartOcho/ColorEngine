"""report.json (machine-readable summary) and report.html (self-contained, printable)."""

from __future__ import annotations

import base64
from collections import Counter
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from .checks import fmt_cmyk

ROLE_ORDER = ("background", "accent", "text")


def build_report(meta: dict, findings: list[dict], swatches: list[dict], versions: dict[str, list[int]],
                 press: dict, remap: dict | None = None) -> dict:
    counts = Counter(f["severity"] for f in findings)
    return {
        "file": {"name": meta["inputs"][0]["name"] if meta.get("inputs") else None,
                 "sha256": meta.get("working_sha256"), "pages": meta["page_count"],
                 "pdfx": meta.get("pdfx_version"), "creator": meta.get("creator"),
                 "output_intents": [o.get("output_condition_identifier") for o in meta.get("output_intents", [])]},
        "press": press,
        "counts": {s: counts.get(s, 0) for s in ("error", "warn", "info")},
        "blocking": counts.get("error", 0) > 0,
        "rules": dict(sorted(Counter(f["rule"] for f in findings).items())),
        "color_versions": versions,
        "swatches": swatches,
        "remap": remap,
    }


def _role(records: list[dict], page_area: float) -> str:
    if any(r["kind"].startswith("text") for r in records):
        return "text"
    if any(r["area_pt2"] >= 0.5 * page_area for r in records):
        return "background"
    return "accent"


def change_note(table: dict, inventory: list[dict], meta: dict) -> list[dict]:
    """Plain-language note per page, e.g. 'Page 10: background, accent and text colors adjusted ...'."""
    areas = {}
    for p in meta["pages"]:
        b = p.get("trim_box") or p["media_box"]
        areas[p["page"]] = abs(b[2] - b[0]) * abs(b[3] - b[1])
    pages = sorted({p for m in table["map"] for p in (m.get("pages") or [])})
    others = meta["page_count"] - len(pages)
    notes = []
    for page in pages:
        changes = []
        for m in table["map"]:
            if m.get("pages") and page not in m["pages"]:
                continue
            recs = [r for r in inventory if r["page"] == page and r["cmyk"] == list(m["from"])]
            if not recs:
                continue
            changes.append({"role": _role(recs, areas[page]), "from": m["from"], "to": m["to"]})
        changes.sort(key=lambda c: (ROLE_ORDER.index(c["role"]), c["from"]))
        roles = list(dict.fromkeys(c["role"] for c in changes))
        listed = roles[0] if len(roles) == 1 else ", ".join(roles[:-1]) + " and " + roles[-1]
        notes.append({"page": page, "changes": changes,
                      "text": f"Page {page}: {listed} colors adjusted to match the other {others} pages."})
    return notes


def _b64(path: Path) -> str | None:
    return base64.b64encode(path.read_bytes()).decode() if path.exists() else None


def write_html(job_dir: Path, report: dict, findings: list[dict], press, notes: list[dict]) -> Path:
    from .cms import ImageCMS

    cms = ImageCMS(press)

    def chip(cmyk):
        r, g, b = cms.chip(cmyk)
        return f"#{r:02x}{g:02x}{b:02x}"

    proof = job_dir / "proof"
    pages = [{"page": n, "thumb": _b64(proof / "thumbs" / f"p{n:03d}.png"), "href": f"proof/p{n:03d}.png"}
             for n in report.get("render", {}).get("pages", [])]
    comparisons = [dict(c, img=_b64(job_dir / "compare" / c["file"])) for c in report.get("comparisons", [])]
    env = Environment(loader=PackageLoader("proofing_engine", "templates"), autoescape=select_autoescape())
    env.filters["cmyk"] = fmt_cmyk
    env.filters["chip"] = chip
    html = env.get_template("report.html.j2").render(report=report, findings=findings, notes=notes, pages=pages,
                                                     comparisons=comparisons)
    out = job_dir / "report.html"
    out.write_text(html, encoding="utf-8")
    return out

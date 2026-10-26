"""report.json (machine-readable summary). report.html arrives in phase 2."""

from __future__ import annotations

from collections import Counter


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

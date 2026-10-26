"""Swatch table: distinct resolved colours clustered by ΔE00."""

from __future__ import annotations

import numpy as np

from .cms import delta_e00_matrix


def distinct_colors(inventory: list[dict]) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for r in inventory:
        if r["kind"] == "image":
            continue
        e = out.setdefault(tuple(r["cmyk"]), {"cmyk": r["cmyk"], "lab": r["lab"], "area": 0.0, "pages": set(),
                                             "spaces": set(), "objects": 0})
        e["area"] += r["area_pt2"]
        e["pages"].add(r["page"])
        e["spaces"].add(r["space"])
        e["objects"] += 1
    return out


def build_swatch_table(inventory: list[dict], findings: list[dict], threshold: float = 2.0) -> list[dict]:
    """Cluster colours (largest coverage first); drift outliers get their own rows.

    Mutates ``inventory`` records by setting ``cluster``.
    """
    colors = distinct_colors(inventory)
    outliers: dict[tuple, dict] = {}
    for f in findings:
        if f["rule"] == "cross-page-color-drift":
            for m in f["fix"]["map"]:
                outliers[tuple(m["from"])] = m
    order = sorted(colors, key=lambda k: (-colors[k]["area"], k))
    clusters: list[dict] = []
    assign: dict[tuple, str] = {}
    centres: list[list[float]] = []
    for key in order:
        c = colors[key]
        if key not in outliers and centres:
            d = delta_e00_matrix(np.array([c["lab"]]), np.array(centres))[0]
            j = int(d.argmin())
            if d[j] <= threshold and not clusters[j]["outlier"]:
                clusters[j]["members"].append(key)
                assign[key] = clusters[j]["id"]
                continue
        cid = f"c{len(clusters) + 1}"
        clusters.append({"id": cid, "members": [key], "outlier": key in outliers})
        centres.append(c["lab"])
        assign[key] = cid
    for r in inventory:
        if r["kind"] != "image":
            r["cluster"] = assign[tuple(r["cmyk"])]
    rows = []
    for cl in clusters:
        lead = colors[cl["members"][0]]
        row = {
            "cluster": cl["id"],
            "cmyk": lead["cmyk"],
            "lab": lead["lab"],
            "members": [list(m) for m in cl["members"]],
            "pages": sorted({p for m in cl["members"] for p in colors[m]["pages"]}),
            "spaces": sorted({s for m in cl["members"] for s in colors[m]["spaces"]}),
            "area_pt2": round(sum(colors[m]["area"] for m in cl["members"]), 1),
            "objects": sum(colors[m]["objects"] for m in cl["members"]),
        }
        if cl["outlier"]:
            m = outliers[cl["members"][0]]
            row["outlier"] = {"nearest": m["to"], "delta_e": m["delta_e"]}
        rows.append(row)
    return rows

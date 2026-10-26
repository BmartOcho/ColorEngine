"""Rule registry. Each rule is a pure function ``(ctx) -> list[finding]``.

Add a rule by dropping a module in this package and decorating its function
with ``@rule("rule-id", "severity")``; the pipeline never changes.
"""

from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from functools import cached_property
from typing import Callable

import numpy as np

from ..cms import PressProfile, delta_e00_matrix

SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}

DEFAULT_CONFIG = {
    "drift_min_de": 1.0,
    "drift_max_de": 15.0,
    "drift_min_area_fraction": 0.005,
    "cluster_de": 2.0,
    "small_text_pt": 8.0,
    "small_reverse_text_pt": 6.0,
    "rich_black_k": 90.0,
}

RULES: dict[str, tuple[str, Callable]] = {}


def rule(rule_id: str, severity: str):
    def deco(fn):
        RULES[rule_id] = (severity, fn)
        fn.rule_id = rule_id
        fn.severity = severity
        return fn
    return deco


def finding(rule_id: str, pages, detail: str, objects=None, fix=None, **extra) -> dict:
    f = {"rule": rule_id, "severity": RULES[rule_id][0], "pages": sorted(set(pages)), "detail": detail,
         "objects": list(objects or [])}
    if fix is not None:
        f["fix"] = fix
    f.update(extra)
    return f


def fmt_cmyk(c) -> str:
    return "/".join(f"{v:g}" for v in c)


@dataclass
class CheckContext:
    inventory: list[dict]
    meta: dict
    press: PressProfile
    device: dict | None = None
    config: dict = field(default_factory=lambda: dict(DEFAULT_CONFIG))

    @cached_property
    def page_areas(self) -> dict[int, float]:
        out = {}
        for p in self.meta["pages"]:
            b = p.get("trim_box") or p.get("crop_box") or p["media_box"]
            out[p["page"]] = abs(b[2] - b[0]) * abs(b[3] - b[1])
        return out

    @cached_property
    def page_colors(self) -> dict[int, dict[tuple, dict]]:
        """Per page: distinct resolved CMYK -> {area, lab, text, objects}."""
        out: dict[int, dict[tuple, dict]] = {p["page"]: {} for p in self.meta["pages"]}
        for r in self.inventory:
            if r["kind"] == "image":
                continue
            key = tuple(r["cmyk"])
            e = out.setdefault(r["page"], {}).setdefault(
                key, {"area": 0.0, "lab": r["lab"], "text": False, "objects": [], "kinds": set()})
            e["area"] += r["area_pt2"]
            e["text"] |= r["kind"].startswith("text")
            e["objects"].append(r["obj"])
            e["kinds"].add(r["kind"])
        return out

    def significant(self, page: int) -> dict[tuple, dict]:
        """Colours that count for drift: ≥ min area fraction of the page, or any text."""
        limit = self.page_areas.get(page, 0) * self.config["drift_min_area_fraction"]
        return {k: v for k, v in self.page_colors.get(page, {}).items() if v["text"] or v["area"] >= limit}

    @cached_property
    def color_versions(self) -> dict[int, str]:
        """Group pages into colour versions by area-weighted palette distance.

        Two pages share a version when, weighted by coverage, their colours are
        on average closer than the drift upper bound. Different palettes
        (orange set vs green set) are deliberate and form separate versions.
        """
        pages = sorted(self.page_colors)
        sig = {p: self.significant(p) for p in pages}
        # Deduplicate identical palettes so 64 pages cost a handful of comparisons.
        palettes: dict[tuple, list[int]] = {}
        for p in pages:
            palettes.setdefault(tuple(sorted(sig[p])), []).append(p)
        keys = list(palettes)
        labs = [np.array([sig[palettes[k][0]][c]["lab"] for c in k]) if k else np.zeros((0, 3)) for k in keys]
        areas = [np.array([sig[palettes[k][0]][c]["area"] for c in k]) for k in keys]

        def directed(i, j):
            if len(labs[i]) == 0 or len(labs[j]) == 0:
                return 0.0 if len(labs[i]) == len(labs[j]) else float("inf")
            d = delta_e00_matrix(labs[i], labs[j]).min(axis=1)
            w = areas[i]
            return float((d * w).sum() / w.sum()) if w.sum() else float(d.mean())

        n = len(keys)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        limit = self.config["drift_max_de"]
        for i in range(n):
            for j in range(i + 1, n):
                if max(directed(i, j), directed(j, i)) < limit:
                    parent[find(j)] = find(i)
        groups: dict[int, list[int]] = {}
        for i, k in enumerate(keys):
            groups.setdefault(find(i), []).extend(palettes[k])
        ordered = sorted((sorted(v) for v in groups.values()), key=lambda g: g[0])
        return {p: f"v{n + 1}" for n, g in enumerate(ordered) for p in g}

    def versions(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = {}
        for p, v in sorted(self.color_versions.items()):
            out.setdefault(v, []).append(p)
        return out


def _load_rules():
    for mod in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{mod.name}")


def run_all(inventory: list[dict], meta: dict, profile: PressProfile, device: dict | None = None,
            config: dict | None = None, only: list[str] | None = None) -> list[dict]:
    _load_rules()
    ctx = CheckContext(inventory, meta, profile, device, {**DEFAULT_CONFIG, **(config or {})})
    findings = []
    for rule_id in sorted(RULES):
        if only and rule_id not in only:
            continue
        findings.extend(RULES[rule_id][1](ctx))
    findings.sort(key=lambda f: (SEVERITY_ORDER[f["severity"]], f["rule"], f["pages"], f["detail"]))
    return findings

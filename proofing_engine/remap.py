"""Operand rewriting: apply a remap table to DeviceCMYK colour operators.

Only pages named in the table (or all pages when an entry lists none) are
rewritten; every other page keeps its original content stream object, so its
bytes are untouched in the saved file.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pikepdf

from .cms import round1

CMYK_SET = {"k", "K"}
SPACE_SET = {"sc", "scn", "SC", "SCN"}


def _pct(operands) -> tuple:
    return tuple(round1(float(v) * 100) for v in operands)


def derive_auto_table(findings: list[dict], profile: str) -> dict:
    entries = []
    for f in findings:
        if f["rule"] == "cross-page-color-drift" and f.get("fix", {}).get("type") == "remap":
            for m in f["fix"]["map"]:
                entries.append({"from": m["from"], "to": m["to"], "pages": m.get("pages", f["pages"])})
    return {"profile": profile, "tolerance_de": 1.0, "map": entries}


def exact_operands(inventory: list[dict]) -> dict[tuple, list]:
    """Resolved CMYK -> the raw operands used most often in the job (so remapped values match byte-for-byte)."""
    counts: dict[tuple, dict[tuple, int]] = {}
    for r in inventory:
        if r["space"] == "DeviceCMYK" and len(r["raw"]) == 4:
            counts.setdefault(tuple(r["cmyk"]), {}).setdefault(tuple(r["raw"]), 0)
            counts[tuple(r["cmyk"])][tuple(r["raw"])] += 1
    return {k: list(max(v.items(), key=lambda kv: (kv[1], kv[0]))[0]) for k, v in counts.items()}


def _is_cmyk_space(name, resources) -> bool:
    if name == "/DeviceCMYK":
        return True
    cs = resources.get("/ColorSpace", {}).get(name) if resources is not None else None
    return cs is not None and (cs == pikepdf.Name.DeviceCMYK or (isinstance(cs, pikepdf.Name) and str(cs) == "/DeviceCMYK"))


def _rewrite(stream_owner, resources, table: dict[tuple, list], hits: list) -> bytes | None:
    instructions = pikepdf.parse_content_stream(stream_owner)
    fill_cmyk = stroke_cmyk = False
    stack = []
    changed = False
    out = []
    for inst in instructions:
        if isinstance(inst, pikepdf.ContentStreamInlineImage):
            out.append(inst)
            continue
        op = str(inst.operator)
        operands = list(inst.operands)
        if op == "q":
            stack.append((fill_cmyk, stroke_cmyk))
        elif op == "Q" and stack:
            fill_cmyk, stroke_cmyk = stack.pop()
        elif op in ("cs", "CS"):
            is_cmyk = _is_cmyk_space(str(operands[0]), resources)
            if op == "cs":
                fill_cmyk = is_cmyk
            else:
                stroke_cmyk = is_cmyk
        elif op in ("g", "rg", "G", "RG"):
            if op.islower():
                fill_cmyk = False
            else:
                stroke_cmyk = False
        elif op in CMYK_SET:
            if op == "k":
                fill_cmyk = True
            else:
                stroke_cmyk = True
        target = None
        if len(operands) == 4 and (op in CMYK_SET or (op in SPACE_SET and (fill_cmyk if op.islower() else stroke_cmyk))):
            target = table.get(_pct(operands))
        if target is not None:
            hits.append({"from": list(_pct(operands)), "op": op})
            operands = [Decimal(repr(v)) if isinstance(v, float) else v for v in target]
            inst = pikepdf.ContentStreamInstruction(operands, pikepdf.Operator(op))
            changed = True
        out.append(inst)
    return pikepdf.unparse_content_stream(out) if changed else None


def _walk_forms(pdf, resources, table, hits, seen):
    xobjs = resources.get("/XObject") if resources is not None else None
    for _, x in (xobjs or {}).items():
        if x.get("/Subtype") != "/Form" or x.objgen in seen:
            continue
        seen.add(x.objgen)
        data = _rewrite(x, x.get("/Resources", resources), table, hits)
        if data is not None:
            x.write(data)
        _walk_forms(pdf, x.get("/Resources"), table, hits, seen)


def apply_remap(src: Path, dst: Path, table: dict, inventory: list[dict]) -> dict:
    exact = exact_operands(inventory)
    by_page: dict[int | None, dict[tuple, list]] = {}
    for m in table["map"]:
        to = tuple(round1(v) for v in m["to"])
        operands = exact.get(to) or [round(v / 100, 6) for v in to]
        for p in (m.get("pages") or [None]):
            by_page.setdefault(p, {})[tuple(round1(v) for v in m["from"])] = operands
    report = {"pages": {}, "table": table}
    with pikepdf.open(src) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            table_here = {**by_page.get(None, {}), **by_page.get(i, {})}
            if not table_here:
                continue
            hits: list = []
            res = page.obj.get("/Resources")
            data = _rewrite(page, res, table_here, hits)
            if data is not None:
                page.obj.Contents = pdf.make_stream(data)
            _walk_forms(pdf, res, table_here, hits, set())
            if hits:
                report["pages"][i] = len(hits)
        pdf.save(dst, deterministic_id=True)
    return report

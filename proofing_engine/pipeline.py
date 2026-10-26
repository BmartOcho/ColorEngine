"""Stage orchestration. Every stage writes into the job folder."""

from __future__ import annotations

import json
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import pikepdf

from . import __version__
from .checks import CheckContext, DEFAULT_CONFIG, run_all
from .cms import ColorEngine, PressProfile, get_press_profile, lcms_version
from .ingest import ingest, sha256_file
from .inventory import build_inventory
from .report import build_report, change_note, write_html
from .swatches import build_swatch_table


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def default_job_id(pdf: Path, profile: str) -> str:
    return f"{Path(pdf).stem[:40]}-{sha256_file(pdf)[:8]}-{profile}"


def load_device(device_id: str | None) -> dict | None:
    if not device_id:
        return None
    from .cms import PROFILE_DIR
    manifest = json.loads((PROFILE_DIR / "devices.json").read_text())
    for d in manifest["devices"]:
        if d["id"] == device_id:
            return d
    raise KeyError(f"unknown device '{device_id}'")


def _press_info(press: PressProfile, engine: ColorEngine) -> dict:
    return {"name": press.name, "description": press.description, "file": press.file,
            "sha256": engine.profile_sha256, "tac": press.tac}


def _stages_2_3(pdf_path: Path, meta: dict, press: PressProfile, engine: ColorEngine, dev, cfg, out: Path) -> dict:
    """Analyze + check one PDF; writes inventory/findings/report.json into ``out``."""
    with pikepdf.open(pdf_path) as pdf:
        inventory = build_inventory(pdf, engine)
    findings = run_all(inventory, meta, press, dev, cfg)
    swatches = build_swatch_table(inventory, findings, cfg["cluster_de"])
    versions = CheckContext(inventory, meta, press, dev, cfg).versions()
    write_json(out / "inventory.json", inventory)
    write_json(out / "findings.json", findings)
    return {"inventory": inventory, "findings": findings, "swatches": swatches, "versions": versions}


def _job_json(job_dir, job_id, press_info, dev, cfg, meta, started, stages, extra=None):
    tools = {"pikepdf": pikepdf.__version__, "qpdf": pikepdf.__libqpdf_version__, "lcms2": lcms_version(),
             "python": platform.python_version()}
    if "render" in stages:
        from .render import gs_version
        tools["ghostscript"] = gs_version()
    previous = read_json(job_dir / "job.json") if (job_dir / "job.json").exists() else {}
    job = {
        "job_id": job_id, "engine_version": __version__, "status": "done", "stages": stages,
        "press": press_info, "device": dev, "config": cfg, "tools": tools, "ingest": meta,
        **(extra or {}),
        "started": started, "finished": datetime.now(timezone.utc).isoformat(),
    }
    for keep in ("approval", "submissions"):
        if keep in previous and previous.get("ingest", {}).get("working_sha256") == meta.get("working_sha256"):
            job[keep] = previous[keep]
    write_json(job_dir / "job.json", job)
    return job


def analyze(sources: list[Path], out_root: Path, profile: str | None = None, job_id: str | None = None,
            device: str | None = None, config: dict | None = None, order: list[int] | None = None) -> Path:
    """Stages 1-3: ingest, inventory, checks (phase 1 `proof analyze`)."""
    press = get_press_profile(profile)
    job_id = job_id or default_job_id(sources[0], press.name)
    job_dir = Path(out_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    working, meta = ingest([Path(s) for s in sources], job_dir, order=order, output_intent=press.output_intent())
    engine = ColorEngine(press)
    dev = load_device(device)
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    res = _stages_2_3(working, meta, press, engine, dev, cfg, job_dir)
    info = _press_info(press, engine)
    write_json(job_dir / "report.json", build_report(meta, res["findings"], res["swatches"], res["versions"], info))
    _job_json(job_dir, job_id, info, dev, cfg, meta, started, ["ingest", "analyze", "check"])
    return job_dir


def run(sources: list[Path], out_root: Path, profile: str | None = None, job_id: str | None = None,
        device: str | None = None, config: dict | None = None, order: list[int] | None = None,
        remap: str | dict | None = "auto", ppi: int = 150, separations: bool = False, bleed: bool = False,
        display_icc: Path | None = None, paper_white: bool = True, mode: str = "reference",
        progress: Callable[[float, str], None] | None = None) -> Path:
    """All five stages. With a remap the proof, report and top-level files describe normalized.pdf;
    the original's inventory and findings are kept under before/.

    ``mode="device"`` proofs through the device's own ICC when its manifest entry has one;
    otherwise the proof stays in reference mode (and `device-profile-missing` says so).
    """
    from .remap import apply_remap, derive_auto_table
    from .render import compare_pages, render_proofs

    step = progress or (lambda _f, _s: None)
    step(0.0, "ingest")
    press = get_press_profile(profile)
    job_id = job_id or default_job_id(sources[0], press.name)
    job_dir = Path(out_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    working, meta = ingest([Path(s) for s in sources], job_dir, order=order, output_intent=press.output_intent())
    engine = ColorEngine(press)
    dev = load_device(device)
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    info = _press_info(press, engine)

    step(0.05, "analyze")
    for stale in ("before", "proof", "compare"):
        shutil.rmtree(job_dir / stale, ignore_errors=True)
    (job_dir / "normalized.pdf").unlink(missing_ok=True)

    before = _stages_2_3(working, meta, press, engine, dev, cfg, job_dir)
    table = None
    if remap == "auto":
        table = derive_auto_table(before["findings"], press.name)
    elif isinstance(remap, (str, Path)) and remap not in ("none", ""):
        table = read_json(Path(remap))
    elif isinstance(remap, dict):
        table = remap
    if table and not table.get("map"):
        table = None

    proof_file, after, remap_info, note = working, before, None, []
    step(0.35, "remap" if table else "render")
    if table:
        normalized = job_dir / "normalized.pdf"
        applied = apply_remap(working, normalized, table, before["inventory"])
        (job_dir / "before").mkdir(exist_ok=True)
        for name in ("inventory.json", "findings.json"):
            shutil.move(job_dir / name, job_dir / "before" / name)
        with pikepdf.open(normalized) as pdf:
            from .ingest import inspect
            nmeta = dict(inspect(pdf), inputs=meta["inputs"], working_file="normalized.pdf",
                         working_sha256=sha256_file(normalized))
        if meta.get("merge"):
            nmeta["merge"] = meta["merge"]
        after = _stages_2_3(normalized, nmeta, press, engine, dev, cfg, job_dir)
        note = change_note(table, before["inventory"], meta)
        remap_info = {"table": table, "rewrites": applied["pages"], "normalized_sha256": nmeta["working_sha256"],
                      "change_note": note,
                      "before_counts": _counts(before["findings"]), "after_counts": _counts(after["findings"])}
        write_json(job_dir / "before" / "report.json",
                   build_report(meta, before["findings"], before["swatches"], before["versions"], info))
        proof_file = normalized

    step(0.55, "render")
    render_dev = dev if mode == "device" and dev and dev.get("device_profile_file") else None
    render_meta = render_proofs(proof_file, job_dir / "proof", press, meta, ppi=ppi, separations=separations,
                                bleed=bleed, display_icc=display_icc.read_bytes() if display_icc else None,
                                paper_white=paper_white, device=render_dev)
    if mode == "device" and not render_dev:
        render_meta["mode_note"] = "device mode requested but the device has no ICC; proofed in reference mode"
    step(0.85, "report")
    comparisons = []
    for f in before["findings"]:
        if f["rule"] == "cross-page-color-drift" and f.get("reference_page"):
            ref, page = f["reference_page"], f["pages"][0]
            a, b = sorted((ref, page))
            comparisons.append(compare_pages(working, a, b, job_dir / "compare" / f"p{a:03d}_vs_p{b:03d}.png",
                                             press, ppi))
            if table:
                comparisons.append(compare_pages(proof_file, a, b,
                                                 job_dir / "compare" / f"p{a:03d}_vs_p{b:03d}.after.png", press, ppi))

    report = build_report(meta, after["findings"], after["swatches"], after["versions"], info, remap_info)
    report["render"] = render_meta
    report["comparisons"] = comparisons
    write_json(job_dir / "report.json", report)
    write_html(job_dir, report, after["findings"], press, note)
    _job_json(job_dir, job_id, info, dev, cfg, meta, started,
              ["ingest", "analyze", "check", "render", "report"] + (["remap"] if table else []),
              {"proof_file": str(Path(proof_file).relative_to(job_dir)), "render": render_meta})
    step(1.0, "done")
    return job_dir


def _counts(findings):
    return {s: sum(f["severity"] == s for f in findings) for s in ("error", "warn", "info")}


def render_only(pdf: Path, out_root: Path, profile: str | None = None, job_id: str | None = None,
                ppi: int = 150, separations: bool = False, bleed: bool = False,
                display_icc: Path | None = None, paper_white: bool = True) -> Path:
    from .ingest import inspect
    from .render import render_proofs

    press = get_press_profile(profile)
    job_dir = Path(out_root) / (job_id or default_job_id(pdf, press.name))
    with pikepdf.open(pdf) as doc:
        meta = inspect(doc)
    render_proofs(Path(pdf), job_dir / "proof", press, meta, ppi=ppi, separations=separations, bleed=bleed,
                  display_icc=display_icc.read_bytes() if display_icc else None, paper_white=paper_white)
    return job_dir

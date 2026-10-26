"""Stage orchestration. Every stage writes into the job folder."""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path

import pikepdf

from . import __version__
from .checks import CheckContext, DEFAULT_CONFIG, run_all
from .cms import ColorEngine, get_press_profile, lcms_version
from .ingest import ingest, sha256_file
from .inventory import build_inventory
from .report import build_report
from .swatches import build_swatch_table


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


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


def analyze(sources: list[Path], out_root: Path, profile: str | None = None, job_id: str | None = None,
            device: str | None = None, config: dict | None = None, order: list[int] | None = None) -> Path:
    press = get_press_profile(profile)
    job_id = job_id or default_job_id(sources[0], press.name)
    job_dir = Path(out_root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()

    working, meta = ingest([Path(s) for s in sources], job_dir, order=order)
    engine = ColorEngine(press)
    with pikepdf.open(working) as pdf:
        inventory = build_inventory(pdf, engine)
    dev = load_device(device)
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    findings = run_all(inventory, meta, press, dev, cfg)
    swatches = build_swatch_table(inventory, findings, cfg["cluster_de"])
    versions = CheckContext(inventory, meta, press, dev, cfg).versions()

    press_info = {"name": press.name, "description": press.description, "file": press.file,
                  "sha256": engine.profile_sha256, "tac": press.tac}
    write_json(job_dir / "inventory.json", inventory)
    write_json(job_dir / "findings.json", findings)
    write_json(job_dir / "report.json", build_report(meta, findings, swatches, versions, press_info))
    job = {
        "job_id": job_id,
        "engine_version": __version__,
        "status": "done",
        "stages": ["ingest", "analyze", "check"],
        "press": press_info,
        "device": dev,
        "config": cfg,
        "tools": {"pikepdf": pikepdf.__version__, "qpdf": pikepdf.__libqpdf_version__, "lcms2": lcms_version(),
                  "python": platform.python_version()},
        "ingest": meta,
        "started": started,
        "finished": datetime.now(timezone.utc).isoformat(),
    }
    write_json(job_dir / "job.json", job)
    return job_dir

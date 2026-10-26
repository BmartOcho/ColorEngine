"""Approval and file integrity: what the customer approved is exactly what prints."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .checks import run_all
from .cms import get_press_profile
from .ingest import sha256_file


def _load(job_dir: Path) -> dict:
    return json.loads((job_dir / "job.json").read_text())


def _save(job_dir: Path, job: dict) -> None:
    (job_dir / "job.json").write_text(json.dumps(job, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def approve(job_dir: Path, approver: str, accept_remap: bool = True) -> dict:
    """Record the SHA-256 of the approved file.

    With a remap, approving means approving normalized.pdf; rejecting the remap
    approves the original as supplied.
    """
    job_dir = Path(job_dir)
    job = _load(job_dir)
    normalized = job_dir / "normalized.pdf"
    if normalized.exists() and accept_remap:
        target, remap = normalized, "accepted"
    else:
        target = job_dir / job["ingest"]["working_file"]
        remap = "rejected: difference approved as supplied" if normalized.exists() else "none"
    job["approval"] = {
        "file": str(target.relative_to(job_dir)),
        "sha256": sha256_file(target),
        "approver": approver,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "remap": remap,
    }
    _save(job_dir, job)
    return job["approval"]


def verify_submission(job_dir: Path, submitted: Path) -> list[dict]:
    """Hash a file sent to press or plate; returns `unapproved-file` findings on mismatch."""
    job_dir = Path(job_dir)
    job = _load(job_dir)
    digest = sha256_file(submitted)
    meta = dict(job["ingest"], approval=job.get("approval"), submitted_sha256=digest)
    if not job.get("approval"):
        findings = [{"rule": "unapproved-file", "severity": "error", "pages": [], "objects": [],
                     "detail": "No approval recorded for this job", "submitted": digest}]
    else:
        findings = run_all([], meta, get_press_profile(job["press"]["name"]), only=["unapproved-file"])
    job.setdefault("submissions", []).append({
        "file": Path(submitted).name, "sha256": digest, "at": datetime.now(timezone.utc).isoformat(),
        "ok": not findings})
    _save(job_dir, job)
    return findings

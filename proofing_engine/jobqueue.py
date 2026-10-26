"""In-process job queue for the LAN service.

One proofing station, one process: a small thread pool runs pipeline jobs off
the request thread. State lives in each job folder's ``status.json`` so a
restart (Windows service recovery) re-queues anything that was queued or
running. No Redis needed; the queue interface is small enough to put RQ behind
later if a second station is added.
"""

from __future__ import annotations

import json
import logging
import threading
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("proofing_engine.queue")

ACTIVE = ("queued", "running")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobQueue:
    def __init__(self, root: Path, workers: int = 1):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="proof-job")
        self.lock = threading.Lock()

    # status ---------------------------------------------------------------
    def status_path(self, job_id: str) -> Path:
        return self.root / job_id / "status.json"

    def status(self, job_id: str) -> dict | None:
        p = self.status_path(job_id)
        return json.loads(p.read_text()) if p.exists() else None

    def _update(self, job_id: str, **fields) -> dict:
        with self.lock:
            st = self.status(job_id) or {"job_id": job_id}
            st.update(fields)
            p = self.status_path(job_id)
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=1))
            tmp.replace(p)
            return st

    # submission -----------------------------------------------------------
    def submit(self, job_id: str, params: dict, submitted_by: str | None = None) -> dict:
        st = self._update(job_id, status="queued", progress=0.0, stage="queued", params=params,
                          submitted_by=submitted_by, queued_at=_now(), started_at=None, finished_at=None,
                          error=None)
        self.pool.submit(self._execute, job_id)
        return st

    def recover(self) -> list[str]:
        """Re-queue jobs left queued or running by a previous process."""
        resumed = []
        for p in sorted(self.root.glob("*/status.json")):
            st = json.loads(p.read_text())
            if st.get("status") in ACTIVE:
                self._update(st["job_id"], status="queued", stage="queued (recovered)")
                self.pool.submit(self._execute, st["job_id"])
                resumed.append(st["job_id"])
        return resumed

    def shutdown(self, wait: bool = True) -> None:
        self.pool.shutdown(wait=wait)

    # execution ------------------------------------------------------------
    def _execute(self, job_id: str) -> None:
        from .pipeline import run

        st = self._update(job_id, status="running", started_at=_now(), stage="ingest")
        params = dict(st["params"])
        job_dir = self.root / job_id
        sources = [job_dir / "input" / n for n in params.pop("inputs")]
        display = params.pop("display_profile", None)
        callback = params.pop("callback_url", None)

        def progress(frac: float, stage: str) -> None:
            self._update(job_id, progress=round(frac, 2), stage=stage)

        try:
            run(sources, self.root, job_id=job_id, progress=progress,
                display_icc=Path(display) if display else None, **params)
            final = self._update(job_id, status="done", progress=1.0, stage="done", finished_at=_now())
        except Exception as exc:  # noqa: BLE001 - any failure is reported on the job
            log.exception("job %s failed", job_id)
            final = self._update(job_id, status="failed", stage="failed", finished_at=_now(),
                                 error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc(limit=8))
        if callback:
            self._notify(job_id, callback, final)

    def _notify(self, job_id: str, url: str, st: dict) -> None:
        body = {"job_id": job_id, "status": st["status"], "error": st.get("error")}
        report = self.root / job_id / "report.json"
        if st["status"] == "done" and report.exists():
            r = json.loads(report.read_text())
            body.update(counts=r["counts"], blocking=r["blocking"])
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                self._update(job_id, callback={"url": url, "status": resp.status, "at": _now()})
        except Exception as exc:  # noqa: BLE001
            self._update(job_id, callback={"url": url, "error": str(exc), "at": _now()})

"""LAN HTTP API over the pipeline (FastAPI). The API adds a job queue and nothing else.

Configuration (environment):
  PROOF_JOBS_DIR          job folder root (an SMB share on the station)       default ./jobs
  PROOF_API_TOKENS        "name:token,name:token": one bearer token per calling system (required)
  PROOF_WORKERS           concurrent jobs                                     default 1
  PROOF_MAX_UPLOAD_MB     per-file upload limit                               default 500
  PROOF_DISPLAY_PROFILE   monitor ICC for `display=station` soft proofs (EIZO CG2700X profile)
"""

from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .jobqueue import JobQueue

JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


def _tokens() -> dict[str, str]:
    raw = os.environ.get("PROOF_API_TOKENS", "")
    out = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        name, _, token = part.partition(":")
        if not token:
            raise RuntimeError("PROOF_API_TOKENS entries must be name:token")
        out[token] = name
    return out


def create_app(jobs_dir: Path | None = None, workers: int | None = None) -> FastAPI:
    root = Path(jobs_dir or os.environ.get("PROOF_JOBS_DIR", "jobs")).resolve()
    tokens = _tokens()
    if not tokens:
        raise RuntimeError("PROOF_API_TOKENS is empty; refusing to start without auth")
    max_bytes = int(os.environ.get("PROOF_MAX_UPLOAD_MB", "500")) * 1024 * 1024
    queue = JobQueue(root, workers or int(os.environ.get("PROOF_WORKERS", "1")))

    @asynccontextmanager
    async def lifespan(_app):
        queue.recover()
        yield
        queue.shutdown(wait=False)

    app = FastAPI(title="Voom proofing engine", version=__version__, lifespan=lifespan)
    app.state.queue = queue

    def caller(authorization: str | None = Header(default=None)) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})
        given = authorization[7:].strip()
        for token, name in tokens.items():
            if hmac.compare_digest(given.encode(), token.encode()):
                return name
        raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})

    def job_dir(job_id: str) -> Path:
        if not JOB_ID.match(job_id):
            raise HTTPException(400, "bad job id")
        d = (root / job_id).resolve()
        if d.parent != root or not d.is_dir():
            raise HTTPException(404, "no such job")
        return d

    def read(path: Path):
        if not path.exists():
            raise HTTPException(404, f"{path.name} not available yet")
        return json.loads(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    @app.get("/health")
    def health():
        from .cms import lcms_version
        from .render import gs_version
        return {"ok": True, "version": __version__, "ghostscript": gs_version(), "lcms2": lcms_version()}

    @app.get("/profiles")
    def profiles(_who: str = Depends(caller)):
        from .cms import PROFILE_DIR, load_manifest
        return {"press": load_manifest(), "devices": json.loads((PROFILE_DIR / "devices.json").read_text())}

    @app.post("/jobs", status_code=202)
    async def create_job(
        files: list[UploadFile] = File(...),
        profile: str | None = Form(None),
        ppi: int = Form(150),
        remap: str = Form("auto"),
        separations: bool = Form(False),
        bleed: bool = Form(False),
        device: str | None = Form(None),
        mode: str = Form("reference"),
        order: str | None = Form(None),
        display: str = Form("srgb"),
        ink_only: bool = Form(False),
        callback_url: str | None = Form(None),
        who: str = Depends(caller),
    ):
        from .cms import get_press_profile
        from .pipeline import load_device

        try:
            press = get_press_profile(profile)
            load_device(device)
        except KeyError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not 36 <= ppi <= 600:
            raise HTTPException(422, "ppi must be 36-600")
        if mode not in ("reference", "device"):
            raise HTTPException(422, "mode must be reference or device")
        if display not in ("srgb", "station"):
            raise HTTPException(422, "display must be srgb or station")
        remap_value: str | dict = remap
        if remap not in ("auto", "none"):
            try:
                remap_value = json.loads(remap)
                assert isinstance(remap_value, dict) and isinstance(remap_value.get("map"), list)
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(422, "remap must be auto, none or a remap table JSON") from exc
        display_profile = None
        if display == "station":
            display_profile = os.environ.get("PROOF_DISPLAY_PROFILE")
            if not display_profile or not Path(display_profile).exists():
                raise HTTPException(422, "PROOF_DISPLAY_PROFILE is not configured on the station")

        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(files[0].filename or "upload").stem)[:40] or "upload"
        job_id = f"{stem}-{uuid.uuid4().hex[:10]}"
        d = root / job_id / "input"
        d.mkdir(parents=True)
        try:
            names = await _save_uploads(files, d, max_bytes)
        except HTTPException:
            shutil.rmtree(root / job_id, ignore_errors=True)
            raise
        params = {"inputs": names, "profile": press.name, "ppi": ppi, "remap": remap_value,
                  "separations": separations, "bleed": bleed, "device": device, "mode": mode,
                  "order": [int(x) for x in order.split(",")] if order else None,
                  "display_profile": display_profile, "paper_white": not ink_only, "callback_url": callback_url}
        st = queue.submit(job_id, params, submitted_by=who)
        return {"job_id": job_id, "status": st["status"]}

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, _who: str = Depends(caller)):
        d = job_dir(job_id)
        st = queue.status(job_id) or {"status": "done" if (d / "job.json").exists() else "unknown"}
        job = json.loads((d / "job.json").read_text()) if (d / "job.json").exists() else {}
        return {**job, "status": st.get("status"), "progress": st.get("progress"), "stage": st.get("stage"),
                "error": st.get("error"), "submitted_by": st.get("submitted_by")}

    @app.get("/jobs/{job_id}/findings")
    def findings(job_id: str, severity: str | None = Query(None), _who: str = Depends(caller)):
        data = read(job_dir(job_id) / "findings.json")
        return [f for f in data if not severity or f["severity"] == severity]

    @app.get("/jobs/{job_id}/inventory")
    def inventory(job_id: str, page: int | None = Query(None), _who: str = Depends(caller)):
        data = read(job_dir(job_id) / "inventory.json")
        return [r for r in data if page is None or r["page"] == page]

    @app.get("/jobs/{job_id}/proof/{name}")
    def proof(job_id: str, name: str, sep: str | None = Query(None), _who: str = Depends(caller)):
        m = re.fullmatch(r"(\d{1,4})\.png", name)
        if not m:
            raise HTTPException(404, "expected /proof/<page>.png")
        if sep and not re.fullmatch(r"[a-z0-9-]{1,80}", sep):
            raise HTTPException(400, "bad separation name")
        f = job_dir(job_id) / "proof" / (f"p{int(m.group(1)):03d}" + (f".{sep}" if sep else "") + ".png")
        if not f.exists():
            raise HTTPException(404, "no such proof image")
        return FileResponse(f, media_type="image/png")

    @app.get("/jobs/{job_id}/report.html")
    def report(job_id: str, _who: str = Depends(caller)):
        f = job_dir(job_id) / "report.html"
        if not f.exists():
            raise HTTPException(404, "report not available yet")
        return FileResponse(f, media_type="text/html")

    @app.get("/jobs/{job_id}/normalized.pdf")
    def normalized(job_id: str, _who: str = Depends(caller)):
        f = job_dir(job_id) / "normalized.pdf"
        if not f.exists():
            raise HTTPException(404, "no remap ran for this job")
        return FileResponse(f, media_type="application/pdf", filename=f"{job_id}_normalized.pdf")

    @app.post("/jobs/{job_id}/remap", status_code=202)
    def remap_job(job_id: str, table: dict, who: str = Depends(caller)):
        d = job_dir(job_id)
        st = queue.status(job_id)
        if st and st.get("status") in ("queued", "running"):
            raise HTTPException(409, "job is still running")
        if not isinstance(table.get("map"), list):
            raise HTTPException(422, "remap table needs a map list")
        params = dict(st["params"]) if st else {"inputs": sorted(p.name for p in (d / "input").glob("*.pdf"))}
        params["remap"] = table
        return {"job_id": job_id, "status": queue.submit(job_id, params, submitted_by=who)["status"]}

    @app.post("/jobs/{job_id}/approve")
    def approve_job(job_id: str, body: dict, _who: str = Depends(caller)):
        from .approval import approve
        if not body.get("approver"):
            raise HTTPException(422, "approver required")
        d = job_dir(job_id)
        if not (d / "job.json").exists():
            raise HTTPException(409, "job has not finished")
        return approve(d, body["approver"], accept_remap=body.get("accept_remap", True))

    @app.post("/jobs/{job_id}/verify")
    async def verify_job(job_id: str, file: UploadFile = File(...), _who: str = Depends(caller)):
        from .approval import verify_submission
        d = job_dir(job_id)
        sub = d / "submissions"
        sub.mkdir(exist_ok=True)
        target = sub / f"{uuid.uuid4().hex[:8]}_{Path(file.filename or 'submitted.pdf').name}"
        with open(target, "wb") as out:
            while chunk := await file.read(1 << 20):
                out.write(chunk)
        found = verify_submission(d, target)
        return JSONResponse({"ok": not found, "findings": found}, status_code=200)

    return app


async def _save_uploads(files, d: Path, max_bytes: int) -> list[str]:
    names = []
    for i, f in enumerate(files):
        name = Path(f.filename or f"upload{i}.pdf").name
        if not name.lower().endswith(".pdf"):
            raise HTTPException(422, f"{name}: only PDF uploads are accepted")
        if name in names:
            raise HTTPException(422, f"duplicate file name {name}")
        size = 0
        with open(d / name, "wb") as out:
            while chunk := await f.read(1 << 20):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(413, f"{name} exceeds the upload limit")
                out.write(chunk)
        with open(d / name, "rb") as check:
            if check.read(5) != b"%PDF-":
                raise HTTPException(422, f"{name} is not a PDF")
        names.append(name)
    return names


def serve(host: str = "127.0.0.1", port: int = 8750, jobs_dir: Path | None = None, workers: int | None = None):
    import uvicorn

    uvicorn.run(create_app(jobs_dir, workers), host=host, port=port, log_level="info")

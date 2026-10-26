"""Phase 3: merge (12), spot rendering (14), device mode, LAN API."""

import json
import time

import numpy as np
import pikepdf
import pytest
from conftest import Job, run_full
from fastapi.testclient import TestClient
from fixtures import build

from proofing_engine.cms import ColorEngine, ImageCMS, get_press_profile
from proofing_engine.ingest import inspect, merge_pdfs
from proofing_engine.pipeline import analyze
from proofing_engine.render import render_cmyk, render_proofs, render_separations

# --- 12 merge -------------------------------------------------------------------


def test_12_merge_order_and_single_intent(tmp_path):
    a, b = build.merge_sources(tmp_path)
    press = get_press_profile()
    info = merge_pdfs([a, b], tmp_path / "merged.pdf", order=[1, 3, 2], output_intent=press.output_intent())
    assert [(p["source"], p["page"]) for p in info["page_map"]] == [
        ("badges_a.pdf", 1), ("badge_b.pdf", 1), ("badges_a.pdf", 2)]
    with pikepdf.open(tmp_path / "merged.pdf") as pdf:
        meta = inspect(pdf)
    assert [o["output_condition_identifier"] for o in meta["output_intents"]] == ["CGATS21-2-CRPC6"]
    assert meta["output_intents"][0]["embedded_profile"]["description"] == "GRACoL2013_CRPC6.icc"
    assert meta["pdfx_version"] == "PDF/X-1a:2003"


def test_12_multiple_output_intents_reports_sources(tmp_path):
    a, b = build.merge_sources(tmp_path)
    job = Job(analyze([a, b], tmp_path / "jobs", job_id="merged", order=[1, 3, 2]))
    f = job.rule("multiple-output-intents")
    assert len(f) == 1
    assert f[0]["sources"] == {"badges_a.pdf": ["CGATS TR 001"], "badge_b.pdf": ["CGATS21-2-CRPC6"]}
    assert f[0]["written"] == "CGATS21-2-CRPC6"
    assert job.job["ingest"]["merge"]["order"] == [1, 3, 2]
    assert job.rule("intent-mismatch") == []  # the engine wrote the press condition's intent


def test_12_brightspot_split_and_remerge(brightspot_pdf, tmp_path):
    """Acrobat-order reproduction: p10 as its own source, merged back in place."""
    with pikepdf.open(brightspot_pdf) as src:
        rest, p10 = pikepdf.new(), pikepdf.new()
        for i, page in enumerate(src.pages):
            (p10 if i == 9 else rest).pages.append(page)
        rest.save(tmp_path / "rest.pdf")
        p10.save(tmp_path / "p10.pdf")
    order = [*range(1, 10), 64, *range(10, 64)]
    merge_pdfs([tmp_path / "rest.pdf", tmp_path / "p10.pdf"], tmp_path / "m.pdf", order=order,
               output_intent=get_press_profile().output_intent())
    with pikepdf.open(brightspot_pdf) as a, pikepdf.open(tmp_path / "m.pdf") as b:
        assert len(b.pages) == 64
        for i in range(64):
            assert a.pages[i].obj.Contents.read_bytes() == b.pages[i].obj.Contents.read_bytes()
        assert len(b.Root.OutputIntents) == 1


# --- 14 spots -------------------------------------------------------------------


def test_14_spots_render_from_lab(tmp_path):
    pdf = build.fixture5_spots(tmp_path / "f5.pdf")
    press = get_press_profile()
    ink = render_cmyk(pdf, 72, "media", [1], press)[1]
    expect = [round(v * 2.55) for v in ColorEngine(press).lab_to_cmyk(build.SPOTS["PANTONE 1795 C"])]
    got = ink[180 - 50, 70].astype(int)
    assert np.abs(got - expect).max() <= 2
    plates = render_separations(pdf, 72, "media", [1], press)[1]
    assert {"PANTONE 1795 C", "PANTONE 295 C", "Cyan", "Magenta", "Yellow", "Black"} <= set(plates)
    assert plates["PANTONE 1795 C"][130, 70] == 0  # solid spot (0 = full ink)
    assert plates["PANTONE 295 C"][130, 180] == 128  # 50% tint
    assert plates["PANTONE 1795 C"][130, 180] == 255


def test_spot_plates_written_as_separations(tmp_path):
    pdf = build.fixture5_spots(tmp_path / "f5.pdf")
    with pikepdf.open(pdf) as doc:
        meta = inspect(doc)
    info = render_proofs(pdf, tmp_path / "p", get_press_profile(), meta, ppi=36, separations=True)
    assert info["plates"]["pantone-1795-c"] == "PANTONE 1795 C"
    assert (tmp_path / "p" / "p001.pantone-1795-c.png").exists()
    assert (tmp_path / "p" / "p001.cyan.png").exists()


# --- device mode ------------------------------------------------------------------


def test_device_mode_changes_the_proof(tmp_path):
    pdf = build.fixture4_tac(tmp_path / "f4.pdf")
    with pikepdf.open(pdf) as doc:
        meta = inspect(doc)
    press = get_press_profile()
    ref = render_proofs(pdf, tmp_path / "ref", press, meta, ppi=36)
    # Stand-in device ICC until the Fiery profile is exported.
    dev = {"id": "test-device", "device_profile_file": "SWOP2013C3_CRPC5.icc"}
    dv = render_proofs(pdf, tmp_path / "dev", press, meta, ppi=36, device=dev)
    assert ref["mode"] == "reference" and dv["mode"] == "device"
    a = np.asarray(__import__("PIL.Image").Image.open(tmp_path / "ref" / "p001.png"))
    b = np.asarray(__import__("PIL.Image").Image.open(tmp_path / "dev" / "p001.png"))
    assert not np.array_equal(a, b)


def test_device_without_icc_falls_back_to_reference(tmp_path):
    pdf = build.fixture4_tac(tmp_path / "f4.pdf")
    job = run_full(pdf, tmp_path / "jobs", job_id="d", device="offset-1", mode="device", ppi=36)
    assert job.report["render"]["mode"] == "reference"
    assert "mode_note" in job.report["render"]
    assert [f["rule"] for f in job.findings if f["rule"] == "device-profile-missing"] == ["device-profile-missing"]


def test_device_cms_identity_is_not_applied_in_reference_mode():
    press = get_press_profile()
    px = np.array([[[36, 174, 203, 58]]], np.uint8)
    assert ImageCMS(press).to_rgb(px).shape == (1, 1, 3)


# --- API ----------------------------------------------------------------------------

TOKEN = "test-token-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROOF_API_TOKENS", f"printssistant:{TOKEN}")
    from proofing_engine.api import create_app
    app = create_app(tmp_path / "jobs", workers=1)
    with TestClient(app) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def _wait(client, job_id, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        st = client.get(f"/jobs/{job_id}").json()
        if st["status"] in ("done", "failed"):
            return st
        time.sleep(0.2)
    raise TimeoutError(job_id)


def test_api_requires_token(client):
    r = client.get("/profiles", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert client.get("/health", headers={}).json()["ok"] is True


def test_api_job_lifecycle(client, tmp_path):
    pdf = build.fixture4_tac(tmp_path / "f4.pdf")
    with open(pdf, "rb") as fh:
        r = client.post("/jobs", files={"files": ("f4.pdf", fh, "application/pdf")},
                        data={"ppi": "36", "separations": "true"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    st = _wait(client, job_id)
    assert st["status"] == "done" and st["progress"] == 1.0 and st["submitted_by"] == "printssistant"
    errors = client.get(f"/jobs/{job_id}/findings", params={"severity": "error"}).json()
    assert {f["rule"] for f in errors} == {"tac-over-limit"}
    inv = client.get(f"/jobs/{job_id}/inventory", params={"page": 1}).json()
    assert inv and all(r["page"] == 1 for r in inv)
    png = client.get(f"/jobs/{job_id}/proof/1.png")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert client.get(f"/jobs/{job_id}/proof/1.png", params={"sep": "black"}).status_code == 200
    assert client.get(f"/jobs/{job_id}/report.html").text.startswith("<!doctype html>")
    assert client.get(f"/jobs/{job_id}/normalized.pdf").status_code == 404

    # Operator-supplied remap re-runs the job on the same inputs.
    table = {"profile": "gracol2013", "map": [{"from": [100, 100, 100, 100], "to": [60, 40, 40, 100], "pages": [1]}]}
    assert client.post(f"/jobs/{job_id}/remap", json=table).status_code == 202
    assert _wait(client, job_id)["status"] == "done"
    assert client.get(f"/jobs/{job_id}/normalized.pdf").status_code == 200
    assert client.get(f"/jobs/{job_id}/findings", params={"severity": "error"}).json() == []

    appr = client.post(f"/jobs/{job_id}/approve", json={"approver": "csr@voom"}).json()
    assert appr["file"] == "normalized.pdf"
    with open(pdf, "rb") as fh:
        v = client.post(f"/jobs/{job_id}/verify", files={"file": ("f4.pdf", fh, "application/pdf")}).json()
    assert v["ok"] is False and v["findings"][0]["rule"] == "unapproved-file"


def test_api_rejects_bad_input(client, tmp_path):
    r = client.post("/jobs", files={"files": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 422
    assert not any((tmp_path / "jobs").glob("x-*")), "failed uploads leave no job folder"
    r = client.post("/jobs", files={"files": ("x.txt", b"%PDF-1.4", "text/plain")})
    assert r.status_code == 422
    assert client.get("/jobs/..%2Fetc").status_code in (400, 404)
    assert client.get("/jobs/nope").status_code == 404
    with open(build.fixture4_tac(tmp_path / "f4.pdf"), "rb") as fh:
        r = client.post("/jobs", files={"files": ("f4.pdf", fh, "application/pdf")}, data={"profile": "nope"})
    assert r.status_code == 422


def test_api_merge_upload(client, tmp_path):
    a, b = build.merge_sources(tmp_path)
    with open(a, "rb") as fa, open(b, "rb") as fb:
        r = client.post("/jobs", files=[("files", ("badges_a.pdf", fa, "application/pdf")),
                                        ("files", ("badge_b.pdf", fb, "application/pdf"))],
                        data={"order": "1,3,2", "ppi": "36", "remap": "none"})
    job_id = r.json()["job_id"]
    st = _wait(client, job_id)
    assert st["status"] == "done", st
    assert st["ingest"]["page_count"] == 3
    rules = {f["rule"] for f in client.get(f"/jobs/{job_id}/findings").json()}
    assert "multiple-output-intents" in rules


def test_queue_recovers_interrupted_jobs(tmp_path, monkeypatch):
    from proofing_engine.jobqueue import JobQueue

    root = tmp_path / "jobs"
    pdf = build.fixture4_tac(tmp_path / "f4.pdf")
    (root / "j1" / "input").mkdir(parents=True)
    (root / "j1" / "input" / "f4.pdf").write_bytes(pdf.read_bytes())
    (root / "j1" / "status.json").write_text(json.dumps({
        "job_id": "j1", "status": "running", "params": {"inputs": ["f4.pdf"], "ppi": 36, "remap": "none"}}))
    q = JobQueue(root, workers=1)
    assert q.recover() == ["j1"]
    q.shutdown(wait=True)
    assert q.status("j1")["status"] == "done"


@pytest.mark.parametrize("icc,flagged", [("GRACoL2013_CRPC6.icc", False), ("SWOP2013C3_CRPC5.icc", True)])
def test_iccbased_cmyk_press_passthrough(tmp_path, icc, flagged):
    """ICCBased N=4 tagged with the press profile passes through; any other CMYK profile is `cmyk-retagged`."""
    from proofing_engine.cms import PROFILE_DIR

    pdf = build._base(pdfx=None)
    stream = pdf.make_stream((PROFILE_DIR / "icc" / icc).read_bytes(), N=4)
    res = {"/ColorSpace": pikepdf.Dictionary(CS0=pikepdf.Array([pikepdf.Name.ICCBased, stream]))}
    build._page(pdf, b"/CS0 cs 0.1 0.6 0.8 0.1 sc 9 9 234 162 re f\n", res)
    pdf.save(tmp_path / "icc.pdf")
    job = Job(analyze([tmp_path / "icc.pdf"], tmp_path / "jobs"))
    rec = job.inventory[0]
    assert ("cmyk-retagged" in rec.get("flags", [])) is flagged
    if not flagged:
        assert rec["cmyk"] == [10.0, 60.0, 80.0, 10.0]

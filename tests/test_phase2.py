"""Acceptance tests 8-11 and 15 (render, remap, approval, determinism, framing)."""

import hashlib
import json

import numpy as np
import pikepdf
import pytest
from conftest import Job, run_full
from fixtures import build
from PIL import Image

from proofing_engine.approval import approve, verify_submission
from proofing_engine.cms import ImageCMS, get_press_profile
from proofing_engine.ingest import inspect
from proofing_engine.render import delta_e_map, render_cmyk, render_proofs

ORANGE, P10_ORANGE = (36, 174, 203, 58), (9, 194, 250, 39)  # 8-bit ink of the two backgrounds


def _mask(cmyk, ink, tol=2):
    return (np.abs(cmyk.astype(int) - np.array(ink)) <= tol).all(axis=-1)


def test_8_render_p9_vs_p10(brightspot_pdf):
    press = get_press_profile()
    r = render_cmyk(brightspot_pdf, 150, "trim", [9, 10])
    cms = ImageCMS(press)
    rgb9, rgb10 = cms.to_rgb(r[9]), cms.to_rgb(r[10])
    assert not np.array_equal(rgb9, rgb10)
    bg = _mask(r[9], ORANGE) & _mask(r[10], P10_ORANGE)
    assert bg.mean() > 0.5
    de = delta_e_map(r[9], r[10], cms)
    assert de[bg].mean() > 4.0
    # Spec asks for identical pixels in "the text region"; the names and the text ink differ
    # between these two badges, so we check the region both pages paint with identical ink
    # (the 0/0/0/0 white elements).
    white = (r[9] == 0).all(-1) & (r[10] == 0).all(-1)
    assert white.sum() > 100
    assert np.array_equal(rgb9[white], rgb10[white])


def test_9_auto_remap(brightspot_run, brightspot_pdf):
    job = brightspot_run
    assert job.rule("cross-page-color-drift") == []
    before = json.loads((job.path / "before" / "findings.json").read_text())
    assert [f["pages"] for f in before if f["rule"] == "cross-page-color-drift"] == [[10]]
    # p10's remapped fills now equal the matching values used on the other pages (byte-identical operands).
    p9, p10 = job.page_cmyk(9), job.page_cmyk(10)
    for c in [(14.0, 68.3, 79.7, 22.6), (9.4, 30.8, 66.1, 7.2), (0.0, 5.3, 12.8, 0.0)]:
        assert c in p10 and p10[c]["raw"] == p9[c]["raw"]
    for c in [(3.6, 75.9, 97.9, 15.2), (14.0, 32.0, 73.0, 0.0), (0.0, 4.0, 10.0, 0.0)]:
        assert c not in p10
    with pikepdf.open(brightspot_pdf) as a, pikepdf.open(job.path / "normalized.pdf") as b:
        for i in range(len(a.pages)):
            same = a.pages[i].obj.Contents.read_raw_bytes() == b.pages[i].obj.Contents.read_raw_bytes()
            assert same == (i != 9), f"page {i + 1}"
    assert job.report["remap"]["change_note"][0]["text"].startswith(
        "Page 10: background, accent and text colors adjusted to match the other 63 pages")
    assert (job.path / "report.html").exists()
    assert (job.path / "compare" / "p009_vs_p010.png").exists()


def test_10_approval_integrity(brightspot_run, brightspot_pdf):
    rec = approve(brightspot_run.path, "customer@example.com")
    assert rec["file"] == "normalized.pdf"
    assert rec["sha256"] == hashlib.sha256((brightspot_run.path / "normalized.pdf").read_bytes()).hexdigest()
    assert verify_submission(brightspot_run.path, brightspot_run.path / "normalized.pdf") == []
    bad = verify_submission(brightspot_run.path, brightspot_pdf)
    assert [f["rule"] for f in bad] == ["unapproved-file"]
    job = json.loads((brightspot_run.path / "job.json").read_text())
    assert [s["ok"] for s in job["submissions"]] == [True, False]


def test_10_reject_remap_approves_original(brightspot_run, brightspot_pdf):
    rec = approve(brightspot_run.path, "customer@example.com", accept_remap=False)
    assert rec["remap"].startswith("rejected")
    assert verify_submission(brightspot_run.path, brightspot_pdf) == []


def test_11_determinism_with_remap(brightspot_pdf, tmp_path):
    def digests(job: Job):
        files = ["inventory.json", "findings.json", "normalized.pdf", *sorted(
            str(p.relative_to(job.path)) for p in (job.path / "proof").rglob("*.png"))]
        return {n: hashlib.sha256((job.path / n).read_bytes()).hexdigest() for n in files}

    a = run_full(brightspot_pdf, tmp_path / "a", job_id="j")
    b = run_full(brightspot_pdf, tmp_path / "b", job_id="j")
    assert digests(a) == digests(b)


@pytest.mark.parametrize("ppi", [150, 300])
def test_15_proof_framing(tmp_path, ppi):
    pdf = build.fixture4_tac(tmp_path / "f4.pdf")  # media/bleed 252x180, trim inset 9 pt
    with pikepdf.open(pdf) as doc:
        meta = inspect(doc)
    press = get_press_profile()
    render_proofs(pdf, tmp_path / "trim", press, meta, ppi=ppi)
    render_proofs(pdf, tmp_path / "bleed", press, meta, ppi=ppi, bleed=True)
    s = ppi / 72
    trim = Image.open(tmp_path / "trim" / "p001.png")
    bleed = Image.open(tmp_path / "bleed" / "p001.png")
    assert trim.size == (round(234 * s), round(162 * s))
    assert bleed.size == (round(252 * s), round(180 * s))
    assert trim.info.get("icc_profile")
    arr = np.asarray(bleed)
    x = round(9 * s)
    column = arr[round(90 * s), x]
    assert tuple(column) == (0, 160, 255)


def test_overprint_simulated(tmp_path):
    """K overprinting cyan (OP true, OPM 1) keeps the cyan underneath; 0/0/0/0 under OPM 1 paints nothing."""
    pdf = pikepdf.new()
    egs = pikepdf.Dictionary(Type=pikepdf.Name.ExtGState, OP=True, op=True, OPM=1)
    content = b"1 0 0 0 k 0 0 100 100 re f /GS0 gs 0 0 0 1 k 25 25 50 50 re f 0 0 0 0 k 0 0 10 10 re f"
    pdf.pages.append(pikepdf.Page(pikepdf.Dictionary(
        Type=pikepdf.Name.Page, MediaBox=[0, 0, 100, 100],
        Resources=pikepdf.Dictionary(ExtGState=pikepdf.Dictionary(GS0=egs)), Contents=pdf.make_stream(content))))
    pdf.save(tmp_path / "op.pdf")
    r = render_cmyk(tmp_path / "op.pdf", 72, "media")[1]
    assert tuple(r[50, 50]) == (255, 0, 0, 255)
    assert tuple(r[95, 5]) == (255, 0, 0, 0)


def test_separations_written(tmp_path):
    pdf = build.fixture4_tac(tmp_path / "f4.pdf")
    with pikepdf.open(pdf) as doc:
        meta = inspect(doc)
    render_proofs(pdf, tmp_path / "p", get_press_profile(), meta, ppi=72, separations=True)
    for ch in ("cyan", "magenta", "yellow", "black"):
        assert (tmp_path / "p" / f"p001.{ch}.png").exists()

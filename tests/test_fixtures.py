"""Acceptance tests on engine-built fixtures (2, 4, 5) plus unit checks."""

from conftest import run_job
from fixtures import build

from proofing_engine.functions import PDFFunction


def test_7_rgb_in_pdfx1a_fires(tmp_path):
    job = run_job(build.fixture2_rgb(tmp_path / "f2.pdf"), tmp_path / "jobs")
    f = job.rule("rgb-in-pdfx1a")
    assert len(f) == 1 and f[0]["severity"] == "error"
    spaces = {r["space"] for r in job.inventory}
    assert {"ICCBased N=3", "DeviceRGB"} <= spaces
    assert all("rgb-object" in r["flags"] for r in job.inventory if r["space"] != "DeviceCMYK")


def test_13_tac_over_limit(tmp_path):
    job = run_job(build.fixture4_tac(tmp_path / "f4.pdf"), tmp_path / "jobs")
    f = job.rule("tac-over-limit")
    assert len(f) == 1
    assert f[0]["pages"] == [1] and f[0]["tac"] == 400.0 and f[0]["limit"] == 320


def test_rich_black_inconsistent(tmp_path):
    job = run_job(build.fixture4_tac(tmp_path / "f4.pdf"), tmp_path / "jobs")
    assert len(job.rule("rich-black-inconsistent")) == 1


def test_14_spots_listed_from_lab(tmp_path):
    job = run_job(build.fixture5_spots(tmp_path / "f5.pdf"), tmp_path / "jobs")
    spots = [r for r in job.inventory if r.get("colorants")]
    assert {r["colorants"][0] for r in spots} == set(build.SPOTS)
    assert all(r["spot_source"] == "lab-alternate" for r in spots)
    solid = [r for r in spots if r["colorants"] == ["PANTONE 1795 C"]][0]
    assert solid["lab"] == build.SPOTS["PANTONE 1795 C"]
    assert job.rule("spot-vs-process-duplicate") == []


def test_14_spot_vs_process_duplicate(tmp_path):
    job = run_job(build.fixture5_spots(tmp_path / "f5b.pdf", with_process_duplicate=True), tmp_path / "jobs")
    f = job.rule("spot-vs-process-duplicate")
    assert len(f) == 1 and f[0]["colorant"] == "PANTONE 1795 C"


def test_postscript_tint_transform():
    import pikepdf

    pdf = pikepdf.new()
    fn = pdf.make_stream(b"{dup 0.5 mul exch dup 0.25 mul exch 0 exch}", FunctionType=4,
                         Domain=[0, 1], Range=[0, 1, 0, 1, 0, 1, 0, 1])
    assert PDFFunction(fn)([0.8]) == [0.4, 0.2, 0, 0.8]

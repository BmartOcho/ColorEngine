"""Acceptance tests 1-6 and 11 on fixture 1 (Brightspot J218500-02)."""

import hashlib

from conftest import run_job

ORANGE_SET = [(14.0, 68.3, 79.7, 22.6), (9.4, 30.8, 66.1, 7.2), (0.0, 4.3, 12.2, 0.0)]
P10_SET = [(3.6, 75.9, 97.9, 15.2), (14.0, 32.0, 73.0, 0.0), (0.0, 4.0, 10.0, 0.0)]
GREEN_PAGES = [*range(17, 22), *range(48, 65)]


def test_1_ingest(brightspot):
    meta = brightspot.job["ingest"]
    assert meta["page_count"] == 64
    assert meta["creator"] == "Adobe InDesign 21.6 (Windows)"
    assert meta["pdfx_version"] == "PDF/X-1a:2003"
    assert [o["output_condition_identifier"] for o in meta["output_intents"]] == ["CGATS TR 001"]
    bad = [r for r in brightspot.inventory if r["space"] != "DeviceCMYK" or r.get("flags")]
    assert bad == []


def test_2_inventory_p9_p10(brightspot):
    p9, p10 = brightspot.page_cmyk(9), brightspot.page_cmyk(10)
    for c in ORANGE_SET:
        assert c in p9
        assert p9[c]["round"] is False
    for c in P10_SET:
        assert c in p10
    assert p10[(14.0, 32.0, 73.0, 0.0)]["round"] is True
    assert p10[(0.0, 4.0, 10.0, 0.0)]["round"] is True
    assert p10[(3.6, 75.9, 97.9, 15.2)]["round"] is False
    # raw operands kept exactly as written
    assert p10[(14.0, 32.0, 73.0, 0.0)]["raw"] == [0.14, 0.32, 0.73, 0]


def test_3_cross_page_color_drift(brightspot):
    drift = brightspot.rule("cross-page-color-drift")
    assert len(drift) == 1
    f = drift[0]
    assert f["severity"] == "error" and f["pages"] == [10]
    mapping = {tuple(m["from"]): tuple(m["to"]) for m in f["fix"]["map"]}
    # The three spec values, each mapped to what the same objects carry on the other pages.
    assert mapping[(3.6, 75.9, 97.9, 15.2)] == (14.0, 68.3, 79.7, 22.6)
    assert mapping[(14.0, 32.0, 73.0, 0.0)] == (9.4, 30.8, 66.1, 7.2)
    # Logo cream: its counterpart elsewhere is 0/5.3/12.8 (the numerically nearest, 0/3.9/11.8, is text).
    assert mapping[(0.0, 4.0, 10.0, 0.0)] == (0.0, 5.3, 12.8, 0.0)
    assert all(m["match"] == "layout" for m in f["fix"]["map"])
    # p10 also carries 9 pt text at 0/3/9/0 (ΔE00 1.8 from 0/3.9/11.8/0); text always counts.
    assert set(mapping) - {*P10_SET} == {(0.0, 3.0, 9.0, 0.0)}
    for m in f["fix"]["map"]:
        assert 1.0 < m["delta_e"] < 15.0


def test_4_color_versions(brightspot):
    versions = brightspot.report["color_versions"]
    assert len(versions) == 2
    green = [v for v in versions.values() if 17 in v][0]
    assert green == GREEN_PAGES
    for c in [(25.3, 13.2, 37.8, 3.8), (60.2, 41.5, 67.2, 46.2)]:
        assert c in brightspot.page_cmyk(17)
    drift_pages = {p for f in brightspot.rule("cross-page-color-drift") for p in f["pages"]}
    assert not drift_pages & set(GREEN_PAGES)


def test_5_mixed_source_fingerprint(brightspot):
    assert [f["pages"] for f in brightspot.rule("mixed-source-fingerprint")] == [[10]]


def test_6_intent_mismatch(brightspot, brightspot_swop):
    assert len(brightspot.rule("intent-mismatch")) == 1
    assert brightspot_swop.rule("intent-mismatch") == []


def test_mixed_creator_flags_p10(brightspot):
    assert [f["pages"] for f in brightspot.rule("mixed-creator")] == [[10]]


def test_7_rgb_silent_on_brightspot(brightspot):
    assert brightspot.rule("rgb-in-pdfx1a") == []


def test_11_determinism(brightspot_pdf, tmp_path):
    def digests(job):
        return {n: hashlib.sha256((job.path / n).read_bytes()).hexdigest()
                for n in ("inventory.json", "findings.json", "report.json")}

    a = run_job(brightspot_pdf, tmp_path / "a")
    b = run_job(brightspot_pdf, tmp_path / "b")
    assert digests(a) == digests(b)

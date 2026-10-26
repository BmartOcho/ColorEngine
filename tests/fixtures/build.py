"""Synthetic fixtures built with pikepdf from InDesign-style constructs.

fixture 2: PDF/X-1a claim with sRGB (ICCBased N=3) and DeviceRGB fills
fixture 4: 400% rich-black test page
fixture 5: two spot colours with Lab alternates (+ optional process build of the same name)
"""

from __future__ import annotations

from pathlib import Path

import pikepdf

SRGB_ICC = Path(pikepdf.__file__).parent / "pdfa" / "data" / "sRGB.icc"


def _base(pdfx: str | None = "PDF/X-1a:2003", intent: str | None = "CGATS21-2-CRPC6"):
    pdf = pikepdf.new()
    pdf.docinfo["/Creator"] = "Adobe InDesign 21.6 (Windows)"
    pdf.docinfo["/Producer"] = "proofing-engine test fixture"
    if pdfx:
        pdf.docinfo["/GTS_PDFXVersion"] = pdfx
    if intent:
        pdf.Root.OutputIntents = pdf.make_indirect(pikepdf.Array([pikepdf.Dictionary(
            Type=pikepdf.Name.OutputIntent, S=pikepdf.Name.GTS_PDFX,
            OutputConditionIdentifier=intent, RegistryName="http://www.color.org", Info=intent)]))
    return pdf


def _page(pdf, content: bytes, resources: dict | None = None, size=(252, 180)):
    page = pikepdf.Page(pikepdf.Dictionary(
        Type=pikepdf.Name.Page,
        MediaBox=[0, 0, *size], TrimBox=[9, 9, size[0] - 9, size[1] - 9], BleedBox=[0, 0, *size],
        Resources=pikepdf.Dictionary(resources or {}),
        Contents=pdf.make_stream(content)))
    pdf.pages.append(page)
    return page


def _font(pdf):
    return pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))


def fixture2_rgb(path: Path) -> Path:
    pdf = _base()
    icc = pdf.make_stream(SRGB_ICC.read_bytes(), N=3)
    res = {"/ColorSpace": pikepdf.Dictionary(CS0=pikepdf.Array([pikepdf.Name.ICCBased, icc])),
           "/Font": pikepdf.Dictionary(F1=_font(pdf))}
    content = (b"0.1 0.6 0.8 0.1 k 9 9 234 162 re f\n"
               b"/CS0 cs 0.85 0.35 0.1 sc 20 20 100 60 re f\n"
               b"0.2 0.5 0.9 rg 130 20 100 60 re f\n"
               b"BT /F1 12 Tf /CS0 cs 1 1 1 sc 20 120 Td (No Color Conversion) Tj ET\n")
    _page(pdf, content, res)
    pdf.save(path, deterministic_id=True)
    return path


def fixture4_tac(path: Path) -> Path:
    pdf = _base()
    content = (b"0 0 0 0 k 9 9 234 162 re f\n"
               b"1 1 1 1 k 30 30 192 120 re f\n"
               b"0.6 0.4 0.4 1 k 30 10 40 15 re f\n")
    _page(pdf, content)
    pdf.save(path, deterministic_id=True)
    return path


def _spot(pdf, name: str, lab: list[float]):
    fn = pdf.make_indirect(pikepdf.Dictionary(FunctionType=2, Domain=[0, 1], C0=[100, 0, 0], C1=lab, N=1))
    return pikepdf.Array([pikepdf.Name.Separation, pikepdf.Name("/" + name),
                          pikepdf.Array([pikepdf.Name.Lab, pikepdf.Dictionary(WhitePoint=[0.9642, 1, 0.8249],
                                                                               Range=[-128, 127, -128, 127])]),
                          fn])


SPOTS = {"PANTONE 1795 C": [47.6, 69.2, 49.5], "PANTONE 295 C": [21.6, 2.6, -36.2]}


def fixture5_spots(path: Path, with_process_duplicate: bool = False) -> Path:
    pdf = _base()
    names = list(SPOTS)
    res = {"/ColorSpace": pikepdf.Dictionary(CS0=_spot(pdf, names[0], SPOTS[names[0]]),
                                             CS1=_spot(pdf, names[1], SPOTS[names[1]]))}
    content = b"/CS0 cs 1 sc 20 20 100 60 re f\n/CS1 cs 0.5 sc 130 20 100 60 re f\n"
    _page(pdf, content, res)
    if with_process_duplicate:
        _page(pdf, b"0 0.96 0.9 0.02 k 20 20 100 60 re f\n")
        _set_swatches(pdf, [("PANTONE 1795 C", "PROCESS", [0, 96, 90, 2])])
    pdf.save(path, deterministic_id=True)
    return path


def _set_swatches(pdf, swatches):
    items = "".join(
        f'<rdf:li rdf:parseType="Resource"><xmpG:swatchName>{n}</xmpG:swatchName><xmpG:mode>CMYK</xmpG:mode>'
        f"<xmpG:type>{t}</xmpG:type><xmpG:cyan>{c[0]}</xmpG:cyan><xmpG:magenta>{c[1]}</xmpG:magenta>"
        f"<xmpG:yellow>{c[2]}</xmpG:yellow><xmpG:black>{c[3]}</xmpG:black></rdf:li>"
        for n, t, c in swatches)
    xmp = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           '<rdf:Description rdf:about="" xmlns:xmpTPg="http://ns.adobe.com/xap/1.0/t/pg/" '
           'xmlns:xmpG="http://ns.adobe.com/xap/1.0/g/"><xmpTPg:SwatchGroups><rdf:Seq><rdf:li rdf:parseType="Resource">'
           f'<xmpG:Colorants><rdf:Seq>{items}</rdf:Seq></xmpG:Colorants></rdf:li></rdf:Seq></xmpTPg:SwatchGroups>'
           "</rdf:Description></rdf:RDF></x:xmpmeta>")
    pdf.Root.Metadata = pdf.make_stream(xmp.encode(), Type=pikepdf.Name.Metadata, Subtype=pikepdf.Name.XML)


def merge_sources(dir_: Path) -> tuple[Path, Path]:
    """Two InDesign-style sources with different output intents (fixture 3 stand-in)."""
    a = _base(intent="CGATS TR 001")
    _page(a, b"0.14 0.68 0.8 0.23 k 9 9 234 162 re f\n")
    _page(a, b"0.6 0.42 0.67 0.46 k 9 9 234 162 re f\n")
    b = _base(intent="CGATS21-2-CRPC6")
    _page(b, b"0.04 0.76 0.98 0.15 k 9 9 234 162 re f\n")
    pa, pb = dir_ / "badges_a.pdf", dir_ / "badge_b.pdf"
    a.save(pa, deterministic_id=True)
    b.save(pb, deterministic_id=True)
    return pa, pb

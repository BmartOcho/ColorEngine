"""Content-stream walker, colour-space resolver and image sampler.

Walks every page's content stream, recursing into Form XObjects, tiling
patterns, shadings and inline images, and emits one record per
colour-bearing object.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field

import numpy as np
import pikepdf

from .cms import ColorEngine, round1
from .functions import PDFFunction

SHADING_STOPS = 16
CURVE_STEPS = 8

# --- colour spaces ------------------------------------------------------------


@dataclass(eq=False)
class ColorSpace:
    family: str  # DeviceCMYK, DeviceRGB, DeviceGray, ICCBased, Lab, CalRGB, CalGray, Separation, DeviceN, Indexed, Pattern
    n: int
    label: str  # e.g. "ICCBased N=3"
    base: "ColorSpace | None" = None
    icc: bytes | None = None
    icc_desc: str | None = None
    names: list[str] = field(default_factory=list)
    tint: PDFFunction | None = None
    hival: int = 0
    lookup: bytes = b""
    lab_alternate: bool = False
    decode_range: list[float] | None = None
    colorant_lab: dict[str, list[float]] = field(default_factory=dict)

    def initial(self) -> list[float]:
        if self.family == "DeviceCMYK":
            return [0.0, 0.0, 0.0, 1.0]
        if self.family in ("Separation", "DeviceN"):
            return [1.0] * self.n
        if self.family == "Lab":
            return [0.0, 0.0, 0.0]
        return [0.0] * self.n


DEVICE_GRAY = ColorSpace("DeviceGray", 1, "DeviceGray")
DEVICE_RGB = ColorSpace("DeviceRGB", 3, "DeviceRGB")
DEVICE_CMYK = ColorSpace("DeviceCMYK", 4, "DeviceCMYK")
PATTERN = ColorSpace("Pattern", 1, "Pattern")

_DEVICE = {
    "/DeviceGray": DEVICE_GRAY, "/G": DEVICE_GRAY,
    "/DeviceRGB": DEVICE_RGB, "/RGB": DEVICE_RGB,
    "/DeviceCMYK": DEVICE_CMYK, "/CMYK": DEVICE_CMYK,
    "/Pattern": PATTERN,
}


def _icc_description(data: bytes) -> str | None:
    """Read the 'desc' tag text from raw ICC bytes (v2 'desc' or v4 'mluc')."""
    try:
        count = int.from_bytes(data[128:132], "big")
        for i in range(count):
            off = 132 + 12 * i
            sig = data[off:off + 4]
            if sig != b"desc":
                continue
            start = int.from_bytes(data[off + 4:off + 8], "big")
            tag = data[start:]
            if tag[:4] == b"desc":
                n = int.from_bytes(tag[8:12], "big")
                return tag[12:12 + n].rstrip(b"\0").decode("latin1")
            if tag[:4] == b"mluc":
                rec_len = int.from_bytes(tag[20:24], "big")  # first record: lang, country, length, offset
                s_off = int.from_bytes(tag[24:28], "big")
                return tag[s_off:s_off + rec_len].decode("utf-16-be").rstrip("\0")
    except Exception:
        return None
    return None


class ColorSpaceResolver:
    def __init__(self):
        self._cache: dict[tuple, ColorSpace] = {}

    def resolve(self, obj, resources) -> ColorSpace:
        if isinstance(obj, pikepdf.Name):
            key = str(obj)
            if key in _DEVICE:
                # Default colour spaces in resources override device spaces.
                default = _lookup(resources, "/ColorSpace", "/Default" + key[len("/Device"):]) if key.startswith("/Device") else None
                if default is not None:
                    return self.resolve(default, resources)
                return _DEVICE[key]
            named = _lookup(resources, "/ColorSpace", key)
            if named is None:
                raise KeyError(f"colour space {key} not in resources")
            return self.resolve(named, resources)
        cache_key = obj.objgen if obj.is_indirect else None
        if cache_key and cache_key in self._cache:
            return self._cache[cache_key]
        cs = self._build(obj, resources)
        if cache_key:
            self._cache[cache_key] = cs
        return cs

    def _build(self, obj, resources) -> ColorSpace:
        if isinstance(obj, pikepdf.Name):
            return self.resolve(obj, resources)
        fam = str(obj[0])
        if fam == "/ICCBased":
            stream = obj[1]
            n = int(stream.N)
            data = stream.read_bytes()
            return ColorSpace("ICCBased", n, f"ICCBased N={n}", icc=data, icc_desc=_icc_description(data))
        if fam == "/Lab":
            rng = [float(x) for x in obj[1].get("/Range", [-100, 100, -100, 100])]
            return ColorSpace("Lab", 3, "Lab", decode_range=[0, 100, *rng])
        if fam == "/CalRGB":
            return ColorSpace("CalRGB", 3, "CalRGB")
        if fam == "/CalGray":
            return ColorSpace("CalGray", 1, "CalGray")
        if fam in ("/Separation", "/DeviceN"):
            names = [str(obj[1])[1:]] if fam == "/Separation" else [str(x)[1:] for x in obj[1]]
            alt = self.resolve(obj[2], resources)
            cs = ColorSpace(fam[1:], len(names), fam[1:], base=alt, names=names, tint=PDFFunction(obj[3]))
            cs.lab_alternate = alt.family == "Lab"
            if fam == "/DeviceN" and len(obj) > 4:
                colorants = obj[4].get("/Colorants")
                if colorants is not None:
                    for name, sep in colorants.items():
                        sub = self.resolve(sep, resources)
                        if sub.base is not None and sub.base.family == "Lab":
                            cs.colorant_lab[name[1:]] = sub.tint([1.0])
            return cs
        if fam in ("/Indexed", "/I"):
            base = self.resolve(obj[1], resources)
            lookup = obj[3]
            data = lookup.read_bytes() if isinstance(lookup, pikepdf.Stream) else bytes(lookup)
            return ColorSpace("Indexed", 1, "Indexed", base=base, hival=int(obj[2]), lookup=data)
        if fam == "/Pattern":
            base = self.resolve(obj[1], resources) if len(obj) > 1 else None
            return ColorSpace("Pattern", 1, "Pattern", base=base)
        if fam in _DEVICE:
            return _DEVICE[fam]
        raise ValueError(f"unsupported colour space {fam}")


def _lookup(resources, category, name):
    if resources is None:
        return None
    cat = resources.get(category)
    if cat is None:
        return None
    return cat.get(name)


# --- colour resolution --------------------------------------------------------


@dataclass
class Resolved:
    cmyk: list[float]  # percent, full precision
    lab: list[float]
    space: str  # report label
    flags: list[str]
    colorants: list[str] = field(default_factory=list)
    spot_source: str | None = None


class Resolver:
    def __init__(self, engine: ColorEngine):
        self.engine = engine

    def resolve(self, cs: ColorSpace, comps: list[float]) -> Resolved:
        e = self.engine
        fam = cs.family
        if fam == "DeviceCMYK":
            cmyk = [c * 100.0 for c in comps]
            return Resolved(cmyk, e.cmyk_to_lab(cmyk), "DeviceCMYK", [])
        if fam == "DeviceGray":
            cmyk = e.gray_to_cmyk(comps[0])
            return Resolved(cmyk, e.cmyk_to_lab(cmyk), "DeviceGray", [])
        if fam == "DeviceRGB":
            cmyk = e.rgb_to_cmyk(comps)
            return Resolved(cmyk, e.cmyk_to_lab(cmyk), "DeviceRGB", ["rgb-object"])
        if fam == "ICCBased":
            if cs.n == 4 and self._is_press(cs):
                cmyk = [c * 100.0 for c in comps]
                return Resolved(cmyk, e.cmyk_to_lab(cmyk), cs.label, [])
            cmyk = e.icc_to_cmyk(cs.icc, cs.n, comps)
            flag = {3: "rgb-object", 4: "cmyk-retagged", 1: "gray-icc"}[cs.n]
            return Resolved(cmyk, e.cmyk_to_lab(cmyk), cs.label, [flag])
        if fam == "Lab":
            cmyk = e.lab_to_cmyk(comps)
            return Resolved(cmyk, list(comps), "Lab", ["lab-object"])
        if fam == "CalRGB":
            cmyk = e.rgb_to_cmyk(comps)
            return Resolved(cmyk, e.cmyk_to_lab(cmyk), "CalRGB", ["lab-object"])
        if fam == "CalGray":
            cmyk = e.gray_to_cmyk(comps[0])
            return Resolved(cmyk, e.cmyk_to_lab(cmyk), "CalGray", ["lab-object"])
        if fam in ("Separation", "DeviceN"):
            alt_comps = cs.tint(comps)
            base = self.resolve(cs.base, alt_comps)
            flags = [f"spot-{n}" for n in cs.names if n not in ("Cyan", "Magenta", "Yellow", "Black", "None")]
            if "All" in cs.names:
                flags.append("registration")
            source = "lab-alternate" if cs.lab_alternate else "simulated-from-alternate"
            return Resolved(base.cmyk, base.lab, f"{fam}({','.join(cs.names)})", flags + base.flags,
                            colorants=list(cs.names), spot_source=source)
        if fam == "Indexed":
            idx = int(round(comps[0]))
            idx = max(0, min(cs.hival, idx))
            n = cs.base.n
            raw = cs.lookup[idx * n:(idx + 1) * n]
            base_comps = [b / 255.0 for b in raw]
            if cs.base.family == "Lab":
                r = cs.base.decode_range
                base_comps = [r[2 * i] + v * (r[2 * i + 1] - r[2 * i]) for i, v in enumerate(base_comps)]
            base = self.resolve(cs.base, base_comps)
            return Resolved(base.cmyk, base.lab, f"Indexed({base.space})", base.flags, base.colorants, base.spot_source)
        raise ValueError(f"cannot resolve {fam}")

    def _is_press(self, cs: ColorSpace) -> bool:
        desc = (cs.icc_desc or "").lower()
        press = self.engine.press
        names = [press.file.rsplit(".", 1)[0], press.description, press.name, *press.aliases]
        return any(n.lower() == desc or (n.lower() in desc and len(n) > 4) for n in names)


# --- geometry -----------------------------------------------------------------


def _mul(a, b):
    """Matrix product a x b for PDF [a b c d e f] matrices."""
    return [
        a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5],
    ]


def _apply(m, x, y):
    return (m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5])


def _scale(m) -> float:
    return math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))


def _poly_area(pts) -> float:
    if len(pts) < 3:
        return 0.0
    s = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def _poly_len(pts, closed) -> float:
    seq = pts + pts[:1] if closed else pts
    return sum(math.dist(a, b) for a, b in zip(seq, seq[1:]))


def _bbox(points):
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return [min(xs), min(ys), max(xs), max(ys)]


class Path:
    def __init__(self):
        self.subpaths: list[tuple[list, bool]] = []
        self.current: list = []
        self.closed = False

    def move(self, p):
        self._flush()
        self.current = [p]

    def line(self, p):
        self.current.append(p)

    def curve(self, p0, p1, p2, p3):
        for i in range(1, CURVE_STEPS + 1):
            t = i / CURVE_STEPS
            u = 1 - t
            self.current.append((
                u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
            ))

    def close(self):
        self.closed = True
        self._flush()

    def _flush(self):
        if len(self.current) > 1 or (self.current and self.subpaths == []):
            self.subpaths.append((self.current, self.closed))
        self.current, self.closed = [], False

    def finish(self):
        self._flush()
        return self.subpaths

    @property
    def last(self):
        if self.current:
            return self.current[-1]
        if self.subpaths:
            return self.subpaths[-1][0][0]
        return (0.0, 0.0)


# --- graphics state -----------------------------------------------------------


@dataclass
class GState:
    ctm: list[float]
    fill_cs: ColorSpace = DEVICE_GRAY
    stroke_cs: ColorSpace = DEVICE_GRAY
    fill: list = field(default_factory=lambda: [0.0])
    stroke: list = field(default_factory=lambda: [0.0])
    fill_pattern: str | None = None
    stroke_pattern: str | None = None
    op_fill: bool = False
    op_stroke: bool = False
    opm: int = 0
    line_width: float = 1.0
    font_size: float = 0.0
    font: str | None = None
    render_mode: int = 0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    hscale: float = 1.0
    leading: float = 0.0
    rise: float = 0.0

    def copy(self) -> "GState":
        g = GState(**self.__dict__)
        g.ctm = list(self.ctm)
        g.fill = list(self.fill)
        g.stroke = list(self.stroke)
        return g


# --- walker -------------------------------------------------------------------


def _num(x) -> float:
    return float(x)


def _raw(x):
    """Keep raw operands exactly as written in the PDF."""
    if isinstance(x, (int,)):
        return x
    s = str(x)
    try:
        return int(s) if s.lstrip("-").isdigit() else float(s)
    except ValueError:
        return s


class PageWalker:
    def __init__(self, pdf: pikepdf.Pdf, page_no: int, page: pikepdf.Page, resolver: Resolver,
                 cs_resolver: ColorSpaceResolver):
        self.pdf = pdf
        self.page_no = page_no
        self.page = page
        self.resolver = resolver
        self.cs_resolver = cs_resolver
        self.records: list[dict] = []
        self.counters: dict[str, int] = {}
        self.page_box = [float(x) for x in (page.obj.get("/TrimBox") or page.obj.get("/CropBox") or page.MediaBox)]
        self._resolved_cache: dict[tuple, Resolved] = {}
        self._form_depth = 0

    # records ------------------------------------------------------------
    def _id(self, kind: str) -> str:
        n = self.counters.get(kind, 0) + 1
        self.counters[kind] = n
        return f"p{self.page_no}.{kind}.{n}"

    def _resolve(self, cs: ColorSpace, comps) -> Resolved:
        key = (id(cs), tuple(comps))
        if key not in self._resolved_cache:
            self._resolved_cache[key] = self.resolver.resolve(cs, [float(c) for c in comps])
        return self._resolved_cache[key]

    def _emit(self, kind, cs, comps, gs, area, bbox, overprint, extra=None, resolved=None):
        res = resolved or self._resolve(cs, comps)
        raw = [_raw(c) for c in comps]
        rec = {
            "page": self.page_no,
            "obj": self._id(kind),
            "kind": kind,
            "space": res.space,
            "raw": raw,
            "cmyk": [round1(v) for v in res.cmyk],
            "lab": [round(v, 2) + 0.0 for v in res.lab],
            "round": _is_round(cs, comps),
            "overprint": overprint,
            "area_pt2": round(area, 1),
            "bbox": [round(v, 2) for v in bbox] if bbox else None,
        }
        if gs is not None and overprint:
            rec["opm"] = gs.opm
        if res.flags:
            rec["flags"] = sorted(set(res.flags))
        if res.colorants:
            rec["colorants"] = res.colorants
            rec["spot_source"] = res.spot_source
        if extra:
            rec.update(extra)
        self.records.append(rec)
        return rec

    # walking ------------------------------------------------------------
    def walk(self):
        gs = GState(ctm=[1, 0, 0, 1, 0, 0])
        self._walk_stream(self.page.obj, self.page.Resources if "/Resources" in self.page.obj else None, gs)
        return self.records

    def _walk_stream(self, stream_owner, resources, gs: GState, pattern_paint=None):
        try:
            instructions = pikepdf.parse_content_stream(stream_owner)
        except pikepdf.PdfError:
            return
        stack: list[GState] = []
        path = Path()
        clip_pending = False
        tm = tlm = [1, 0, 0, 1, 0, 0]
        for inst in instructions:
            op = str(inst.operator)
            ops = inst.operands
            if op == "q":
                stack.append(gs.copy())
            elif op == "Q":
                if stack:
                    gs = stack.pop()
            elif op == "cm":
                gs.ctm = _mul([_num(x) for x in ops], gs.ctm)
            elif op == "w":
                gs.line_width = _num(ops[0])
            elif op == "gs":
                self._ext_gstate(resources, ops[0], gs)
            # colour --------------------------------------------------
            elif op in ("cs", "CS"):
                cs = self.cs_resolver.resolve(ops[0], resources)
                if op == "cs":
                    gs.fill_cs, gs.fill, gs.fill_pattern = cs, cs.initial(), None
                else:
                    gs.stroke_cs, gs.stroke, gs.stroke_pattern = cs, cs.initial(), None
            elif op in ("sc", "scn"):
                self._set_color(gs, ops, fill=True)
            elif op in ("SC", "SCN"):
                self._set_color(gs, ops, fill=False)
            elif op in ("g", "rg", "k"):
                gs.fill_cs = {"g": DEVICE_GRAY, "rg": DEVICE_RGB, "k": DEVICE_CMYK}[op]
                gs.fill, gs.fill_pattern = list(ops), None
            elif op in ("G", "RG", "K"):
                gs.stroke_cs = {"G": DEVICE_GRAY, "RG": DEVICE_RGB, "K": DEVICE_CMYK}[op]
                gs.stroke, gs.stroke_pattern = list(ops), None
            # path ----------------------------------------------------
            elif op == "m":
                path.move(_apply(gs.ctm, _num(ops[0]), _num(ops[1])))
            elif op == "l":
                path.line(_apply(gs.ctm, _num(ops[0]), _num(ops[1])))
            elif op in ("c", "v", "y"):
                v = [_num(x) for x in ops]
                p0 = path.last
                if op == "c":
                    p1, p2, p3 = _apply(gs.ctm, v[0], v[1]), _apply(gs.ctm, v[2], v[3]), _apply(gs.ctm, v[4], v[5])
                elif op == "v":
                    p1, p2, p3 = p0, _apply(gs.ctm, v[0], v[1]), _apply(gs.ctm, v[2], v[3])
                else:
                    p1 = _apply(gs.ctm, v[0], v[1])
                    p2 = p3 = _apply(gs.ctm, v[2], v[3])
                path.curve(p0, p1, p2, p3)
            elif op == "re":
                x, y, w, h = (_num(o) for o in ops)
                path.move(_apply(gs.ctm, x, y))
                path.line(_apply(gs.ctm, x + w, y))
                path.line(_apply(gs.ctm, x + w, y + h))
                path.line(_apply(gs.ctm, x, y + h))
                path.close()
            elif op == "h":
                path.close()
            elif op in ("W", "W*"):
                clip_pending = True
            elif op in ("f", "F", "f*", "B", "B*", "b", "b*", "S", "s", "n"):
                if op in ("b", "b*", "s"):
                    path.close()
                subpaths = path.finish()
                path = Path()
                clip_pending = False
                if op == "n" or not subpaths:
                    continue
                pts = [p for sp, _ in subpaths for p in sp]
                bbox = _bbox(pts)
                if op in ("f", "F", "f*", "B", "B*", "b", "b*"):
                    area = sum(_poly_area(sp) for sp, _ in subpaths)
                    self._paint(gs, True, "fill", area, bbox, resources)
                if op in ("B", "B*", "b", "b*", "S", "s"):
                    length = sum(_poly_len(sp, closed) for sp, closed in subpaths)
                    area = length * max(gs.line_width, 0.0) * _scale(gs.ctm)
                    self._paint(gs, False, "stroke", area, bbox, resources)
            # text ----------------------------------------------------
            elif op == "BT":
                tm = tlm = [1, 0, 0, 1, 0, 0]
            elif op == "Tf":
                gs.font, gs.font_size = str(ops[0]), _num(ops[1])
            elif op == "Tr":
                gs.render_mode = int(ops[0])
            elif op == "Tc":
                gs.char_spacing = _num(ops[0])
            elif op == "Tw":
                gs.word_spacing = _num(ops[0])
            elif op == "Tz":
                gs.hscale = _num(ops[0]) / 100.0
            elif op == "TL":
                gs.leading = _num(ops[0])
            elif op == "Ts":
                gs.rise = _num(ops[0])
            elif op == "Tm":
                tm = tlm = [_num(x) for x in ops]
            elif op in ("Td", "TD"):
                tx, ty = _num(ops[0]), _num(ops[1])
                if op == "TD":
                    gs.leading = -ty
                tm = tlm = _mul([1, 0, 0, 1, tx, ty], tlm)
            elif op == "T*":
                tm = tlm = _mul([1, 0, 0, 1, 0, -gs.leading], tlm)
            elif op in ("Tj", "TJ", "'", '"'):
                if op in ("'", '"'):
                    tm = tlm = _mul([1, 0, 0, 1, 0, -gs.leading], tlm)
                if op == '"':
                    gs.word_spacing, gs.char_spacing = _num(ops[0]), _num(ops[1])
                tm = self._text(gs, tm, ops[-1], resources)
            # XObjects / shadings / images ----------------------------
            elif op == "Do":
                self._do(gs, ops[0], resources)
            elif op == "sh":
                sh = _lookup(resources, "/Shading", str(ops[0]))
                if sh is not None:
                    self._shading(sh, gs, resources, self._clip_bbox_area())
            elif op == "INLINE IMAGE":
                self._inline_image(gs, ops[0], resources)

    def _clip_bbox_area(self):
        b = self.page_box
        return (b[2] - b[0]) * (b[3] - b[1]), b

    def _ext_gstate(self, resources, name, gs):
        egs = _lookup(resources, "/ExtGState", str(name))
        if egs is None:
            return
        if "/OP" in egs:
            gs.op_stroke = bool(egs.OP)
            gs.op_fill = bool(egs.get("/op", egs.OP))
        if "/op" in egs:
            gs.op_fill = bool(egs.op)
        if "/OPM" in egs:
            gs.opm = int(egs.OPM)
        if "/LW" in egs:
            gs.line_width = float(egs.LW)
        if "/Font" in egs:
            gs.font_size = float(egs.Font[1])

    def _set_color(self, gs, ops, fill):
        cs = gs.fill_cs if fill else gs.stroke_cs
        pattern = None
        comps = list(ops)
        if comps and isinstance(comps[-1], pikepdf.Name):
            pattern = str(comps.pop())
        if fill:
            gs.fill, gs.fill_pattern = (comps or gs.fill), pattern
        else:
            gs.stroke, gs.stroke_pattern = (comps or gs.stroke), pattern
        _ = cs

    def _paint(self, gs, fill, kind, area, bbox, resources, extra=None):
        cs = gs.fill_cs if fill else gs.stroke_cs
        comps = gs.fill if fill else gs.stroke
        pattern = gs.fill_pattern if fill else gs.stroke_pattern
        overprint = gs.op_fill if fill else gs.op_stroke
        if cs.family == "Pattern":
            if pattern:
                self._pattern(pattern, gs, resources, area, bbox, kind, comps)
            return
        self._emit(kind, cs, comps, gs, area, bbox, overprint, extra)

    def _pattern(self, name, gs, resources, area, bbox, kind, comps):
        pat = _lookup(resources, "/Pattern", name)
        if pat is None:
            return
        ptype = int(pat.get("/PatternType", 1))
        if ptype == 2:
            self._shading(pat.Shading, gs, resources, (area, bbox), via=f"pattern {name}")
        else:
            m = [float(x) for x in pat.get("/Matrix", [1, 0, 0, 1, 0, 0])]
            sub = GState(ctm=_mul(m, gs.ctm))
            if int(pat.get("/PaintType", 1)) == 2:
                # Uncoloured tiling pattern: colour comes from the underlying space.
                base = gs.fill_cs.base if kind != "stroke" else gs.stroke_cs.base
                if base is not None and comps:
                    self._emit(kind, base, comps, gs, area, bbox, gs.op_fill, {"pattern": name})
                return
            self._recurse(pat, pat.get("/Resources"), sub)

    def _recurse(self, stream, resources, gs):
        if self._form_depth > 12:
            return
        self._form_depth += 1
        try:
            self._walk_stream(stream, resources, gs)
        finally:
            self._form_depth -= 1

    def _do(self, gs, name, resources):
        xobj = _lookup(resources, "/XObject", str(name))
        if xobj is None:
            return
        sub = xobj.get("/Subtype")
        if sub == "/Form":
            m = [float(x) for x in xobj.get("/Matrix", [1, 0, 0, 1, 0, 0])]
            g = gs.copy()
            g.ctm = _mul(m, gs.ctm)
            res = xobj.get("/Resources", resources)
            self._recurse(xobj, res, g)
        elif sub == "/Image":
            self._image(gs, xobj, resources, str(name))

    # text -----------------------------------------------------------------
    def _text(self, gs, tm, arg, resources):
        if isinstance(arg, pikepdf.Array):
            chars = sum(len(bytes(x)) for x in arg if isinstance(x, pikepdf.String))
            adjust = sum(float(x) for x in arg if not isinstance(x, pikepdf.String))
        else:
            chars, adjust = len(bytes(arg)), 0.0
        font = _lookup(resources, "/Font", gs.font) if gs.font else None
        if font is not None and font.get("/Subtype") == "/Type0":
            chars //= 2
        trm = _mul(tm, gs.ctm)
        size_pt = abs(gs.font_size) * _scale(trm)
        # Width estimate: half an em per glyph plus spacing; height one em.
        width_text = chars * (0.5 * gs.font_size + gs.char_spacing) * gs.hscale - adjust / 1000.0 * gs.font_size
        x0, y0 = _apply(trm, 0, gs.rise - 0.2 * gs.font_size)
        x1, y1 = _apply(trm, width_text, gs.rise + 0.8 * gs.font_size)
        bbox = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]
        area = abs(width_text * gs.font_size) * _scale(trm) ** 2
        extra = {"font_size_pt": round(size_pt, 2), "chars": chars}
        if font is not None:
            extra["font"] = str(font.get("/BaseFont", gs.font))[1:]
        mode = gs.render_mode
        if mode in (0, 2, 4, 6):
            self._paint(gs, True, "text", area, bbox, resources, extra)
        if mode in (1, 2, 5, 6):
            self._paint(gs, False, "text-stroke", area, bbox, resources, extra)
        return _mul([1, 0, 0, 1, width_text, 0], tm)

    # shadings ---------------------------------------------------------------
    def _shading(self, sh, gs, resources, area_bbox, via=None):
        try:
            cs = self.cs_resolver.resolve(sh.ColorSpace, resources)
        except Exception:
            return
        area, bbox = area_bbox
        stype = int(sh.get("/ShadingType", 2))
        fn = sh.get("/Function")
        stops = []
        if fn is not None:
            f = PDFFunction(fn)
            if stype in (2, 3):
                d = [float(x) for x in sh.get("/Domain", [0, 1])]
                stops = [f([d[0] + (d[1] - d[0]) * i / (SHADING_STOPS - 1)]) for i in range(SHADING_STOPS)]
            elif stype == 1:
                d = [float(x) for x in sh.get("/Domain", [0, 1, 0, 1])]
                stops = [f([d[0] + (d[1] - d[0]) * i / (SHADING_STOPS - 1), d[2] + (d[3] - d[2]) * i / (SHADING_STOPS - 1)])
                         for i in range(SHADING_STOPS)]
            else:
                stops = [f([i / (SHADING_STOPS - 1)]) for i in range(SHADING_STOPS)]
        if not stops:
            stops = [cs.initial()]
        shading_id = f"p{self.page_no}.shading.{self.counters.get('shading', 0) + 1}"
        self.counters["shading"] = self.counters.get("shading", 0) + 1
        for i, comps in enumerate(stops):
            extra = {"shading": shading_id, "stop": i}
            if via:
                extra["pattern"] = via
            self._emit("shading-stop", cs, [round(c, 6) for c in comps], gs, area / len(stops), bbox, gs.op_fill, extra)

    # images ---------------------------------------------------------------
    def _image(self, gs, xobj, resources, name):
        if xobj.get("/ImageMask", False):
            corners = [_apply(gs.ctm, x, y) for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]
            self._paint(gs, True, "fill", _poly_area(corners), _bbox(corners), resources, {"image_mask": name})
            return
        try:
            pim = pikepdf.PdfImage(xobj)
        except Exception:
            return
        self._image_common(gs, pim, xobj.get("/ColorSpace"), resources, name)

    def _inline_image(self, gs, inline, resources):
        try:
            pim = inline if isinstance(inline, pikepdf.PdfInlineImage) else pikepdf.PdfInlineImage(inline)
        except Exception:
            return
        cs_obj = None
        try:
            cs_obj = pim.obj.get("/ColorSpace") or pim.obj.get("/CS")
        except Exception:
            pass
        self._image_common(gs, pim, cs_obj, resources, "inline")

    def _image_common(self, gs, pim, cs_obj, resources, name):
        corners = [_apply(gs.ctm, x, y) for x, y in ((0, 0), (1, 0), (1, 1), (0, 1))]
        area = _poly_area(corners)
        bbox = _bbox(corners)
        try:
            cs = self.cs_resolver.resolve(cs_obj, resources) if cs_obj is not None else DEVICE_GRAY
        except Exception:
            cs = DEVICE_GRAY
        w, h = int(pim.width), int(pim.height)
        wpt = math.dist(corners[0], corners[1]) or 1.0
        hpt = math.dist(corners[0], corners[3]) or 1.0
        ppi = round(min(w / wpt, h / hpt) * 72.0, 1)
        comps_list, hist = _sample_image(pim, cs)
        base_extra = {"image": name, "pixels": [w, h], "effective_ppi": ppi, "bpc": int(pim.bits_per_component)}
        if not comps_list:
            res = Resolved([0, 0, 0, 0], [100, 0, 0], cs.label, [])
            self._emit("image", cs, [], gs, area, bbox, gs.op_fill, base_extra, resolved=res)
            return
        for (comps, share) in zip(comps_list, hist):
            extra = dict(base_extra, share=round(share, 4))
            self._emit("image", cs, comps, gs, area * share, bbox, gs.op_fill, extra)


def _sample_image(pim, cs: ColorSpace, max_colors: int = 8):
    """Dominant colours of an image as component tuples in the image's space."""
    try:
        img = pim.as_pil_image()
    except Exception:
        return [], []
    img.thumbnail((64, 64))
    arr = np.asarray(img)
    if arr.ndim == 2:
        arr = arr[..., None]
    flat = arr.reshape(-1, arr.shape[-1]).astype(np.int64)
    if flat.size == 0:
        return [], []
    # Quantise to 16 levels per channel, count, take the largest buckets.
    q = (flat // 16) * 16 + 8
    keys, counts = np.unique(q, axis=0, return_counts=True)
    order = np.lexsort((*keys.T[::-1], -counts))[:max_colors]
    total = counts.sum()
    comps_list, shares = [], []
    for i in order:
        k = keys[i]
        if cs.family == "Indexed":
            comps = [int(k[0] // 16 * 16)]
        else:
            vals = [v / 255.0 for v in k]
            if cs.family == "DeviceCMYK" and img.mode == "CMYK" and pim.obj.get("/Decode") is not None:
                vals = [1 - v for v in vals]
            if cs.family == "Lab" and cs.decode_range:
                r = cs.decode_range
                vals = [r[2 * j] + v * (r[2 * j + 1] - r[2 * j]) for j, v in enumerate(vals)]
            comps = [round(v, 4) for v in vals[: max(cs.n, 1)]]
        comps_list.append(comps)
        shares.append(float(counts[i] / total))
    return comps_list, shares


def _is_round(cs: ColorSpace, comps) -> bool:
    """True when every component is an integer percent (a typed-in swatch)."""
    if cs.family not in ("DeviceCMYK", "DeviceGray", "Separation", "DeviceN", "ICCBased"):
        return False
    for c in comps:
        v = float(c) * 100.0
        if abs(v - round(v)) > 1e-6:
            return False
    return True


def is_trivial(cmyk) -> bool:
    """Paper white or solid single-ink values carry no export fingerprint."""
    return all(v in (0.0, 100.0) for v in cmyk)


# --- document-level entry point -------------------------------------------------


def build_inventory(pdf: pikepdf.Pdf, engine: ColorEngine, pages: list[int] | None = None) -> list[dict]:
    resolver = Resolver(engine)
    cs_resolver = ColorSpaceResolver()
    records: list[dict] = []
    for i, page in enumerate(pdf.pages, start=1):
        if pages and i not in pages:
            continue
        records.extend(PageWalker(pdf, i, page, resolver, cs_resolver).walk())
    return records


def page_area(page: pikepdf.Page) -> float:
    b = [float(x) for x in (page.obj.get("/TrimBox") or page.obj.get("/CropBox") or page.MediaBox)]
    return abs(b[2] - b[0]) * abs(b[3] - b[1])


def content_digest(page: pikepdf.Page) -> str:
    contents = page.obj.get("/Contents")
    if contents is None:
        return hashlib.sha256(b"").hexdigest()
    if isinstance(contents, pikepdf.Array):
        data = b"".join(c.read_raw_bytes() for c in contents)
    else:
        data = contents.read_raw_bytes()
    return hashlib.sha256(data).hexdigest()

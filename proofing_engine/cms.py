"""Profile store, colour transforms, ΔE and TAC.

This is the only module that touches LittleCMS. It binds liblcms2 directly
through ctypes so every conversion runs in double precision (Pillow's
ImageCms is 8-bit only, too coarse for a ΔE00 1.0 threshold).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import glob
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

# --- LittleCMS binding --------------------------------------------------------

INTENT_PERCEPTUAL = 0
INTENT_RELATIVE_COLORIMETRIC = 1
INTENT_ABSOLUTE_COLORIMETRIC = 3

cmsFLAGS_NOOPTIMIZE = 0x0100
cmsFLAGS_BLACKPOINTCOMPENSATION = 0x2000


def _fmt(colorspace: int, channels: int) -> int:
    # FLOAT_SH(1) | COLORSPACE_SH(cs) | CHANNELS_SH(n) | BYTES_SH(0) -> double
    return (1 << 22) | (colorspace << 16) | (channels << 3)


TYPE_GRAY_DBL = _fmt(3, 1)
TYPE_RGB_DBL = _fmt(4, 3)
TYPE_CMYK_DBL = _fmt(6, 4)
TYPE_Lab_DBL = _fmt(10, 3)

_CHANNELS = {TYPE_GRAY_DBL: 1, TYPE_RGB_DBL: 3, TYPE_CMYK_DBL: 4, TYPE_Lab_DBL: 3}


def _find_lcms() -> str:
    env = os.environ.get("PROOF_LCMS2")
    if env:
        return env
    found = ctypes.util.find_library("lcms2") or ctypes.util.find_library("lcms2-2")
    if found:
        return found
    # Linux/macOS Pillow wheels ship liblcms2 as a separate shared object.
    import PIL

    base = Path(PIL.__file__).resolve().parent.parent
    for pattern in ("pillow.libs/liblcms2*", "PIL/.dylibs/liblcms2*", "Pillow.libs/liblcms2*"):
        hits = sorted(glob.glob(str(base / pattern)))
        if hits:
            return hits[0]
    raise OSError(
        "liblcms2 not found. Install LittleCMS 2 (apt install liblcms2-2 / "
        "lcms2.dll on Windows) or set PROOF_LCMS2 to the library path."
    )


@lru_cache(maxsize=1)
def _lib() -> ctypes.CDLL:
    lib = ctypes.CDLL(_find_lcms())
    vp, u32 = ctypes.c_void_p, ctypes.c_uint32
    lib.cmsOpenProfileFromMem.restype = vp
    lib.cmsOpenProfileFromMem.argtypes = [ctypes.c_char_p, u32]
    lib.cmsCreateLab4Profile.restype = vp
    lib.cmsCreateLab4Profile.argtypes = [vp]
    lib.cmsCreate_sRGBProfile.restype = vp
    lib.cmsCreateTransform.restype = vp
    lib.cmsCreateTransform.argtypes = [vp, u32, vp, u32, u32, u32]
    lib.cmsDoTransform.argtypes = [vp, vp, vp, u32]
    lib.cmsDeleteTransform.argtypes = [vp]
    lib.cmsCloseProfile.argtypes = [vp]
    lib.cmsGetEncodedCMMversion.restype = ctypes.c_int
    return lib


def lcms_version() -> str:
    v = _lib().cmsGetEncodedCMMversion()
    return f"{v // 1000}.{(v % 1000) // 10}"


class _Profile:
    def __init__(self, handle: int):
        self.handle = handle

    def __del__(self):
        if getattr(self, "handle", None):
            _lib().cmsCloseProfile(self.handle)


def _open_bytes(data: bytes) -> _Profile:
    h = _lib().cmsOpenProfileFromMem(data, len(data))
    if not h:
        raise ValueError("LittleCMS could not open profile")
    return _Profile(h)


class Transform:
    def __init__(self, src: _Profile, src_fmt: int, dst: _Profile, dst_fmt: int,
                 intent: int = INTENT_RELATIVE_COLORIMETRIC, bpc: bool = True):
        flags = cmsFLAGS_NOOPTIMIZE | (cmsFLAGS_BLACKPOINTCOMPENSATION if bpc else 0)
        self._keep = (src, dst)
        self.handle = _lib().cmsCreateTransform(src.handle, src_fmt, dst.handle, dst_fmt, intent, flags)
        if not self.handle:
            raise ValueError("LittleCMS could not build transform")
        self.n_in, self.n_out = _CHANNELS[src_fmt], _CHANNELS[dst_fmt]

    def __call__(self, values) -> np.ndarray:
        arr = np.ascontiguousarray(np.asarray(values, dtype=np.float64).reshape(-1, self.n_in))
        out = np.zeros((arr.shape[0], self.n_out), dtype=np.float64)
        _lib().cmsDoTransform(self.handle, arr.ctypes.data, out.ctypes.data, arr.shape[0])
        return out

    def __del__(self):
        if getattr(self, "handle", None):
            _lib().cmsDeleteTransform(self.handle)


# --- Profile store ------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = Path(os.environ.get("PROOF_PROFILE_DIR", REPO_ROOT / "profiles"))
WINDOWS_COLOR_DIR = Path(r"C:\Windows\System32\spool\drivers\color")


@dataclass
class PressProfile:
    name: str
    file: str
    description: str
    output_condition: str
    tac: float
    sha256: str | None = None
    paper_lab: list[float] | None = None
    url: str | None = None
    aliases: list[str] = field(default_factory=list)

    def path(self) -> Path:
        for d in (PROFILE_DIR / "icc", WINDOWS_COLOR_DIR):
            p = d / self.file
            if p.exists():
                return p
        raise FileNotFoundError(
            f"ICC profile {self.file} for '{self.name}' not found in {PROFILE_DIR / 'icc'} "
            f"or {WINDOWS_COLOR_DIR}. Run: python scripts/fetch_profiles.py"
        )

    def matches_condition(self, identifier: str | None) -> bool:
        if not identifier:
            return False
        ident = identifier.strip().lower()
        return ident in {a.lower() for a in [self.output_condition, self.name, *self.aliases]}


def load_manifest() -> dict:
    return json.loads((PROFILE_DIR / "profiles.json").read_text())


def get_press_profile(name: str | None = None) -> PressProfile:
    manifest = load_manifest()
    name = name or manifest["default"]
    for entry in manifest["press"]:
        if entry["name"] == name or name in entry.get("aliases", []):
            return PressProfile(**entry)
    raise KeyError(f"unknown press profile '{name}'; known: {[e['name'] for e in manifest['press']]}")


def list_press_profiles() -> list[PressProfile]:
    return [PressProfile(**e) for e in load_manifest()["press"]]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- Colour engine ------------------------------------------------------------


class ColorEngine:
    """Resolves source colours to press CMYK and Lab under one press profile."""

    def __init__(self, press: PressProfile):
        self.press = press
        self.press_path = press.path()
        self._press = _open_bytes(self.press_path.read_bytes())
        self._lab = _Profile(_lib().cmsCreateLab4Profile(None))
        self._srgb = _Profile(_lib().cmsCreate_sRGBProfile())
        self._cmyk_to_lab = Transform(self._press, TYPE_CMYK_DBL, self._lab, TYPE_Lab_DBL)
        self._rgb_to_cmyk = Transform(self._srgb, TYPE_RGB_DBL, self._press, TYPE_CMYK_DBL)
        self._lab_to_cmyk = Transform(self._lab, TYPE_Lab_DBL, self._press, TYPE_CMYK_DBL)
        self._icc_cache: dict[str, tuple[_Profile, int]] = {}
        self._lab_cache: dict[tuple, tuple] = {}

    @property
    def profile_sha256(self) -> str:
        return file_sha256(self.press_path)

    def cmyk_to_lab(self, cmyk_pct) -> list[float]:
        key = tuple(cmyk_pct)
        if key not in self._lab_cache:
            self._lab_cache[key] = tuple(float(x) for x in self._cmyk_to_lab(key)[0])
        return list(self._lab_cache[key])

    def rgb_to_cmyk(self, rgb01) -> list[float]:
        return [float(x) for x in self._rgb_to_cmyk(rgb01)[0]]

    def gray_to_cmyk(self, g01: float) -> list[float]:
        # Untagged gray maps to K only (Dot Gain 20% is effectively K for ink).
        return [0.0, 0.0, 0.0, (1.0 - float(g01)) * 100.0]

    def lab_to_cmyk(self, lab) -> list[float]:
        return [float(x) for x in self._lab_to_cmyk(lab)[0]]

    def icc_to_cmyk(self, icc_bytes: bytes, n: int, comps) -> list[float]:
        digest = hashlib.sha256(icc_bytes).hexdigest()
        if digest not in self._icc_cache:
            fmt = {1: TYPE_GRAY_DBL, 3: TYPE_RGB_DBL, 4: TYPE_CMYK_DBL}[n]
            self._icc_cache[digest] = (_open_bytes(icc_bytes), fmt)
        prof, fmt = self._icc_cache[digest]
        scale = 100.0 if n == 4 else 1.0
        t = Transform(prof, fmt, self._press, TYPE_CMYK_DBL)
        return [float(x) for x in t([c * scale for c in comps])[0]]


def delta_e00(lab1, lab2) -> float:
    """CIEDE2000 (kL=kC=kH=1)."""
    import colour

    return float(colour.delta_E(np.asarray(lab1), np.asarray(lab2), method="CIE 2000"))


def delta_e00_matrix(labs_a: np.ndarray, labs_b: np.ndarray) -> np.ndarray:
    import colour

    a = np.asarray(labs_a)[:, None, :]
    b = np.asarray(labs_b)[None, :, :]
    a, b = np.broadcast_arrays(a, b)
    return colour.delta_E(a, b, method="CIE 2000")


def tac(cmyk_pct) -> float:
    return float(sum(cmyk_pct))


def round1(x: float) -> float:
    r = round(float(x) + 0.0, 1)
    return 0.0 if r == 0 else r


if sys.platform == "win32":  # pragma: no cover - station only
    os.environ.setdefault("PROOF_LCMS2", "lcms2.dll")

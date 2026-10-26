"""PDF function evaluation (types 0, 2, 3, 4) for tint transforms and shadings."""

from __future__ import annotations

import math
import struct

import pikepdf


def _floats(arr) -> list[float]:
    return [float(x) for x in arr] if arr is not None else []


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


class PDFFunction:
    """Callable wrapper over a PDF function object (or an array of 1-out functions)."""

    def __init__(self, obj):
        if isinstance(obj, pikepdf.Array):
            self._parts = [PDFFunction(o) for o in obj]
            self.domain = self._parts[0].domain
            return
        self._parts = None
        self.obj = obj
        self.type = int(obj.FunctionType)
        self.domain = _floats(obj.Domain)
        self.range = _floats(obj.get("/Range"))
        if self.type == 0:
            self._init_sampled()
        elif self.type == 2:
            self.c0 = _floats(obj.get("/C0")) or [0.0]
            self.c1 = _floats(obj.get("/C1")) or [1.0]
            self.n = float(obj.N)
        elif self.type == 3:
            self.funcs = [PDFFunction(f) for f in obj.Functions]
            self.bounds = _floats(obj.Bounds)
            self.encode = _floats(obj.Encode)
        elif self.type == 4:
            self.program = _parse_ps(obj.read_bytes().decode("latin1"))
        else:
            raise ValueError(f"unsupported function type {self.type}")

    def __call__(self, inputs) -> list[float]:
        if self._parts is not None:
            return [p(inputs)[0] for p in self._parts]
        ins = [_clip(float(x), self.domain[2 * i], self.domain[2 * i + 1]) for i, x in enumerate(inputs)]
        out = getattr(self, f"_eval{self.type}")(ins)
        if self.range:
            out = [_clip(v, self.range[2 * i], self.range[2 * i + 1]) for i, v in enumerate(out)]
        return out

    # type 0 -------------------------------------------------------------
    def _init_sampled(self):
        o = self.obj
        self.size = [int(s) for s in o.Size]
        self.bps = int(o.BitsPerSample)
        m = len(self.size)
        self.encode = _floats(o.get("/Encode")) or [v for s in self.size for v in (0, s - 1)]
        self.decode = _floats(o.get("/Decode")) or self.range
        n_out = len(self.range) // 2
        data = o.read_bytes()
        total = n_out * math.prod(self.size)
        self.samples = _unpack_bits(data, self.bps, total)
        self.n_out = n_out
        self.m = m

    def _eval0(self, ins):
        maxv = (1 << self.bps) - 1
        # Multilinear interpolation over m inputs.
        idx_f = []
        for i, x in enumerate(ins):
            d0, d1 = self.domain[2 * i], self.domain[2 * i + 1]
            e0, e1 = self.encode[2 * i], self.encode[2 * i + 1]
            e = e0 + (x - d0) * (e1 - e0) / (d1 - d0) if d1 != d0 else e0
            idx_f.append(_clip(e, 0, self.size[i] - 1))
        out = [0.0] * self.n_out
        for corner in range(1 << self.m):
            w, offset, stride = 1.0, 0, 1
            for i, f in enumerate(idx_f):
                lo = int(math.floor(f))
                hi = min(lo + 1, self.size[i] - 1)
                t = f - lo
                bit = (corner >> i) & 1
                w *= t if bit else (1 - t)
                offset += (hi if bit else lo) * stride
                stride *= self.size[i]
            if w == 0:
                continue
            for j in range(self.n_out):
                out[j] += w * self.samples[offset * self.n_out + j]
        res = []
        for j, s in enumerate(out):
            d0, d1 = self.decode[2 * j], self.decode[2 * j + 1]
            res.append(d0 + s * (d1 - d0) / maxv)
        return res

    # type 2 -------------------------------------------------------------
    def _eval2(self, ins):
        x = ins[0]
        xn = x**self.n if x > 0 or self.n == int(self.n) else 0.0
        return [c0 + xn * (c1 - c0) for c0, c1 in zip(self.c0, self.c1)]

    # type 3 -------------------------------------------------------------
    def _eval3(self, ins):
        x = ins[0]
        d0, d1 = self.domain
        edges = [d0, *self.bounds, d1]
        k = len(self.funcs) - 1
        for i, b in enumerate(self.bounds):
            if x < b:
                k = i
                break
        lo, hi = edges[k], edges[k + 1]
        e0, e1 = self.encode[2 * k], self.encode[2 * k + 1]
        t = e0 + (x - lo) * (e1 - e0) / (hi - lo) if hi != lo else e0
        return self.funcs[k]([t])

    # type 4 -------------------------------------------------------------
    def _eval4(self, ins):
        stack = list(ins)
        _run_ps(self.program, stack)
        return [float(v) for v in stack[-(len(self.range) // 2):]]


def _unpack_bits(data: bytes, bps: int, count: int) -> list[int]:
    if bps == 8:
        return list(data[:count])
    if bps == 16:
        return list(struct.unpack(f">{count}H", data[: count * 2]))
    out, acc, nbits, mask = [], 0, 0, (1 << bps) - 1
    for byte in data:
        acc = (acc << 8) | byte
        nbits += 8
        while nbits >= bps and len(out) < count:
            nbits -= bps
            out.append((acc >> nbits) & mask)
    return out


# PostScript calculator (type 4) ---------------------------------------------


def _tokenize(src: str):
    src = src.replace("{", " { ").replace("}", " } ")
    return src.split()


def _parse_ps(src: str):
    tokens = _tokenize(src)
    pos = 0

    def block():
        nonlocal pos
        assert tokens[pos] == "{"
        pos += 1
        items = []
        while tokens[pos] != "}":
            if tokens[pos] == "{":
                items.append(block())
            else:
                t = tokens[pos]
                try:
                    items.append(float(t) if any(c in t for c in ".eE") else int(t))
                except ValueError:
                    items.append(t)
                pos += 1
        pos += 1
        return items

    return block()


def _run_ps(prog, s):
    i = 0
    while i < len(prog):
        t = prog[i]
        if isinstance(t, list):
            if i + 1 < len(prog) and prog[i + 1] == "if":
                if s.pop():
                    _run_ps(t, s)
                i += 2
                continue
            if i + 2 < len(prog) and isinstance(prog[i + 1], list) and prog[i + 2] == "ifelse":
                _run_ps(t if s.pop() else prog[i + 1], s)
                i += 3
                continue
            raise ValueError("bad PostScript block")
        if isinstance(t, (int, float)):
            s.append(t)
        else:
            _PS_OPS[t](s)
        i += 1


def _b2(f):
    def op(s):
        b, a = s.pop(), s.pop()
        s.append(f(a, b))
    return op


def _b1(f):
    def op(s):
        s.append(f(s.pop()))
    return op


def _roll(s):
    j, n = int(s.pop()), int(s.pop())
    if n:
        part = s[-n:]
        j %= n
        s[-n:] = part[-j:] + part[:-j] if j else part


def _index(s):
    n = int(s.pop())
    s.append(s[-1 - n])


def _copy(s):
    n = int(s.pop())
    if n:
        s.extend(s[-n:])


_PS_OPS = {
    "add": _b2(lambda a, b: a + b), "sub": _b2(lambda a, b: a - b),
    "mul": _b2(lambda a, b: a * b), "div": _b2(lambda a, b: a / b if b else 0.0),
    "idiv": _b2(lambda a, b: int(a) // int(b) if b else 0), "mod": _b2(lambda a, b: int(a) % int(b) if b else 0),
    "exp": _b2(lambda a, b: a**b), "atan": _b2(lambda a, b: math.degrees(math.atan2(a, b)) % 360),
    "eq": _b2(lambda a, b: a == b), "ne": _b2(lambda a, b: a != b), "gt": _b2(lambda a, b: a > b),
    "ge": _b2(lambda a, b: a >= b), "lt": _b2(lambda a, b: a < b), "le": _b2(lambda a, b: a <= b),
    "and": _b2(lambda a, b: a & b if isinstance(a, int) and not isinstance(a, bool) else a and b),
    "or": _b2(lambda a, b: a | b if isinstance(a, int) and not isinstance(a, bool) else a or b),
    "xor": _b2(lambda a, b: a ^ b), "bitshift": _b2(lambda a, b: a << b if b >= 0 else a >> -b),
    "abs": _b1(abs), "neg": _b1(lambda a: -a), "ceiling": _b1(math.ceil), "floor": _b1(math.floor),
    "round": _b1(lambda a: math.floor(a + 0.5)), "truncate": _b1(math.trunc), "sqrt": _b1(math.sqrt),
    "sin": _b1(lambda a: math.sin(math.radians(a))), "cos": _b1(lambda a: math.cos(math.radians(a))),
    "ln": _b1(math.log), "log": _b1(math.log10), "cvi": _b1(int), "cvr": _b1(float),
    "not": _b1(lambda a: not a if isinstance(a, bool) else ~a),
    "true": lambda s: s.append(True), "false": lambda s: s.append(False),
    "dup": lambda s: s.append(s[-1]), "pop": lambda s: s.pop(),
    "exch": lambda s: s.extend([s.pop(), s.pop()]), "roll": _roll, "index": _index, "copy": _copy,
}

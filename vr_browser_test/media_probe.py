"""Container probing for streamed video (DESIGN_V2 A9): segment index, 360/3D header boxes, YouTube mesh.

Pure functions on bytes, plus async helpers that read byte ranges through a `fetch(a, b) -> bytes`
coroutine supplied by the caller (the server's chunked upstream fetcher; inclusive ranges).

    index = await build_index(fetch, size, mime)         -> G3 dict or raises ProbeError
    head  = await read_projection(fetch)                  -> {"st3d", "proj", "equi", "mesh"} or None
"""
from __future__ import annotations

import math
import struct
import zlib
from typing import Awaitable, Callable

Fetch = Callable[[int, int], Awaitable[bytes]]


class ProbeError(Exception):
    """The file has no index we can use (the phone then plays it with two elements)."""


class NeedMore(Exception):
    def __init__(self, upto: int) -> None:
        super().__init__(upto)
        self.upto = upto                    # bytes needed from offset 0


# ================================================================ ISO BMFF (mp4)
def boxes(b: bytes, start: int, end: int):
    """Yield (type, offset, header_size, size) of the boxes in b[start:end]; stops at a cut box
    (raises NeedMore with the end it would need)."""
    o = start
    while o + 8 <= end:
        sz, t = struct.unpack(">I4s", b[o:o + 8])
        hs = 8
        if sz == 1:
            if o + 16 > end:
                raise NeedMore(o + 16)
            sz = struct.unpack(">Q", b[o + 8:o + 16])[0]
            hs = 16
        elif sz == 0:
            sz = end - o
        if sz < hs:
            return
        yield t.decode("latin1"), o, hs, sz
        o += sz


def parse_sidx(b: bytes, off: int, hs: int, size: int) -> dict:
    p = off + hs
    version = b[p]
    p += 4
    _ref_id, timescale = struct.unpack(">II", b[p:p + 8])
    p += 8
    if version == 0:
        earliest, first_offset = struct.unpack(">II", b[p:p + 8])
        p += 8
    else:
        earliest, first_offset = struct.unpack(">QQ", b[p:p + 16])
        p += 16
    p += 2
    count = struct.unpack(">H", b[p:p + 2])[0]
    p += 2
    refs = []
    for _ in range(count):
        a, dur, _sap = struct.unpack(">III", b[p:p + 12])
        p += 12
        if a >> 31:
            raise ProbeError("nested sidx is not supported")
        refs.append((a & 0x7FFFFFFF, dur))
    if not timescale:
        raise ProbeError("sidx timescale 0")
    segs, pos, t = [], off + size + first_offset, earliest
    for sz, dur in refs:
        segs.append([pos, pos + sz - 1, round(t / timescale, 4), round(dur / timescale, 4)])
        pos += sz
        t += dur
    return {"segs": segs, "dur": round((t - earliest) / timescale, 3) if refs else 0.0}


def mp4_index(b: bytes, size: int | None) -> dict:
    """Top-level walk of an mp4 head: init = [0, end of moov - 1], segments from sidx."""
    moov_end = None
    for t, off, hs, sz in boxes(b, 0, len(b)):
        if t in ("moof", "mdat"):
            break
        if off + sz > len(b):
            raise NeedMore(off + sz)
        if t == "moov":
            moov_end = off + sz
        elif t == "sidx":
            if moov_end is None:
                raise ProbeError("sidx before moov")
            r = parse_sidx(b, off, hs, sz)
            if not r["segs"]:
                raise ProbeError("empty sidx")
            return {"init": [0, moov_end - 1], "segs": r["segs"], "size": size, "dur": r["dur"]}
    else:
        raise NeedMore(len(b) + 65536)
    raise ProbeError("no sidx")


# ---------------------------------------------------------------- projection boxes (st3d / sv3d)
def parse_projection(b: bytes) -> dict | None:
    """moov/trak/mdia/minf/stbl/stsd/<visual sample entry> -> {"st3d": 0|1|2|None, "proj":
    "equi"|"cbmp"|"mesh"|None, "equi": [top, bottom, left, right] fractions | None, "mesh_raw": bytes|None}.
    None when the file is not mp4. NeedMore when moov is cut."""
    if len(b) < 8 or b[4:8] not in (b"ftyp", b"moov", b"styp", b"sidx", b"free"):
        return None
    moov = None
    for t, off, hs, sz in boxes(b, 0, len(b)):
        if t == "moov":
            if off + sz > len(b):
                raise NeedMore(off + sz)
            moov = (off + hs, off + sz)
            break
        if t in ("moof", "mdat"):
            break
        if off + sz > len(b):
            raise NeedMore(off + sz + 8)
    if moov is None:
        raise NeedMore(len(b) + 65536)
    res = {"st3d": None, "proj": None, "equi": None, "mesh_raw": None}

    def child(path, start, end):
        for t, off, hs, sz in boxes(b, start, end):
            if t == path[0]:
                if len(path) == 1:
                    return off, hs, sz
                r = child(path[1:], off + hs, off + sz)
                if r:
                    return r
        return None

    # every video trak (first one with a visual sample entry wins)
    for t, off, hs, sz in boxes(b, moov[0], moov[1]):
        if t != "trak":
            continue
        st = child(["mdia", "minf", "stbl", "stsd"], off + hs, off + sz)
        if not st:
            continue
        so, shs, ssz = st
        p = so + shs + 8                      # version/flags + entry_count
        if p + 8 > so + ssz:
            continue
        esz, etype = struct.unpack(">I4s", b[p:p + 8])
        if etype in (b"mp4a", b"Opus", b"ac-3", b"ec-3"):
            continue
        cstart, cend = p + 8 + 78, min(p + esz, so + ssz)
        for ct, co, chs, csz in boxes(b, cstart, cend):
            body = co + chs
            if ct == "st3d" and csz >= chs + 5:
                res["st3d"] = b[body + 4]
            elif ct == "sv3d":
                proj = child(["proj"], body, co + csz)
                if not proj:
                    continue
                po, phs, psz = proj
                for pt, qo, qhs, qsz in boxes(b, po + phs, po + psz):
                    qb = qo + qhs
                    if pt == "equi" and qsz >= qhs + 20:
                        vals = struct.unpack(">IIII", b[qb + 4:qb + 20])
                        res["proj"] = "equi"
                        res["equi"] = [v / 2 ** 32 for v in vals]
                    elif pt == "cbmp":
                        res["proj"] = "cbmp"
                    elif pt in ("ytmp", "mshp"):
                        res["proj"] = "mesh"
                        res["mesh_raw"] = bytes(b[qb + 4:qo + qsz])      # after version/flags
        return res
    return res


# ---------------------------------------------------------------- mesh (ytmp / mshp)
def _zz(v: int) -> int:
    return (v >> 1) ^ -(v & 1)


class _Bits:
    def __init__(self, b: bytes, pos: int) -> None:
        self.b, self.p = b, pos * 8

    def read(self, n: int) -> int:
        if n == 0:
            return 0
        s, e = self.p >> 3, (self.p + n + 7) >> 3
        v = int.from_bytes(self.b[s:e], "big")
        v >>= (e - s) * 8 - ((self.p & 7) + n)
        self.p += n
        return v & ((1 << n) - 1)

    def align(self) -> None:
        self.p = (self.p + 7) & ~7


def _bits_for(n: int) -> int:
    return math.ceil(math.log2(n * 2)) if n > 0 else 0


def _decode_one(m: bytes) -> tuple[list, list]:
    cc = struct.unpack(">I", m[0:4])[0]
    coords = struct.unpack(f">{cc}f", m[4:4 + 4 * cc])
    o = 4 + 4 * cc
    vc = struct.unpack(">I", m[o:o + 4])[0]
    o += 4
    bits = _bits_for(cc)
    br = _Bits(m, o)
    verts, idx = [], [0] * 5
    for _ in range(vc):
        for k in range(5):
            idx[k] += _zz(br.read(bits))
        verts.append(tuple(coords[i] for i in idx))
    br.align()
    o = br.p // 8
    lc = struct.unpack(">I", m[o:o + 4])[0]
    o += 4
    br = _Bits(m, o)
    vbits = _bits_for(vc)
    lists = []
    for _ in range(lc):
        tex, typ, n = br.read(8), br.read(8), br.read(32)
        last, ind = 0, []
        for _ in range(n):
            last += _zz(br.read(vbits))
            ind.append(last)
        lists.append((tex, typ, ind))
    return verts, lists


def decode_mesh(raw: bytes) -> dict:
    """raw = mshp/ytmp body after version/flags: crc32, encoding, payload -> G4 dict."""
    enc, data = raw[4:8], raw[8:]
    if enc == b"dfl8":
        data = zlib.decompress(data, -15)
    elif enc != b"raw ":
        raise ProbeError(f"unknown mesh encoding {enc!r}")
    meshes, j = [], 0
    while j + 8 <= len(data):
        s, t = struct.unpack(">I4s", data[j:j + 8])
        if s < 8:
            break
        if t == b"mesh":
            meshes.append(_decode_one(data[j + 8:j + s]))
        j += s
    if not meshes:
        raise ProbeError("no mesh in the projection box")
    out, yaws = [], []
    prim = {0: "triangles", 1: "strip", 2: "fan"}
    for i, (verts, lists) in enumerate(meshes):
        pos = [round(c, 5) for v in verts for c in v[:3]]
        uv = [round(c, 5) for v in verts for c in v[3:5]]
        yaws += [math.degrees(math.atan2(v[0], -v[2])) for v in verts if abs(v[0]) + abs(v[2]) > 1e-6]
        for _tex, typ, ind in lists:
            out.append({"pos": pos, "uv": uv, "idx": ind, "prim": prim.get(typ, "triangles"),
                        "eye": (i if len(meshes) == 2 else None)})
    span = (max(yaws) - min(yaws)) if yaws else 360.0
    return {"label": "VR180" if span <= 200 else "360 (EAC)", "meshes": out, "yaw_span": round(span, 1)}


# ================================================================ Matroska / WebM
def _vint(b: bytes, o: int, keep_marker: bool) -> tuple[int | None, int]:
    if o >= len(b):
        raise NeedMore(o + 8)
    first = b[o]
    n = 1
    while n <= 8 and not (first & (0x80 >> (n - 1))):
        n += 1
    if n > 8:
        raise ProbeError("bad EBML vint")
    if o + n > len(b):
        raise NeedMore(o + n)
    v = int.from_bytes(b[o:o + n], "big")
    if not keep_marker:
        v &= (1 << (7 * n)) - 1
        if v == (1 << (7 * n)) - 1:
            return None, n                   # unknown size
    return v, n


def _elems(b: bytes, start: int, end: int):
    """Yield (id, data_start, size|None, element_start) of the EBML elements in b[start:end]."""
    o = start
    while o < end:
        try:
            eid, n1 = _vint(b, o, True)
            sz, n2 = _vint(b, o + n1, False)
        except NeedMore:
            return
        ds = o + n1 + n2
        yield eid, ds, sz, o
        if sz is None:
            return
        o = ds + sz


def _uint(b: bytes, s: int, n: int) -> int:
    return int.from_bytes(b[s:s + n], "big")


EBML_MAGIC = bytes.fromhex("1a45dfa3")
ID_SEGMENT, ID_SEEKHEAD, ID_INFO, ID_CUES, ID_CLUSTER = 0x18538067, 0x114D9B74, 0x1549A966, 0x1C53BB6B, 0x1F43B675


def webm_head(b: bytes) -> dict:
    """-> {"seg": segment data offset, "seg_end": int|None, "scale": ns per tick, "dur": ticks|None,
    "cues": absolute offset|None, "cluster": first cluster absolute offset|None}"""
    if b[:4] != EBML_MAGIC:
        raise ProbeError("not webm")
    _eid, ds, sz, _o = next(_elems(b, 0, len(b)))
    o = ds + (sz or 0)
    eid, n1 = _vint(b, o, True)
    if eid != ID_SEGMENT:
        raise ProbeError("no Segment")
    ssz, n2 = _vint(b, o + n1, False)
    seg = o + n1 + n2
    res = {"seg": seg, "seg_end": seg + ssz if ssz is not None else None, "scale": 1_000_000, "dur": None,
           "cues": None, "cluster": None}
    for eid, ds, sz, el in _elems(b, seg, len(b)):
        if eid == ID_SEEKHEAD and sz is not None and ds + sz <= len(b):
            for sid, sds, ssz2, _ in _elems(b, ds, ds + sz):
                if sid != 0x4DBB or ssz2 is None:
                    continue
                target, pos = None, None
                for kid, kds, ksz, _ in _elems(b, sds, sds + ssz2):
                    if kid == 0x53AB:
                        target = _uint(b, kds, ksz)
                    elif kid == 0x53AC:
                        pos = _uint(b, kds, ksz)
                if target == ID_CUES and pos is not None:
                    res["cues"] = seg + pos
                elif target == ID_CLUSTER and pos is not None and res["cluster"] is None:
                    res["cluster"] = seg + pos
        elif eid == ID_INFO and sz is not None and ds + sz <= len(b):
            for kid, kds, ksz, _ in _elems(b, ds, ds + sz):
                if kid == 0x2AD7B1:
                    res["scale"] = _uint(b, kds, ksz) or 1_000_000
                elif kid == 0x4489:
                    res["dur"] = struct.unpack(">f" if ksz == 4 else ">d", b[kds:kds + ksz])[0]
        elif eid == ID_CUES and res["cues"] is None:
            res["cues"] = el
        elif eid == ID_CLUSTER:
            if res["cluster"] is None:
                res["cluster"] = el
            break
        if sz is None or ds + sz > len(b):
            break
    return res


def parse_cues(b: bytes, base: int) -> list[tuple[int, int]]:
    """b starts with the Cues element -> [(CueTime ticks, cluster position relative to the segment)]."""
    eid, n1 = _vint(b, 0, True)
    if eid != ID_CUES:
        raise ProbeError("Cues not found at the SeekHead position")
    sz, n2 = _vint(b, n1, False)
    ds = n1 + n2
    if sz is None or ds + sz > len(b):
        raise NeedMore(base + ds + (sz or 0))
    out = []
    for cid, cds, csz, _ in _elems(b, ds, ds + sz):
        if cid != 0xBB or csz is None:
            continue
        t, pos = None, None
        for kid, kds, ksz, _ in _elems(b, cds, cds + csz):
            if kid == 0xB3:
                t = _uint(b, kds, ksz)
            elif kid == 0xB7 and pos is None:
                for pid, pds, psz, _ in _elems(b, kds, kds + ksz):
                    if pid == 0xF1:
                        pos = _uint(b, pds, psz)
        if t is not None and pos is not None:
            out.append((t, pos))
    return out


# ================================================================ async helpers
async def _grow(fetch: Fetch, b: bytes, upto: int, limit: int) -> bytes:
    if upto > limit:
        raise ProbeError(f"header larger than {limit} bytes")
    if upto <= len(b):
        return b
    more = await fetch(len(b), upto - 1)
    if not more:
        raise ProbeError("short read")
    return b + more


async def build_index(fetch: Fetch, size: int | None, mime: str, head: bytes = b"") -> dict:
    """G3 index for a DASH mp4 (sidx) or webm (Cues) file. Raises ProbeError when there is none."""
    b = head or await fetch(0, 65535)
    if b[:4] == b"\x1a\x45\xdf\xa3":
        h = webm_head(b)
        if h["cues"] is None:
            raise ProbeError("webm without Cues in the SeekHead")
        cb = await fetch(h["cues"], h["cues"] + 65535)
        try:
            cues = parse_cues(cb, h["cues"])
        except NeedMore as e:
            if e.upto - h["cues"] > 4 * 1024 * 1024:
                raise ProbeError("Cues too big")
            cb = await fetch(h["cues"], e.upto - 1)
            cues = parse_cues(cb, h["cues"])
        if not cues:
            raise ProbeError("empty Cues")
        scale = h["scale"]
        dur = (h["dur"] or 0) * scale / 1e9
        starts = sorted({h["seg"] + p for _, p in cues})
        times = {}
        for t, p in cues:
            times.setdefault(h["seg"] + p, t * scale / 1e9)
        end_all = (size or h["seg_end"] or 0) - 1
        if h["seg_end"]:
            end_all = min(end_all, h["seg_end"] - 1) if size else h["seg_end"] - 1
        if h["cues"] > starts[-1]:
            end_all = h["cues"] - 1
        segs = []
        for i, s in enumerate(starts):
            e = starts[i + 1] - 1 if i + 1 < len(starts) else end_all
            t = times[s]
            nt = times[starts[i + 1]] if i + 1 < len(starts) else (dur or t)
            segs.append([s, e, round(t, 4), round(max(0.0, nt - t), 4)])
        first = min(starts[0], h["cluster"] or starts[0])
        return {"init": [0, first - 1], "segs": segs, "size": size, "dur": round(dur or segs[-1][2] + segs[-1][3], 3),
                "mime": mime}
    for _ in range(6):
        try:
            r = mp4_index(b, size)
            r["mime"] = mime
            return r
        except NeedMore as e:
            b = await _grow(fetch, b, e.upto, 1024 * 1024)
    raise ProbeError("no sidx in the first MiB")


async def read_projection(fetch: Fetch, head: bytes = b"") -> dict | None:
    """Header boxes of an mp4 (st3d/sv3d). Reads bytes 0-262143, up to 2 MiB if moov is longer."""
    b = head if len(head) >= 262144 else await fetch(0, 262143)
    for _ in range(4):
        try:
            return parse_projection(b)
        except NeedMore as e:
            b = await _grow(fetch, b, e.upto, 2 * 1024 * 1024)
    return None

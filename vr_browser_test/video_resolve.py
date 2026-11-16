"""Turn "a video is playing on this page" into something the phone can stream (DESIGN_V2 A, C).

- Resolver.lookup: yt-dlp -J, kept in memory only (never written to disk), prefetched on navigation.
- pick_streams: the best video-only + audio-only https pair the phone can decode (its "caps").
- detect_projection: user choice / container boxes / format note / title (C2).
- Resolver.download: the v1 download+merge, only as the last resort.

yt-dlp always runs as a subprocess (asyncio), never in the event loop. Cookie files (and the info
file of the last-resort download) live in cache/ only while yt-dlp runs.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

ProgressCb = Callable[[float, str], Awaitable[None]]

# ---------------------------------------------------------------- Windows job object
_JOB = None


def add_to_job(pid: int) -> None:
    """Put a process in this server's kill-on-close job object: it (and every child it starts later,
    e.g. yt-dlp's ffmpeg merger) dies with the server, even if the server is force-killed.
    Raises OSError when that is not possible (callers log it)."""
    global _JOB
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    if _JOB is None:
        job = k32.CreateJobObjectW(None, None)

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXT(ctypes.Structure):
            _fields_ = [("Basic", BASIC), ("Io", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        info = EXT()
        info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error())
        _JOB = job
    h = k32.OpenProcess(0x0101, False, pid)    # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if not h:
        raise OSError(ctypes.get_last_error())
    ok = k32.AssignProcessToJobObject(wintypes.HANDLE(_JOB), wintypes.HANDLE(h))
    k32.CloseHandle(wintypes.HANDLE(h))
    if not ok:
        raise OSError(ctypes.get_last_error())


async def _spawn(args: list[str], **kw) -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, creationflags=NO_WINDOW, **kw)
    try:
        add_to_job(proc.pid)
    except Exception:
        pass                                     # still killed as a tree on cancel (_kill_tree)
    return proc


async def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Stop a yt-dlp/ffmpeg process AND its children (yt-dlp's ffmpeg merger, node/deno)."""
    if proc.returncode is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            k = await asyncio.create_subprocess_exec("taskkill", "/T", "/F", "/PID", str(proc.pid),
                                                     stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.DEVNULL, creationflags=NO_WINDOW)
            await asyncio.wait_for(k.wait(), 10)
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), 10)


class ResolveError(Exception):
    """yt-dlp could not handle this page (try the next method)."""


# ---------------------------------------------------------------- tools
def find_ffmpeg() -> str | None:
    """Same search as bridge/hmd_bridge.py: PATH, then winget's folders, then tools/ffmpeg."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet"
    candidates = [winget / "Links" / "ffmpeg.exe"]
    candidates += sorted((winget / "Packages").glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"), reverse=True)
    candidates += [HERE.parent / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe", Path("C:/ffmpeg/bin/ffmpeg.exe")]
    return next((str(c) for c in candidates if c.is_file()), None)


def find_ffprobe(ffmpeg: str | None) -> str | None:
    found = shutil.which("ffprobe")
    if found:
        return found
    if ffmpeg:
        p = Path(ffmpeg).with_name("ffprobe.exe")
        if p.is_file():
            return str(p)
    return None


_YTDLP_PY: str | None = None


def find_ytdlp_python() -> str | None:
    """A Python that has yt_dlp installed (the server may run from the venv, yt-dlp may live in C:/Python314)."""
    global _YTDLP_PY
    if _YTDLP_PY:
        return _YTDLP_PY
    cands = [sys.executable, "C:/Python314/python.exe", shutil.which("python") or ""]
    for c in cands:
        if not c or not Path(c).is_file():
            continue
        try:
            r = subprocess.run([c, "-m", "yt_dlp", "--version"], capture_output=True, text=True, timeout=30,
                               creationflags=NO_WINDOW)
            if r.returncode == 0:
                _YTDLP_PY = c
                return c
        except Exception:
            pass
    return None


def js_runtime_args() -> list[str]:
    """YouTube needs a JavaScript runtime for some formats; deno is yt-dlp's default, node also works."""
    if shutil.which("deno"):
        return []
    if shutil.which("node"):
        return ["--js-runtimes", "node"]
    return []


# ---------------------------------------------------------------- urls
YT_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com")


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in YT_HOSTS)


def youtube_id(url: str) -> str | None:
    u = urlparse(url)
    host = (u.hostname or "").lower()
    if not is_youtube(url):
        return None
    if host.endswith("youtu.be"):
        vid = u.path.strip("/").split("/")[0]
    else:
        vid = (parse_qs(u.query).get("v") or [""])[0]
        if not vid:
            m = re.match(r"^/(?:shorts|embed|live|v)/([\w-]{6,})", u.path)
            vid = m.group(1) if m else ""
    return vid if re.fullmatch(r"[\w-]{6,20}", vid or "") else None


def youtube_watch_url(url: str) -> str | None:
    vid = youtube_id(url)
    return f"https://www.youtube.com/watch?v={vid}" if vid else None


def youtube_start(url: str) -> float | None:
    t = (parse_qs(urlparse(url).query).get("t") or [""])[0]
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(\d+)s?", t or "")
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


# ---------------------------------------------------------------- cookies
def write_cookies_txt(cookies: list[dict], path: Path) -> None:
    """CDP cookies -> Netscape cookies.txt (what yt-dlp --cookies reads)."""
    lines = ["# Netscape HTTP Cookie File", ""]
    for c in cookies:
        domain = str(c.get("domain", ""))
        name, value = str(c.get("name", "")), str(c.get("value", ""))
        if not domain or not name or "\n" in value or "\t" in value:
            continue
        exp = c.get("expires", -1)
        exp = int(exp) if isinstance(exp, (int, float)) and exp > 0 else 0
        prefix = "#HttpOnly_" if c.get("httpOnly") else ""
        lines.append("\t".join([prefix + domain, "TRUE" if domain.startswith(".") else "FALSE",
                                str(c.get("path", "/")) or "/", "TRUE" if c.get("secure") else "FALSE",
                                str(exp), name, value]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def _run(args: list[str], timeout: float) -> tuple[int, bytes, bytes]:
    proc = await _spawn(args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        return proc.returncode or 0, out, err
    finally:
        if proc.returncode is None:
            await _kill_tree(proc)


# ---------------------------------------------------------------- projections (DESIGN_V2 C1)
PROJECTIONS = ("flat", "flat_sbs", "flat_tb", "equirect", "equirect_tb", "equirect_sbs",
               "half_sbs", "half_mono", "half_tb", "eac", "mesh")
STEREO_OF = {"flat": "mono", "flat_sbs": "sbs", "flat_tb": "tb", "equirect": "mono", "equirect_tb": "tb",
             "equirect_sbs": "sbs", "half_sbs": "sbs", "half_mono": "mono", "half_tb": "tb"}
ST3D = {0: "mono", 1: "tb", 2: "sbs"}

RE_SBS = re.compile(r"\b(h-?sbs|half[- ]?sbs|f-?sbs|full[- ]?sbs|sbs|side[- ]by[- ]side|left[- ]right|lr 3d)\b")
RE_TB = re.compile(r"\b(h-?ou|over[- ]under|top[- /]?bottom|tab|ou|3d tb)\b")
RE_180 = re.compile(r"vr ?180|180 ?°|180vr")
RE_360 = re.compile(r"360|equirect|spherical")
RE_3D = re.compile(r"\b3d\b|stereo(scopic)?")
RE_SPHERE_NOTE = re.compile(r"\b\d{3,4}s(\d\d)?\b")


def _text(info: dict) -> str:
    return " ".join([str(info.get("title") or ""), " ".join(map(str, info.get("tags") or [])),
                     str(info.get("description") or "")[:300]]).lower()


def note_projection(fmt: dict | None, info: dict | None = None) -> str | None:
    """C2 step 2: YouTube's format_note ("1080s, equi", "equi_threed_top_bottom", "mesh")."""
    note = str((fmt or {}).get("format_note") or "").lower()
    if "equi_threed_top_bottom" in note:
        return "equirect_tb"
    if "equi_threed_left_right" in note:
        return "equirect_sbs"
    if "equi" in note:
        return "equirect"
    if "mesh" in note:
        t = str((info or {}).get("title") or "").lower()
        return "half_sbs" if RE_180.search(t) or re.search(r"\b180\b", t) else "eac"
    return None


def old3d_sign(info: dict, fmt: dict | None) -> bool:
    """Old YouTube 3D: the DASH copy is squeezed (~0.89:1) while the HLS copy of the same height is 16:9."""
    w, h = int((fmt or {}).get("width") or 0), int((fmt or {}).get("height") or 0)
    if not w or not h or not 0.85 <= w / h <= 0.92:
        return False
    for f in info.get("formats") or []:
        if "m3u8" in str(f.get("protocol") or "") and int(f.get("height") or 0) == h \
                and abs(int(f.get("width") or 0) / h - 1.78) < 0.05:
            return True
    return False


def guess_projection(info: dict, w: int, h: int) -> tuple[str, str]:
    """C2 step 3: title, tags and the start of the description + the frame shape -> (projection, source)."""
    text = _text(info)
    ar = w / h if w and h else 0
    sbs, tb = bool(RE_SBS.search(text)), bool(RE_TB.search(text))
    is180, is360, is3d = bool(RE_180.search(text)), bool(RE_360.search(text)), bool(RE_3D.search(text))
    if is360:
        if tb or 0.9 <= ar <= 1.1:
            return "equirect_tb", "title"
        if sbs and ar >= 3.5:
            return "equirect_sbs", "title"
        return "equirect", "title"
    if is180 and (is3d or sbs):
        return "half_sbs", "title"
    if sbs:
        return "flat_sbs", "title"
    if tb:
        return "flat_tb", "title"
    return "flat", "guess"


def container_projection(c: dict | None) -> dict | None:
    """C2 step 1 from media_probe.parse_projection -> {"projection","stereo","mesh_label"?} or None."""
    if not c:
        return None
    st = c.get("st3d")
    stereo = ST3D.get(st, "mono") if st is not None else "mono"
    proj = c.get("proj")
    if proj == "equi":
        bounds = c.get("equi") or [0, 0, 0, 0]
        half = bounds[2] >= 0.2 and bounds[3] >= 0.2
        fam = {"mono": "half_mono", "tb": "half_tb", "sbs": "half_sbs"} if half else \
            {"mono": "equirect", "tb": "equirect_tb", "sbs": "equirect_sbs"}
        return {"projection": fam[stereo], "stereo": stereo}
    if proj == "cbmp":
        return {"projection": "eac", "stereo": stereo}
    if proj == "mesh":
        return {"projection": "mesh", "stereo": stereo}
    if st == 1:
        return {"projection": "flat_tb", "stereo": "tb"}
    if st == 2:
        return {"projection": "flat_sbs", "stereo": "sbs"}
    return None


def detect_projection(info: dict, vfmt: dict | None, container: dict | None, header_ok: bool,
                      saved: dict | None, w: int, h: int, mesh_label: str | None = None) -> dict:
    """DESIGN_V2 C2. container = media_probe.parse_projection result (header_ok False = the read failed).
    -> {"projection","stereo","projection_source","mesh":bool,"mesh_label"}"""
    has_mesh = bool(container and container.get("proj") == "mesh")
    st = (container or {}).get("st3d")
    c_stereo = ST3D.get(st, "mono") if st is not None else "mono"

    def out(proj: str, src: str, stereo: str | None = None) -> dict:
        if stereo is None:
            stereo = STEREO_OF.get(proj, c_stereo)
        return {"projection": proj, "stereo": stereo, "projection_source": src, "mesh": has_mesh,
                "mesh_label": mesh_label if has_mesh else None}

    saved = saved or {}
    up = saved.get("user_projection")
    if up in PROJECTIONS and (up != "mesh" or has_mesh):
        return out(up, "user")
    if saved.get("v") == 2 and saved.get("projection_source") == "image" and saved.get("projection") in PROJECTIONS \
            and (saved.get("projection") != "mesh" or has_mesh):
        return out(saved["projection"], "image")
    cp = container_projection(container)
    if cp:
        return out(cp["projection"], "container", cp["stereo"])
    if not header_ok:
        npj = note_projection(vfmt, info)
        if npj:
            return out(npj, "note")
        if old3d_sign(info, vfmt):
            return out("flat_tb", "note")
    proj, src = guess_projection(info, w, h)
    return out(proj, src)


# ---------------------------------------------------------------- cache/<key>.json (G5)
def valid_key(key: str | None) -> bool:
    return bool(key) and bool(re.fullmatch(r"[\w-]{1,100}", key or ""))


def read_meta(key: str) -> dict | None:
    if not valid_key(key):
        return None
    try:
        d = json.loads((CACHE / f"{key}.json").read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def write_meta(key: str, **fields) -> None:
    """Merge fields into the v2 metadata file (never URLs or cookies)."""
    if not valid_key(key):
        return
    old = read_meta(key) or {}
    m = {"v": 2, "title": old.get("title"), "w": old.get("w"), "h": old.get("h"),
         "projection": old.get("projection"), "stereo": old.get("stereo"),
         "projection_source": old.get("projection_source"), "mesh": bool(old.get("mesh")),
         "user_projection": old.get("user_projection") if old.get("v") == 2 else None,
         "image_scores": old.get("image_scores") if old.get("v") == 2 else None}
    m.update(fields)
    m["updated"] = int(time.time())
    with contextlib.suppress(OSError):
        (CACHE / f"{key}.json").write_text(json.dumps(m), encoding="utf-8")


def cached_flat_file(key: str) -> dict | None:
    """A3.2: an old downloaded mp4 whose metadata says flat -> {"path","title","w","h","meta"}."""
    if not valid_key(key):
        return None
    mp4 = CACHE / f"{key}.mp4"
    m = read_meta(key)
    if not m or m.get("projection") != "flat" or not mp4.is_file() or mp4.stat().st_size <= 0:
        return None
    return {"path": str(mp4), "title": m.get("title"), "w": int(m.get("w") or 0), "h": int(m.get("h") or 0),
            "meta": m}


# ---------------------------------------------------------------- phone capabilities (G2 caps)
FAMILIES = ("avc1", "vp9", "vp9mp4", "av1")
DEFAULT_CAPS = {"mse": "none", "mtex": 4096, "vtex": 4096,
                "v": {"avc1": {"file": [1080, 1080, 1080, 1080], "mse": [0, 0, 0, 0]}},
                "a": {"aac": True, "opus": False}}


def sanitize_caps(m: dict) -> dict | None:
    if not isinstance(m, dict):
        return None
    mse = m.get("mse") if m.get("mse") in ("mms", "mse", "none") else "none"
    try:
        mtex = max(1024, min(32768, int(m.get("mtex") or 4096)))
    except (TypeError, ValueError):
        mtex = 4096
    try:                                          # widest video texture the page wants to upload per frame
        vtex = max(1024, min(mtex, int(m.get("vtex") or mtex)))
    except (TypeError, ValueError):
        vtex = mtex
    v = {}
    vin = m.get("v") if isinstance(m.get("v"), dict) else {}
    for fam in FAMILIES:
        row = vin.get(fam)
        if not isinstance(row, dict):
            continue
        r = {}
        for t in ("mse", "file"):
            x = row.get(t)
            if isinstance(x, list) and len(x) == 4:
                try:
                    r[t] = [max(0, min(8640, int(n or 0))) for n in x]
                except (TypeError, ValueError):
                    continue
        if r:
            v[fam] = r
    a = m.get("a") if isinstance(m.get("a"), dict) else {}
    return {"mse": mse, "mtex": mtex, "vtex": vtex, "v": v,
            "a": {"aac": bool(a.get("aac")), "opus": bool(a.get("opus"))}}


# ---------------------------------------------------------------- format choice (A5)
def _http(f: dict) -> bool:
    return str(f.get("protocol") or "") in ("http", "https")


def _has_audio(f: dict) -> bool:
    return str(f.get("acodec") or "none") != "none"


def _has_video(f: dict) -> bool:
    return str(f.get("vcodec") or "none") != "none"


def codec_family(f: dict) -> str | None:
    vc = str(f.get("vcodec") or "").lower()
    if vc.startswith("avc1") or vc.startswith("avc3") or vc == "h264":
        return "avc1"
    if vc.startswith("vp09") or vc.startswith("vp9"):
        return "vp9" if str(f.get("ext")) == "webm" else "vp9mp4"
    if vc.startswith("av01"):
        return "av1"
    return None


def content_kind(info: dict) -> str:
    for f in info.get("formats") or []:
        if not _http(f) or not _has_video(f):
            continue
        note = str(f.get("format_note") or "").lower()
        if RE_SPHERE_NOTE.search(note) or "equi" in note or "mesh" in note:
            return "sphere"
    text = _text(info)
    if RE_SBS.search(text) or RE_TB.search(text):
        return "flat3d"
    return "flat"


KIND_CAP = {"flat": 1080, "flat3d": 2160, "sphere": 4320}


def _short_side(f: dict) -> int:
    w, h = int(f.get("width") or 0), int(f.get("height") or 0)
    return min(w, h) if w and h else h            # portrait video (Shorts): the short side is the "height"


def _test(f: dict, caps: dict, t: str, fam: str) -> tuple[bool, bool]:
    row = (caps.get("v") or {}).get(fam, {}).get(t)
    if not row:
        return False, False
    h, w = _short_side(f), max(int(f.get("width") or 0), int(f.get("height") or 0))
    fps60 = float(f.get("fps") or 30) > 31
    budget = min(int(caps.get("mtex") or 4096), int(caps.get("vtex") or caps.get("mtex") or 4096))
    playable = 0 < h <= (row[1] if fps60 else row[0]) and w <= budget
    pe = playable and h <= (row[3] if fps60 else row[2])
    return playable, pe


def mime_of(f: dict) -> str:
    ext = str(f.get("ext") or "")
    if _has_video(f):
        return f'video/{"webm" if ext == "webm" else "mp4"}; codecs="{f.get("vcodec")}"'
    return f'audio/{"webm" if ext == "webm" else "mp4"}; codecs="{f.get("acodec")}"'


def pick_audio(info: dict, caps: dict, video_ext: str) -> dict | None:
    cands = [f for f in info.get("formats") or []
             if not _has_video(f) and _has_audio(f) and _http(f) and f.get("url") and not f.get("has_drm")
             and "drc" not in str(f.get("format_id") or "").lower()
             and "drc" not in str(f.get("format_note") or "").lower()]
    if not cands:
        return None
    tiers = [[f for f in cands if "(default)" in str(f.get("format_note") or "")],
             [f for f in cands if info.get("language") and f.get("language") == info.get("language")], cands]
    cands = next(t for t in tiers if t)
    a = caps.get("a") or {}

    def is_opus(f):
        return str(f.get("acodec") or "").lower().startswith("opus") and str(f.get("ext")) == "webm"

    def is_aac(f):
        return str(f.get("acodec") or "").lower().startswith("mp4a") and str(f.get("ext")) in ("m4a", "mp4")

    opus, aac = [f for f in cands if is_opus(f)], [f for f in cands if is_aac(f)]
    if video_ext == "webm" and a.get("opus") and opus:
        pool = opus
    elif a.get("aac") and aac:
        pool = aac
    elif a.get("opus") and opus:
        pool = opus
    else:
        return None

    def score(f):
        abr = float(f.get("abr") or f.get("tbr") or 0)
        return (abr > 160, abs(abr - 128))
    return min(pool, key=score)


def pick_streams(info: dict, caps: dict, kind: str, below: int | None = None) -> dict | None:
    """A5: best video-only + audio-only https pair the phone plays -> {"v","a","kind","family"} or None.
    below: only heights lower than this (a lower rung after the phone could not keep up)."""
    t = "mse" if caps.get("mse") != "none" else "file"
    cap = KIND_CAP.get(kind, 1080)
    if below:
        cap = min(cap, below - 1)
    cands = []
    for f in info.get("formats") or []:
        if not _http(f) or not f.get("url") or not _has_video(f) or _has_audio(f) or f.get("has_drm"):
            continue
        fam = codec_family(f)
        if not fam:
            continue
        h = _short_side(f)
        if not h or h > cap:
            continue
        ok, pe = _test(f, caps, t, fam)
        if ok:
            cands.append((f, fam, pe, h))
    if not cands:
        return None
    top = max(c[3] for c in cands)
    at = [c for c in cands if c[3] == top]
    pes = [c for c in at if c[2]]
    order = ["av1", "vp9", "vp9mp4", "avc1"] if pes else ["avc1", "vp9", "vp9mp4", "av1"]
    pool = pes or at
    best = min(pool, key=lambda c: (order.index(c[1]), -float(c[0].get("tbr") or 0)))
    v = best[0]
    aud = pick_audio(info, caps, str(v.get("ext")))
    if aud is None:
        return None
    return {"v": v, "a": aud, "kind": kind, "family": best[1]}


def pick_progressive(info: dict, caps: dict, kind: str = "flat") -> dict | None:
    """A3.3.2: best single https format with picture AND sound that the phone plays as a file."""
    cap = KIND_CAP.get(kind, 1080)
    best = None
    for f in info.get("formats") or [info]:
        if not f.get("url") or not _http(f) or f.get("has_drm"):
            continue
        if f.get("vcodec") is None:                      # generic extractor, codec unknown: mp4 only
            if str(f.get("ext")) != "mp4" or int(f.get("height") or 0) > cap:
                continue
        else:
            if not (_has_video(f) and _has_audio(f)):
                continue
            fam = codec_family(f)
            if not fam or _short_side(f) > cap or not _test(f, caps, "file", fam)[0]:
                continue
        if best is None or _short_side(f) > _short_side(best):
            best = f
    return best


def header_format(info: dict, chosen: dict) -> dict | None:
    """C2/A9: an mp4 format of the same video to read the 360/3D header boxes from."""
    # YouTube writes the sv3d/st3d boxes only into its "spherical" renditions (format_note "1080s", "2160s60",
    # ...); the plain 240p..720p files of a 360 video have none (measured on ZZyBG6UsvoQ). So read from a
    # sphere-note mp4 when the video has one, else from an mp4 of the chosen height (or the nearest).
    def sph(f: dict) -> bool:
        return bool(RE_SPHERE_NOTE.search(str(f.get("format_note") or "")))
    mp4s = [f for f in info.get("formats") or [] if _http(f) and f.get("url") and str(f.get("ext")) == "mp4"
            and codec_family(f) in ("avc1", "av1") and not _has_audio(f)]
    any_sph = any(sph(f) for f in mp4s)
    if str(chosen.get("ext")) == "mp4" and _http(chosen) and (sph(chosen) or not any_sph):
        return chosen
    ch = int(chosen.get("height") or 0)
    return min(mp4s, key=lambda f: (any_sph and not sph(f), abs(int(f.get("height") or 0) - ch),
                                    codec_family(f) != "avc1"), default=None)


def url_expire(url: str) -> float:
    """googlevideo's expire= (unix time); 6 h from now when absent."""
    q = parse_qs(urlparse(url).query)
    try:
        return float((q.get("expire") or [""])[0])
    except ValueError:
        pass
    m = re.search(r"/expire/(\d+)", urlparse(url).path)
    return float(m.group(1)) if m else time.time() + 6 * 3600


def describe_format(f: dict) -> str:
    if _has_video(f):
        return f"{f.get('format_id')} {f.get('width')}x{f.get('height')} {codec_family(f) or f.get('vcodec')}"
    return f"{f.get('format_id')} {'opus' if 'opus' in str(f.get('acodec')) else 'aac'}"


# ---------------------------------------------------------------- yt-dlp
def _safe_key(s: str) -> str:
    return re.sub(r"[^\w-]", "_", s)[:80]


def lookup_key(page_url: str) -> str:
    vid = youtube_id(page_url)
    return f"yt:{vid}" if vid else page_url.split("#")[0]


def info_key(info: dict) -> str:
    """cache/<key>.json name: youtube_<id> (same as v1), else <extractor>_<id>."""
    return _safe_key(f"{str(info.get('extractor_key', 'x')).lower()}_{info.get('id', 'x')}")


LOOKUP_TTL = 5 * 3600
LOOKUP_MAX = 30
PREFETCH_MAX = 2


HEDGE_AFTER = 14.0                              # s before a second yt-dlp -J is started


class Resolver:
    def __init__(self) -> None:
        self.ffmpeg = find_ffmpeg()
        self.py = find_ytdlp_python()
        self.mem: dict[str, tuple[float, dict]] = {}          # lookup key -> (time, info); memory only
        self.inflight: dict[str, asyncio.Task] = {}
        self.prefetching = 0
        CACHE.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        return (f"yt-dlp: {'yes (' + self.py + ')' if self.py else 'NOT FOUND'}; "
                f"ffmpeg: {self.ffmpeg or 'NOT FOUND'} (only for the last-resort download)")

    def cached_info(self, page_url: str) -> dict | None:
        e = self.mem.get(lookup_key(page_url))
        return e[1] if e and time.time() - e[0] < LOOKUP_TTL else None

    async def lookup(self, page_url: str, cookies: list[dict], force: bool = False) -> dict:
        """yt-dlp -J, kept in memory only (5 h, 30 entries). Concurrent calls for one video share one run;
        a cancelled caller does not stop the run (it still fills the cache). Raises ResolveError."""
        if not self.py:
            raise ResolveError("yt-dlp is not installed")
        key = lookup_key(page_url)
        if not force:
            hit = self.cached_info(page_url)
            if hit is not None:
                return hit
        task = self.inflight.get(key)
        if task is None:
            url = youtube_watch_url(page_url) or page_url
            task = asyncio.create_task(self._lookup(url, cookies))
            self.inflight[key] = task

            def done(t: asyncio.Task, key=key) -> None:
                if self.inflight.get(key) is t:
                    self.inflight.pop(key, None)
                if not t.cancelled() and t.exception() is None:
                    self.mem[key] = (time.time(), t.result())
                    while len(self.mem) > LOOKUP_MAX:
                        self.mem.pop(min(self.mem, key=lambda k: self.mem[k][0]))
            task.add_done_callback(done)
        info = await asyncio.shield(task)
        return info

    def want_prefetch(self, page_url: str) -> bool:
        key = lookup_key(page_url)
        return bool(self.py) and self.cached_info(page_url) is None and key not in self.inflight \
            and self.prefetching < PREFETCH_MAX

    async def prefetch(self, page_url: str, cookies_fn) -> None:
        """Background lookup on navigation (A4): at most 2 at once, others dropped; errors logged."""
        if not self.want_prefetch(page_url):
            return
        self.prefetching += 1
        try:
            t0 = time.monotonic()
            await self.lookup(page_url, await cookies_fn())
            print(f"[{time.strftime('%H:%M:%S')}] [video] prefetched {lookup_key(page_url)} "
                  f"({time.monotonic() - t0:.1f}s)", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{time.strftime('%H:%M:%S')}] [video] prefetch {lookup_key(page_url)} failed: {e}"[:200],
                  flush=True)
        finally:
            self.prefetching -= 1

    async def _lookup_once(self, url: str, cookies: list[dict]) -> dict:
        tag = secrets.token_hex(6)
        cookie_file = CACHE / f"cookies_{tag}.txt"
        try:
            write_cookies_txt(cookies, cookie_file)
            args = [self.py, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--socket-timeout", "10",
                    "--cookies", str(cookie_file), *js_runtime_args(), "-J", url]
            try:
                code, out, err = await _run(args, 90)
                if code != 0 or not out.strip():
                    last = (err.decode("utf-8", "replace").strip().splitlines() or ["no output"])[-1]
                    raise ResolveError(last[:200])
                return json.loads(out)
            except (asyncio.TimeoutError, ValueError, OSError) as e:
                raise ResolveError(f"lookup failed: {type(e).__name__} {e}"[:200]) from e
        finally:
            cookie_file.unlink(missing_ok=True)

    async def _lookup(self, url: str, cookies: list[dict]) -> dict:
        """yt-dlp -J normally takes ~6 s, but now and then one run hangs for a minute (a stalled request).
        Hedge: if the first run is not done after HEDGE_AFTER s, start a second one; the first result wins."""
        t0 = time.monotonic()
        runs = [asyncio.create_task(self._lookup_once(url, cookies))]
        info = None
        err: Exception | None = None
        try:
            done, _ = await asyncio.wait(runs, timeout=HEDGE_AFTER)
            if not done:
                print(f"[{time.strftime('%H:%M:%S')}] [video] yt-dlp slow ({HEDGE_AFTER:.0f}s): starting a second "
                      f"lookup in parallel", flush=True)
                runs.append(asyncio.create_task(self._lookup_once(url, cookies)))
            pending = set(runs)
            while pending and info is None:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() is None and info is None:
                        info = t.result()
                        if len(runs) > 1:
                            print(f"[{time.strftime('%H:%M:%S')}] [video] lookup run {runs.index(t) + 1} of "
                                  f"{len(runs)} won after {time.monotonic() - t0:.1f}s", flush=True)
                    elif t.exception() is not None:
                        err = t.exception()
        finally:
            for t in runs:
                if not t.done():
                    t.cancel()
            for t in runs:
                with contextlib.suppress(BaseException):
                    await t
        if info is None:
            raise err if err else ResolveError("lookup failed")
        if not isinstance(info, dict):
            raise ResolveError("unexpected yt-dlp output")
        if info.get("_type") == "playlist":
            entries = [e for e in info.get("entries") or [] if e]
            if not entries:
                raise ResolveError("page has no single video")
            info = entries[0]
        if info.get("is_live"):
            raise ResolveError("live streams are not supported")
        if str(info.get("extractor_key", "")).lower() == "generic" and not info.get("formats") and not info.get("url"):
            raise ResolveError("generic extractor found nothing")
        info.pop("requested_downloads", None)
        return info

    # ------------------------------------------------------------ last resort: v1 download + merge
    async def download(self, info: dict, cookies: list[dict], progress: ProgressCb) -> dict:
        """-> {"path","w","h","format"}. Only when yt-dlp has no https format the phone can stream."""
        tag = secrets.token_hex(6)
        cookie_file = CACHE / f"cookies_{tag}.txt"
        info_file = CACHE / f"info_{tag}.json"
        key = info_key(info)
        try:
            write_cookies_txt(cookies, cookie_file)
            info_file.write_text(json.dumps(info), encoding="utf-8")
            dur = float(info.get("duration") or 0)
            cap = 1080 if dur <= 1800 else 720
            sel = (f"bv*[vcodec^=avc1][height<={cap}][protocol^=http]+ba[ext=m4a]"
                   f"/bv*[vcodec^=avc1][height<={cap}]+ba[ext=m4a]/b[vcodec^=avc1][height<={cap}]"
                   f"/bv*[height<={cap}][ext=mp4]+ba[ext=m4a]/b[height<={cap}]/b")
            args = [self.py, "-m", "yt_dlp", "--no-playlist", "--no-warnings", "--cookies", str(cookie_file),
                    *js_runtime_args(), "--load-info-json", str(info_file), "-f", sel,
                    "--merge-output-format", "mp4", "--remux-video", "mp4", "-o", str(CACHE / f"{key}.%(ext)s"),
                    "--newline", "--no-mtime", "-N", "4",
                    "--progress-template", "download:VRBPROG %(progress._percent_str)s|%(progress._speed_str)s|%(info.format_id)s",
                    "--print", "before_dl:VRBFMT %(format_id)s|%(width)s|%(height)s|%(vcodec)s|%(acodec)s",
                    "--no-simulate", "--progress",
                    "--postprocessor-args", "Merger+ffmpeg_o:-strict unofficial",
                    "--postprocessor-args", "VideoRemuxer+ffmpeg_o:-strict unofficial"]
            if self.ffmpeg:
                args += ["--ffmpeg-location", self.ffmpeg]
            fmt_desc = await self._download(args, progress)
            mp4 = CACHE / f"{key}.mp4"
            if not mp4.is_file():
                raise ResolveError("download finished but no mp4 was made")
            parts = (fmt_desc.split(";")[0].split("|") + ["", "", ""])[:3]
            try:
                w, h = int(parts[1]), int(parts[2])
            except ValueError:
                w, h = int(info.get("width") or 0), int(info.get("height") or 0)
            return {"path": str(mp4), "w": w, "h": h, "format": fmt_desc, "key": key,
                    "fid": parts[0].split("+")[0].strip()}
        finally:
            cookie_file.unlink(missing_ok=True)
            info_file.unlink(missing_ok=True)

    async def _download(self, args: list[str], progress: ProgressCb) -> str:
        proc = await _spawn(args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        parts: list[str] = []            # format ids in download order
        descs: list[str] = []
        last_err = ""
        try:
            assert proc.stdout
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                s = line.decode("utf-8", "replace").strip()
                if s.startswith("VRBFMT "):
                    descs.append(s[7:])
                elif s.startswith("VRBPROG "):
                    pct_s, speed, fid = (s[8:].split("|") + ["", "", ""])[:3]
                    try:
                        pct = float(pct_s.strip().rstrip("%"))
                    except ValueError:
                        continue
                    if fid not in parts:
                        parts.append(fid)
                    i = parts.index(fid)
                    # video first (0-85 %), audio second (85-97 %), then the merge
                    total = pct * 0.85 if i == 0 else 85 + pct * 0.12
                    await progress(min(97, total), f"Downloading {'video' if i == 0 else 'sound'} {pct:.0f}%  {speed.strip()}")
                elif "[Merger]" in s or "[VideoRemuxer]" in s:
                    await progress(97, "Joining picture and sound...")
                elif "ERROR" in s:
                    last_err = s
            code = await proc.wait()
        finally:
            if proc.returncode is None:
                await _kill_tree(proc)          # cancelled: the ffmpeg merger child must go too
        if code != 0:
            raise ResolveError(last_err[:200] or f"yt-dlp exit code {code}")
        return "; ".join(descs)


def local_moov(path: str) -> bytes | None:
    """The moov box of a local mp4 (it can sit at the end of a merged file) for parse_projection."""
    import struct
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            o = 0
            while o + 8 <= size:
                f.seek(o)
                hdr = f.read(16)
                sz, t = struct.unpack(">I4s", hdr[:8])
                if sz == 1:
                    sz = struct.unpack(">Q", hdr[8:16])[0]
                elif sz == 0:
                    sz = size - o
                if sz < 8:
                    return None
                if t == b"moov":
                    if sz > 64 * 1024 * 1024:
                        return None
                    f.seek(o)
                    return f.read(sz)
                o += sz
    except OSError:
        return None
    return None

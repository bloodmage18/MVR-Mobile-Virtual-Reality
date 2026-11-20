"""Gallery (Home: Gallery planet): the laptop's picture and video folders, browsed from the headset.

Folders: vr_browser_test/gallery.json  {"folders": ["D:/Photos", "E:/Videos"]}  (git-ignored: your own paths);
without it, the Windows Pictures and Videos folders. The phone only ever sees names and paths *inside* these
folders (r = folder number, p = path inside it); anything outside is refused.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "gallery.json"
THUMBS = HERE / "cache" / "thumbs"
THUMB_PX = 384
IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
VIDEOS = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}
PLAIN_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}   # the phone shows these as they are
MAX_VIEW_PX = 4096                                          # bigger photos are scaled down for the phone
MAX_ITEMS = 5000
NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def roots() -> list[tuple[str, Path]]:
    """[(name, folder)] that exist: gallery.json, else the Windows Pictures / Videos folders."""
    want: list[Path] = []
    try:
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        want = [Path(os.path.expandvars(os.path.expanduser(str(f)))) for f in cfg.get("folders") or []]
    except (OSError, ValueError, AttributeError):
        pass
    if not want:
        home = Path.home()
        want = [home / "Pictures", home / "Videos", home / "OneDrive" / "Pictures"]
    out, seen = [], set()
    for f in want:
        try:
            f = f.resolve()
        except OSError:
            continue
        if f.is_dir() and f not in seen:
            seen.add(f)
            out.append((f.name or str(f), f))
    return out


def resolve(r, p) -> Path | None:
    """The file / folder for (folder number, path inside it); None if it is outside the gallery folders."""
    try:
        base = roots()[int(r)][1]
    except (ValueError, TypeError, IndexError):
        return None
    try:
        path = (base / str(p or "")).resolve()
    except OSError:
        return None
    if path != base and base not in path.parents:
        return None
    return path if path.exists() else None


def kind(path: Path) -> str | None:
    ext = path.suffix.lower()
    return "img" if ext in IMAGES else "vid" if ext in VIDEOS else None


def listing(r, p) -> dict:
    """{"title", "items": [{"n": name, "t": "root"|"dir"|"img"|"vid", "r", "p"}]}: folders first (by name),
    then pictures and videos (newest first)."""
    if r is None or str(r) in ("", "-1"):
        return {"title": "Gallery", "items": [{"n": name, "t": "root", "r": i, "p": ""}
                                              for i, (name, _) in enumerate(roots())]}
    folder = resolve(r, p)
    if folder is None or not folder.is_dir():
        return {"title": "Gallery", "items": [], "error": "folder not found"}
    base = roots()[int(r)][1]
    dirs, files = [], []
    try:
        with os.scandir(folder) as it:
            for e in it:
                if e.name.startswith((".", "$")) or e.name.lower() in ("desktop.ini", "thumbs.db"):
                    continue
                try:
                    if e.is_dir():
                        dirs.append(e.name)
                    elif kind(Path(e.name)):
                        files.append((e.stat().st_mtime, e.name))
                except OSError:
                    continue
                if len(dirs) + len(files) >= MAX_ITEMS:
                    break
    except OSError:
        return {"title": folder.name, "items": [], "error": "cannot open the folder"}
    rel = folder.relative_to(base).as_posix()
    rel = "" if rel == "." else rel
    join = (lambda n: f"{rel}/{n}") if rel else (lambda n: n)
    items = [{"n": d, "t": "dir", "r": int(r), "p": join(d)} for d in sorted(dirs, key=str.lower)]
    items += [{"n": n, "t": kind(Path(n)), "r": int(r), "p": join(n)} for _, n in sorted(files, reverse=True)]
    title = roots()[int(r)][0] + (" / " + rel if rel else "")
    return {"title": title, "items": items}


def _cache_name(path: Path, tag: str) -> Path:
    st = path.stat()
    h = hashlib.sha1(f"{path}|{st.st_mtime_ns}|{st.st_size}|{tag}".encode()).hexdigest()[:24]
    return THUMBS / f"{h}.jpg"


def thumb(path: Path, ffmpeg: str | None) -> Path | None:
    """A small JPEG preview (cached): Pillow for pictures, one ffmpeg frame for videos."""
    try:
        out = _cache_name(path, f"t{THUMB_PX}")
    except OSError:
        return None
    if out.is_file():
        return out
    THUMBS.mkdir(parents=True, exist_ok=True)
    k = kind(path)
    try:
        if k == "img":
            from PIL import Image, ImageOps
            with Image.open(path) as im:
                im = ImageOps.exif_transpose(im)
                im.thumbnail((THUMB_PX, THUMB_PX))
                im.convert("RGB").save(out, "JPEG", quality=80)
            return out
        if k == "vid" and ffmpeg:
            for at in ("3", "0"):                   # 3 s in (past a black start); very short clips: the first frame
                r = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", at, "-i", str(path),
                                    "-frames:v", "1", "-vf", f"scale={THUMB_PX}:-2", "-q:v", "4", str(out)],
                                   capture_output=True, timeout=30, creationflags=NO_WINDOW)
                if r.returncode == 0 and out.is_file() and out.stat().st_size:
                    return out
    except Exception:
        pass
    return None


def view_image(path: Path) -> Path | None:
    """The picture to show on the big screen: the file itself, or (huge, or a type the phone can't show)
    a JPEG at most MAX_VIEW_PX on its long side (cached)."""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        if path.suffix.lower() in PLAIN_IMAGES and max(w, h) <= MAX_VIEW_PX:
            return path
        out = _cache_name(path, f"v{MAX_VIEW_PX}")
        if out.is_file():
            return out
        THUMBS.mkdir(parents=True, exist_ok=True)
        from PIL import ImageOps
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((MAX_VIEW_PX, MAX_VIEW_PX))
            im.convert("RGB").save(out, "JPEG", quality=88)
        return out
    except Exception:
        return path if path.suffix.lower() in PLAIN_IMAGES else None


def video_size(path: Path, ffprobe: str | None) -> tuple[int, int]:
    if not ffprobe:
        return 0, 0
    try:
        r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "json", str(path)], capture_output=True, text=True, timeout=20,
                           creationflags=NO_WINDOW)
        s = (json.loads(r.stdout).get("streams") or [{}])[0]
        return int(s.get("width") or 0), int(s.get("height") or 0)
    except Exception:
        return 0, 0

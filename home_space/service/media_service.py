"""Home Space media service: headless media for the library (no windows on the laptop).

    python home_space/service/media_service.py [--port 8090]

Godot connects to it on 127.0.0.1. Each media kind lives in its own module and
exposes a FastAPI `router` (and optionally `async def shutdown()`):
    video.py    -> /api/videos, /ws/video        (ffmpeg decode -> raw frames, audio on the laptop)
    browser.py  -> /ws/browser, /start           (headless Edge via CDP screencast)
Shared helpers live here: library config, ffmpeg lookup.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOME = HERE.parent                      # home_space/
REPO = HOME.parent
CACHE = HERE / "cache"                  # git-ignored scratch (thumbnails, samples, browser profile)
sys.path.insert(0, str(REPO / "bridge"))
sys.path.insert(0, str(HERE))

from hmd_bridge import find_ffmpeg  # noqa: E402  (reuse the bridge's winget-aware lookup)


def find_ffplay() -> str | None:
    ff = find_ffmpeg()
    if not ff:
        return None
    p = Path(ff).with_name("ffplay.exe")
    return str(p) if p.is_file() else None


def load_library() -> dict:
    """home_space/library.json (yours, git-ignored), else library.example.json.
    Folders are lists of absolute paths per kind: {"videos": [...], "images": [...], "comics": [...]}."""
    for name in ("library.json", "library.example.json"):
        p = HOME / name
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"[media] {name} is not valid JSON: {e}")
    return {}


MODULES = ["video", "browser"]


def create_app(modules: list[str] | None = None):
    from fastapi import FastAPI
    loaded = []
    for name in modules or MODULES:
        try:
            loaded.append(importlib.import_module(name))
        except ModuleNotFoundError as e:
            if e.name != name:
                raise
            print(f"[media] module {name}.py not present yet, skipping")
        except Exception as e:  # a broken module must not take the others down
            print(f"[media] module {name}.py failed to load: {e!r}")

    @asynccontextmanager
    async def lifespan(_app):
        yield
        for m in loaded:
            if hasattr(m, "shutdown"):
                await m.shutdown()

    app = FastAPI(lifespan=lifespan)
    for m in loaded:
        app.include_router(m.router)

    @app.get("/api/health")
    def health():
        return {"ok": True, "modules": [m.__name__ for m in loaded], "ffmpeg": find_ffmpeg()}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--modules", default=",".join(MODULES), help="comma list, e.g. video")
    args = ap.parse_args()
    CACHE.mkdir(exist_ok=True)
    import uvicorn
    print(f"[media] http://127.0.0.1:{args.port}  library: {load_library()}")
    uvicorn.run(create_app(args.modules.split(",")), host="127.0.0.1", port=args.port, log_level="warning",
                ws_max_size=16 * 1024 * 1024)


if __name__ == "__main__":
    main()

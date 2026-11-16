"""VR browser test (laptop side): a headless Brave, streamed to a phone in a cardboard headset.

    vr_browser_test.bat            (or: python vr_browser_test/server.py)
    vr_browser_test/login.bat      (or: server.py --login) sign in once in a normal Brave window

The phone opens https://<laptop-ip>:8443/?k=<key> (web/index.html, a WebGL page) and gets one
WebSocket, wss://<ip>:8443/ws/vr?k=<key>. The full protocol is DESIGN_V2.md section G:

Server -> phone
    binary  one JPEG screencast frame of the ACTIVE window (1280x720)
    {"t":"info",...,"tab","focus"} {"t":"tabs","active","max","list"} {"t":"kbd","open",...}
    {"t":"pad","s":"pad,<bits>,lx,ly,rx,ry,lt,rt" | "pad,none"}       laptop Xbox controller
    {"t":"video","state":"resolving"|"progress"|"ready"|"screen"|"error"|"exit", ...}
    {"t":"toast","text"}
Phone -> server
    ptr / click / scroll / key / text / nav (back|forward|reload|home|url) / tab (new|switch|close)
    vr (enter|exit) / hello / caps / proj / vfail
Media (all need ?k=<key>): /media/<mid>/{v|a|f} (Range), /media/<mid>/{v|a}/index, /media/<mid>/mesh,
/media/<mid>.mp4 (= f).

When a video starts playing in the page, the server pauses it, looks it up with yt-dlp (kept in
memory, prefetched when a YouTube page opens) and the phone STREAMS the video and audio tracks
through /media (MSE, or two elements). Nothing is downloaded, except as a last resort. If nothing
works it sends "screen" (the live page on the cinema screen, sound on the laptop).
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import getpass
import hmac
import importlib.util
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote_plus, urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PHONE_HOST = ROOT / "phone_host"
WEB = HERE / "web"
PROFILE = HERE / "profile"
CACHE = HERE / "cache"
BRAVE = Path("C:/Program Files/BraveSoftware/Brave-Browser/Application/brave.exe")
HOME_URL = "https://www.youtube.com"
W, H = 1280, 720
MAX_FPS = 30.0
PROXY_MAX_SKIP = 48 * 1024 * 1024        # proxy: most bytes we read and drop for a site that ignores Range
MAX_TABS = 6                             # browser windows (F1)
CHUNK = 2 * 1024 * 1024                  # /media upstream: closed chunks of 2 MiB ...
PARALLEL_CHUNKS = 3                      # ... 3 in parallel per phone request ...
UPSTREAM_PER_MID = 4                     # ... at most 4 upstream connections per playback
MAX_RANGE = 16 * 1024 * 1024             # longest answer to bytes=a-b
OPEN_RANGE = 8 * 1024 * 1024             # answer to an open-ended bytes=a-
HEAD_BYTES = 262144                      # first bytes kept per track (index + 360/3D header read)
MEDIA_TTL = 6 * 3600
MEDIA_MAX = 40
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

sys.path.append(str(PHONE_HOST))         # pad_relay

try:    # module level: FastAPI reads the (string) annotations of the routes from these globals
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
except ImportError:                      # --login works without the web packages
    FastAPI = None


def _load_phone_host():
    """phone_host/server.py under its own module name (our file is also called server.py)."""
    spec = importlib.util.spec_from_file_location("phone_host_server", PHONE_HOST / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ================================================================ access key / folders
def load_token(cert_dir: Path) -> str:
    f = cert_dir / "vrb_token.txt"
    try:
        t = f.read_text(encoding="utf-8").strip()
        if len(t) >= 16:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(16)
    f.write_text(t, encoding="utf-8")
    return t


def lock_dir(path: Path) -> None:
    """Only the current Windows user may open this folder (it holds login cookies). Folder-level
    (OI)(CI) grant with inheritance removed; files inside inherit it (no /T: that breaks files)."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        return
    try:
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(OI)(CI)F", "/Q"],
                           capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        if r.returncode != 0:
            print(f"  (could not restrict {path}: {r.stdout.strip() or r.stderr.strip()})")
    except Exception as e:
        print(f"  (could not restrict {path}: {e})")


def profile_users() -> list[tuple[int, str]]:
    """Brave processes (main process only) that use OUR profile: [(pid, 'headless'|'window')]."""
    out = []
    try:
        import psutil
    except ImportError:
        return out
    prof = str(PROFILE).lower().replace("/", "\\")
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or []).lower().replace("/", "\\")
            if prof in cmd and "--type=" not in cmd:
                out.append((p.pid, "headless" if "--headless" in cmd else "window"))
        except Exception:
            pass
    return out


def profile_in_use() -> str | None:
    users = profile_users()
    if any(kind == "window" for _, kind in users):
        return "the sign-in Brave window (login.bat) is still open - close it first"
    if users:
        return "another VR browser server is still running (close its window first)"
    lock = PROFILE / "lockfile"
    if lock.exists():
        try:
            lock.unlink()
        except PermissionError:
            return "a Brave window is using vr_browser_test/profile - close it first"
        except OSError:
            pass
    return None


def port_free(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


# ================================================================ browser helpers (from home_space/service/browser.py)
def normalize_url(text: str, current: str = "") -> str:
    t = (text or "").strip()
    if not t or t == "home":
        return HOME_URL
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t) and not re.match(r"^[\w.-]+:\d+", t):
        return t
    if " " not in t and ("." in t or t.startswith("localhost") or re.match(r"^[\w.-]+:\d+", t)):
        return ("http://" if t.startswith(("localhost", "127.")) else "https://") + t
    return search_url(t, current)


def search_url(words: str, current: str = "") -> str:
    host = (urlparse(current).hostname or "").lower()
    if host == "youtube.com" or host.endswith(".youtube.com"):
        return "https://www.youtube.com/results?search_query=" + quote_plus(words)
    return "https://duckduckgo.com/?q=" + quote_plus(words)


# key -> (code, windowsVirtualKeyCode, text)
_KEYS: dict[str, tuple[str, int, str]] = {
    "Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, ""), "Tab": ("Tab", 9, ""),
    "Escape": ("Escape", 27, ""), "Delete": ("Delete", 46, ""),
    "ArrowUp": ("ArrowUp", 38, ""), "ArrowDown": ("ArrowDown", 40, ""),
    "ArrowLeft": ("ArrowLeft", 37, ""), "ArrowRight": ("ArrowRight", 39, ""),
    "Home": ("Home", 36, ""), "End": ("End", 35, ""), "PageUp": ("PageUp", 33, ""),
    "PageDown": ("PageDown", 34, ""), " ": ("Space", 32, " "), "Space": ("Space", 32, " "),
    "f": ("KeyF", 70, "f"), "k": ("KeyK", 75, "k"), "m": ("KeyM", 77, "m"),
}


def key_events(key: str) -> list[dict]:
    if key not in _KEYS:
        return []
    code, vk, text = _KEYS[key]
    if key == "Space":
        key = " "
    down = {"type": "keyDown" if text else "rawKeyDown", "key": key, "code": code,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
    if text:
        down["text"] = down["unmodifiedText"] = text
    return [down, {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}]


def kill_with_us(proc: subprocess.Popen) -> None:
    """Windows job object: the browser dies with this server even if the server is force-killed.
    (Shared with the yt-dlp/ffmpeg subprocesses, see video_resolve.add_to_job.)"""
    try:
        from video_resolve import add_to_job
        add_to_job(proc.pid)
    except Exception as e:
        log(f"[browser] job object not set ({e!r}); the browser may outlive a killed server")


# Injected into every frame: report <video> 'play' events through the __vrbPlay binding and text-field
# focus through __vrbFocus (input type only: never field values or names).
HOOK_JS = r"""
(() => {
  if (window.__vrbHooked) return;
  try { Object.defineProperty(window, '__vrbHooked', {value: true}); } catch (_) { return; }
  // The CDP bindings are globals any page script (or ad iframe) could call with forged data. Keep private
  // copies and remove the globals (this script runs before the page's own scripts). The server does not trust
  // the 'user' flag either: it uses its own record of phone input.
  let playB = null, focusB = null;
  const grab = () => {
    const nat = (f) => typeof f === 'function' && /\[native code\]/.test(Function.prototype.toString.call(f));
    if (!playB && nat(window.__vrbPlay)) { playB = window.__vrbPlay; try { delete window.__vrbPlay; } catch (_) {} }
    if (!focusB && nat(window.__vrbFocus)) { focusB = window.__vrbFocus; try { delete window.__vrbFocus; } catch (_) {} }
  };
  grab();
  let lastUser = 0;
  const mark = () => { lastUser = Date.now(); };
  addEventListener('pointerdown', mark, true);
  addEventListener('keydown', mark, true);
  const main = () => {
    let best = null, score = -1;
    for (const v of document.querySelectorAll('video')) {
      const r = v.getBoundingClientRect();
      const s = r.width * r.height + (v.paused ? 0 : 1e7);
      if (s > score) { score = s; best = v; }
    }
    return best;
  };
  const info = (v, kind) => v ? JSON.stringify({
    kind, page: location.href, top: window === window.top,
    src: String(v.currentSrc || v.src || '').slice(0, 4000),
    dur: isFinite(v.duration) ? v.duration : -1, t: v.currentTime || 0,
    w: v.videoWidth || 0, h: v.videoHeight || 0, muted: !!(v.muted || v.volume === 0),
    user: Date.now() - lastUser < 3000,
    ad: !!document.querySelector('.ad-showing, .ad-interrupting'),
    title: String(document.title || '').slice(0, 300),
    area: Math.round(v.getBoundingClientRect().width * v.getBoundingClientRect().height)
  }) : null;
  try {
    Object.defineProperty(window, '__vrbMain', {value: main});
    Object.defineProperty(window, '__vrbInfo', {value: () => info(main(), 'query')});
  } catch (_) {}
  addEventListener('play', e => {
    const v = (e.composedPath && e.composedPath()[0]) || e.target;
    if (!(v instanceof HTMLVideoElement)) return;
    grab();
    try { if (playB) playB(info(v, 'play')); } catch (_) {}
  }, true);
  const editable = (el) => !!el && (el.isContentEditable || el.tagName === 'TEXTAREA' ||
    (el.tagName === 'INPUT' && !/^(button|submit|reset|checkbox|radio|range|color|file|image|hidden)$/i.test(el.type || '')));
  const kind = (el) => el.isContentEditable ? 'contenteditable' : el.tagName === 'TEXTAREA' ? 'textarea'
                       : String(el.type || 'text').toLowerCase();
  addEventListener('focusin', (e) => {
    const el = (e.composedPath && e.composedPath()[0]) || e.target;
    if (!editable(el)) return;
    grab();
    try { focusB && focusB(JSON.stringify({kind: 'focus', input: kind(el),
          mode: String(el.inputMode || ''), enter: String(el.enterKeyHint || ''),
          user: Date.now() - lastUser < 2000})); } catch (_) {}
  }, true);
  addEventListener('focusout', (e) => {
    const el = (e.composedPath && e.composedPath()[0]) || e.target;
    if (!editable(el)) return;
    grab();
    try { focusB && focusB(JSON.stringify({kind: 'blur'})); } catch (_) {}
  }, true);
})();
"""
PAUSE_JS = "document.querySelectorAll('video').forEach(v => { try { v.pause(); } catch (_) {} }); true"


# ================================================================ CDP connection
class CDP:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.handlers: dict[str, object] = {}            # sessionId -> fn(sid, method, params)
        self.on_browser_event = None                     # fn(method, params)

    @property
    def alive(self) -> bool:
        return (self.proc is not None and self.proc.poll() is None and self.ws is not None
                and self.reader is not None and not self.reader.done())

    async def start(self, zoom: float = 1.0) -> None:
        from websockets.asyncio.client import connect
        if not BRAVE.is_file():
            raise RuntimeError(f"Brave not found at {BRAVE}")
        port = free_port()
        port_file = PROFILE / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        args = [str(BRAVE), "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={PROFILE}",
                f"--window-size={W},{H}", f"--force-device-scale-factor={zoom:g}", "--no-first-run", "--no-default-browser-check", "--disable-default-apps",
                "--autoplay-policy=no-user-gesture-required", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                "--hide-crash-restore-bubble", "about:blank"]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     creationflags=NO_WINDOW)
        kill_with_us(self.proc)
        log(f"[browser] started headless Brave pid {self.proc.pid} (DevTools on 127.0.0.1:{port})")
        deadline = time.monotonic() + 25
        url = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"Brave exited with code {self.proc.returncode} (profile in use?)")
            url = await asyncio.to_thread(_devtools_url, port)     # Brave writes no DevToolsActivePort
            if url:
                break
            await asyncio.sleep(0.2)
        if not url:
            raise RuntimeError("Brave did not open its DevTools port")
        self.ws = await connect(url, max_size=None, ping_interval=None, open_timeout=10)
        self.reader = asyncio.create_task(self._read())

    async def call(self, method: str, params: dict | None = None, session: str | None = None,
                   timeout: float = 30):
        if self.ws is None:
            raise RuntimeError("browser not running")
        self.next_id += 1
        mid = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        msg = {"id": mid, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        try:
            await self.ws.send(json.dumps(msg))
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(mid, None)

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self.pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(str(msg["error"].get("message"))))
                        else:
                            fut.set_result(msg.get("result", {}))
                    continue
                method, params = msg.get("method", ""), msg.get("params", {})
                sid = msg.get("sessionId")
                try:
                    if sid:
                        h = self.handlers.get(sid)
                        if h:
                            h(sid, method, params)
                    elif self.on_browser_event:
                        self.on_browser_event(method, params)
                except Exception as e:
                    log(f"[browser] event {method} handler error: {e!r}")
        except Exception as e:
            log(f"[browser] CDP connection closed: {e!r}")
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(RuntimeError("CDP connection lost"))
            self.pending.clear()

    async def stop(self) -> None:
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.call("Browser.close"), 3)
            with contextlib.suppress(Exception):
                await self.ws.close()
        if self.reader:
            self.reader.cancel()
        self.ws = self.reader = None
        self.handlers.clear()
        if self.proc is not None:
            proc, self.proc = self.proc, None
            await asyncio.to_thread(self._reap, proc)

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            pass
        if proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)


# ================================================================ phone connections
class Client:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.lock = asyncio.Lock()
        self.frame: bytes | None = None
        self.ev = asyncio.Event()
        self.alive = True
        self.ip = ws.client.host if ws.client else "?"
        self.frames = 0
        self.caps: dict | None = None             # G2 "caps" (sanitized)
        self.caps_t = 0.0

    async def text(self, obj: dict) -> None:
        if not self.alive:
            return
        try:
            async with self.lock:
                await asyncio.wait_for(self.ws.send_text(json.dumps(obj)), 10)
        except Exception:
            self.alive = False
            self.ev.set()

    def push_frame(self, data: bytes) -> None:
        self.frame = data                     # newest wins: a slow phone never builds a backlog
        self.ev.set()

    async def frame_loop(self) -> None:
        while self.alive:
            await self.ev.wait()
            self.ev.clear()
            data, self.frame = self.frame, None
            if data is None or not self.alive:
                continue
            try:
                async with self.lock:
                    await asyncio.wait_for(self.ws.send_bytes(data), 10)
                self.frames += 1
            except Exception:
                self.alive = False

    async def pad_loop(self) -> None:
        import pad_relay
        seen, last = -1, 0.0
        while self.alive:
            ver, msg = pad_relay.relay.snapshot()
            now = time.monotonic()
            if ver != seen or now - last >= 0.5:
                await self.text({"t": "pad", "s": msg})
                seen, last = ver, now
            await asyncio.sleep(1 / 60)


# ================================================================ windows (tabs), DESIGN_V2 F
class Tab:
    def __init__(self, tid: str, target: str) -> None:
        self.id = tid                    # "1", "2", ... never reused in one server run
        self.target = target             # CDP targetId
        self.session = ""                # flat sessionId
        self.url = ""
        self.title = ""
        self.loading = False
        self.can_back = False
        self.focus: dict | None = None   # {"input","enter"} while a text field has focus
        self.focus_seq = 0
        self.kbd_click = -1.0            # last_click value that the last kbd open was sent for
        self.children: dict[str, str] = {}   # iframe sessionId -> url
        self.closing = False             # we asked Brave to close it


FOCUS_INPUTS = ("text", "search", "email", "url", "password", "tel", "number", "textarea", "contenteditable")
FOCUS_ENTER = ("search", "go", "send", "next", "done", "enter")
YT_COOKIE_URLS = ["https://www.youtube.com/", "https://m.youtube.com/", "https://music.youtube.com/"]
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _is_watch_url(url: str) -> bool:
    """A YouTube /watch, /shorts/<id> or youtu.be/<id> page (prefetch trigger, A4)."""
    from video_resolve import youtube_id
    if not youtube_id(url):
        return False
    u = urlparse(url)
    host = (u.hostname or "").lower()
    return host.endswith("youtu.be") or u.path == "/watch" or u.path.startswith("/shorts/")


# ================================================================ the browser + VR state
_beat = [0.0]


def _stack_sampler(main_id: int) -> None:
    """VRB_LAGTRACE=1: print where the event loop thread is while it is blocked (diagnostic)."""
    import traceback
    seen: dict[str, int] = {}
    while True:
        time.sleep(0.2)
        if _beat[0] and time.monotonic() - _beat[0] > 0.4:
            f = sys._current_frames().get(main_id)
            if f is not None:
                st = "".join(traceback.format_stack(f)[-6:])
                seen[st] = seen.get(st, 0) + 1
                if seen[st] in (1, 5, 20):
                    print(f"[lagtrace x{seen[st]}]\n{st}", flush=True)


async def _loop_lag_monitor() -> None:
    """Diagnostic: log when the event loop was blocked (everything, including yt-dlp pipes, stalls then)."""
    if os.environ.get("VRB_LAGTRACE"):
        import threading
        threading.Thread(target=_stack_sampler, args=(threading.get_ident(),), daemon=True).start()
    while True:
        _beat[0] = time.monotonic()
        t0 = time.monotonic()
        await asyncio.sleep(0.25)
        lag = time.monotonic() - t0 - 0.25
        if lag > 1.0:
            log(f"[loop] event loop was blocked for {lag:.1f}s")


class VRBrowser:
    def __init__(self, token: str, zoom: float) -> None:
        from video_resolve import Resolver
        self.token = token
        self.zoom = zoom
        self.cdp = CDP()
        self.resolver = Resolver()
        self.clients: set[Client] = set()
        self.media: dict[str, dict] = {}               # mid -> {"v","a"} | {"f"} (+ "_t", "_mesh", ...)
        self.tabs: dict[str, Tab] = {}                 # insertion order = strip order
        self.active = ""
        self.sess_tab: dict[str, Tab] = {}             # page AND iframe sessions -> their tab
        self.frame_tab: dict[str, Tab] = {}            # iframe targetId -> its tab (popup openers)
        self.next_tab = 1
        self._none = Tab("", "")                       # stands in while no tab exists
        self.sent_info: dict | None = None
        self.sent_tabs: dict | None = None
        self.casting = False
        self.slot: dict | None = None
        self.pump: asyncio.Task | None = None
        self.last_frame_t = 0.0
        self.last_jpeg: bytes | None = None
        self.ua = ""
        self.mode = "browse"                          # browse | resolving | vr | screen
        self.video_msg: dict | None = None            # last "ready"/"screen" message (for reconnects)
        self.video_session = ""
        self.resolve_task: asyncio.Task | None = None
        self.resolve_key = ""                         # which video the running resolve is for
        self.resolve_top = ""                         # top page URL when it started
        self.resolve_title = ""
        self.suppress_until = 0.0
        self.last_click = 0.0                         # last phone click / key / text (monotonic)
        self.adopted: set[str] = set()                # popup targets already handled
        self.issued_vids: set[str] = set()            # "vid" keys sent in "ready" (G2 proj check)
        self.cur_play: dict | None = None             # what the current "ready" was built from (vfail rung)
        self.lock = asyncio.Lock()

    # ---------------------------------------------------------------- active-tab shortcuts (v1 names)
    @property
    def tab(self) -> Tab:
        return self.tabs.get(self.active) or self._none

    @property
    def target(self) -> str:
        return self.tab.target

    @property
    def session(self) -> str:
        return self.tab.session

    @property
    def children(self) -> dict[str, str]:
        return self.tab.children

    @property
    def url(self) -> str:
        return self.tab.url

    @url.setter
    def url(self, v: str) -> None:
        self.tab.url = v

    @property
    def title(self) -> str:
        return self.tab.title

    @title.setter
    def title(self, v: str) -> None:
        self.tab.title = v

    @property
    def loading(self) -> bool:
        return self.tab.loading

    @property
    def can_back(self) -> bool:
        return self.tab.can_back

    # ---------------------------------------------------------------- browser life
    async def start(self) -> None:
        if not getattr(self, "_lag_task", None):
            self._lag_task = asyncio.create_task(_loop_lag_monitor())
        async with self.lock:
            await self._start()

    async def _start(self) -> None:
        last = self.url if self.url.startswith("http") else ""
        await self.cdp.stop()
        self.tabs.clear()
        self.sess_tab.clear()
        self.frame_tab.clear()
        self.active = ""
        self.casting = False
        self.sent_tabs = None
        self.cdp.on_browser_event = self._browser_event
        await self.cdp.start(max(0.5, min(3.0, self.zoom)))   # screencast frames are then 1280x720 real pixels
        await self.cdp.call("Target.setDiscoverTargets", {"discover": True})
        ver = await self.cdp.call("Browser.getVersion")
        self.ua = str(ver.get("userAgent", "")).replace("HeadlessChrome", "Chrome")
        tab = await self._open_tab("about:blank", activate=False)
        # the first about:blank page Brave opens by itself is not needed
        with contextlib.suppress(Exception):
            for t in (await self.cdp.call("Target.getTargets"))["targetInfos"]:
                if t.get("type") == "page" and t["targetId"] != tab.target:
                    self.adopted.add(t["targetId"])
                    await self.cdp.call("Target.closeTarget", {"targetId": t["targetId"]})
        await self._activate(tab)
        self.go(last or HOME_URL)

    async def _open_tab(self, url: str, target_id: str | None = None, activate: bool = True) -> Tab:
        """F2: a new window (target_id None) or an adopted popup target, with the v1 per-page setup."""
        created = target_id is None
        if created:
            r = await self.cdp.call("Target.createTarget", {"url": "about:blank", "newWindow": True,
                                                            "width": W, "height": H})
            target_id = r["targetId"]
        self.adopted.add(target_id)
        tab = Tab(str(self.next_tab), target_id)
        self.next_tab += 1
        tab.url = url or "about:blank"
        self.tabs[tab.id] = tab
        try:
            r = await self.cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            s = tab.session = r["sessionId"]
            self.sess_tab[s] = tab
            self.cdp.handlers[s] = self._event
            await self.cdp.call("Page.enable", session=s)
            await self.cdp.call("Network.enable", session=s)
            await self.cdp.call("Network.setUserAgentOverride", {"userAgent": self.ua}, session=s)
            await self.cdp.call("Emulation.setFocusEmulationEnabled", {"enabled": True}, session=s)
            z = max(0.5, min(3.0, self.zoom))
            await self.cdp.call("Emulation.setDeviceMetricsOverride", {
                "width": round(W / z), "height": round(H / z), "deviceScaleFactor": z, "mobile": False}, session=s)
            await self._hook(s)
            await self.cdp.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True,
                                                         "flatten": True}, session=s)
        except Exception:
            self.tabs.pop(tab.id, None)
            if tab.session:
                self.sess_tab.pop(tab.session, None)
                self.cdp.handlers.pop(tab.session, None)
            with contextlib.suppress(Exception):
                await self.cdp.call("Target.closeTarget", {"targetId": target_id})
            raise
        if created and url and url != "about:blank":          # an adopted popup navigates by itself
            r = await self._safe("Page.navigate", {"url": url}, session=tab.session)
            if r and r.get("errorText"):
                log(f"[browser] navigate {url}: {r['errorText']}")
        with contextlib.suppress(Exception):
            info = (await self.cdp.call("Target.getTargetInfo", {"targetId": target_id}, timeout=5))["targetInfo"]
            tab.url = info.get("url") or tab.url
            tab.title = info.get("title") or ""
        if activate:
            await self._activate(tab)
        self._send_tabs()
        log(f"[tabs] window {tab.id} opened ({len(self.tabs)} open)")
        return tab

    async def new_tab(self) -> Tab | None:
        """{"t":"tab","action":"new"} / typing window: a new about:blank window, activated."""
        if len(self.tabs) >= MAX_TABS:
            await self.toast(f"Up to {MAX_TABS} windows: close one first")
            return None
        return await self._open_tab("about:blank")

    async def _activate(self, tab: Tab) -> None:
        """F4: make tab the one the phone sees and controls."""
        if tab.id not in self.tabs:
            return
        if self.mode in ("vr", "screen"):
            await self.leave_video(None)
        elif self.mode == "resolving":
            self._cancel_resolve("switched to another window")
        old = self.tabs.get(self.active)
        was_casting = self.casting
        self.active = tab.id
        self.casting = False
        self.slot = None
        self.last_jpeg = None
        self.sent_info = None
        self._info()                                   # the strip and address bar update at once
        self._send_tabs()
        asyncio.create_task(self.broadcast({"t": "kbd", "open": False}))
        if old is not None and old is not tab:
            asyncio.create_task(self._safe("Runtime.evaluate", {"expression": PAUSE_JS}, session=old.session,
                                           timeout=5))     # no hidden sound from the window we left
            if was_casting:
                asyncio.create_task(self._safe("Page.stopScreencast", session=old.session))
        with contextlib.suppress(Exception):
            await self.cdp.call("Target.activateTarget", {"targetId": tab.target}, timeout=5)
        await self._safe("Page.bringToFront", session=tab.session, timeout=5)
        await self.update_cast()
        await self._history(tab)

    async def switch_tab(self, tid: str) -> None:
        tab = self.tabs.get(tid)
        if tab is not None and tid != self.active:
            await self._activate(tab)
            log(f"[tabs] switched to window {tid}")

    async def close_tab(self, tid: str | None) -> None:
        tab = self.tabs.get(tid or self.active)
        if tab is None:
            return
        if self.mode != "browse" and self._owns_video(tab):
            await self.leave_video(None)
        tab.closing = True
        with contextlib.suppress(Exception):
            await self.cdp.call("Target.closeTarget", {"targetId": tab.target}, timeout=5)
        await self._remove_tab(tab)

    def _owns_video(self, tab: Tab) -> bool:
        return bool(self.video_session) and (self.video_session == tab.session or self.video_session in tab.children)

    async def _remove_tab(self, tab: Tab) -> None:
        if self.tabs.get(tab.id) is not tab:
            return
        order = list(self.tabs)
        i = order.index(tab.id)
        was_active = tab.id == self.active
        del self.tabs[tab.id]
        for s in [tab.session, *tab.children]:
            self.sess_tab.pop(s, None)
            self.cdp.handlers.pop(s, None)
        for k in [k for k, t in self.frame_tab.items() if t is tab]:
            self.frame_tab.pop(k, None)
        if self.mode != "browse" and self._owns_video(tab):
            await self.leave_video(None)
        log(f"[tabs] window {tab.id} closed ({len(self.tabs)} left)")
        if not self.tabs:
            self.active = ""
            self.casting = False
            await self._open_tab(HOME_URL)
        elif was_active:
            self.active = ""
            self.casting = False                       # its screencast went with it
            nxt = order[i + 1] if i + 1 < len(order) else order[i - 1]
            await self._activate(self.tabs[nxt])
        self._send_tabs()

    async def _hook(self, s: str) -> None:
        await self.cdp.call("Runtime.enable", session=s)         # bindings survive navigations only with this
        await self.cdp.call("Runtime.addBinding", {"name": "__vrbPlay"}, session=s)
        await self.cdp.call("Runtime.addBinding", {"name": "__vrbFocus"}, session=s)
        try:
            await self.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_JS, "runImmediately": True},
                                session=s)
        except Exception:
            await self.cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": HOOK_JS}, session=s)
        with contextlib.suppress(Exception):
            await self.cdp.call("Runtime.evaluate", {"expression": HOOK_JS}, session=s, timeout=5)

    async def watchdog(self) -> None:
        """Restart Brave if it crashes; poll titles/URLs of every window (scripts change them without an event)."""
        while True:
            await asyncio.sleep(1.0)
            if not self.cdp.alive:
                log("[browser] Brave is not running any more; restarting it (windows are reopened as one)")
                try:
                    await self.start()
                except Exception as e:
                    log(f"[browser] restart failed: {e!r}")
                    await asyncio.sleep(5)
                continue
            with contextlib.suppress(Exception):
                infos = (await self.cdp.call("Target.getTargets", timeout=5))["targetInfos"]
                by_target = {t.target: t for t in self.tabs.values()}
                for ti in infos:
                    t = by_target.get(ti.get("targetId"))
                    if t is not None:
                        t.url, t.title = ti.get("url", t.url), ti.get("title", t.title)
                self._info()
                self._send_tabs()

    async def shutdown(self) -> None:
        if self.resolve_task:
            self.resolve_task.cancel()
        await self.cdp.stop()

    # ---------------------------------------------------------------- CDP events
    def _browser_event(self, method: str, p: dict) -> None:
        if method in ("Target.targetCreated", "Target.targetInfoChanged"):
            info = p.get("targetInfo", {})
            tid = info.get("targetId", "")
            if info.get("type") != "page" or tid in self.adopted:
                return
            opener = next((t for t in self.tabs.values() if t.target == info.get("openerId")), None)                 or self.frame_tab.get(info.get("openerId", ""))
            if opener is not None and opener.id not in self.tabs:
                opener = None
            if opener is None:
                return
            self.adopted.add(tid)
            asyncio.create_task(self._popup(tid, str(info.get("url") or ""), opener))
        elif method == "Target.targetDestroyed":
            tab = next((t for t in self.tabs.values() if t.target == p.get("targetId")), None)
            if tab is not None:
                if not tab.closing:
                    log(f"[tabs] window {tab.id} closed by the page")
                asyncio.create_task(self._remove_tab(tab))
        elif method == "Target.detachedFromTarget":
            tab = self.sess_tab.get(p.get("sessionId", ""))
            if tab is not None and tab.session == p.get("sessionId"):
                asyncio.create_task(self._remove_tab(tab))

    async def _popup(self, tid: str, url: str, opener: Tab) -> None:
        """F3: target=_blank / window.open. Clicked -> a new window; unrequested -> closed; at the cap ->
        loaded in the opener (v1)."""
        if time.monotonic() - self.last_click > 2:
            with contextlib.suppress(Exception):
                await self.cdp.call("Target.closeTarget", {"targetId": tid})
            log(f"[tabs] closed a popup nobody asked for ({url[:80] or 'about:blank'})")
            return
        if len(self.tabs) < MAX_TABS:
            try:
                await self._open_tab(url or "about:blank", target_id=tid)
            except Exception as e:
                log(f"[tabs] could not adopt the popup: {e!r}")
                return
            await self.toast("Opened in a new window (LT/RT to switch)")
            return
        # window limit: load it in the opener (wait a little for a popup that is still about:blank)
        for _ in range(25):
            if url and url != "about:blank":
                break
            await asyncio.sleep(0.2)
            with contextlib.suppress(Exception):
                url = (await self.cdp.call("Target.getTargetInfo", {"targetId": tid}, timeout=5))["targetInfo"].get("url", "")
        with contextlib.suppress(Exception):
            await self.cdp.call("Target.closeTarget", {"targetId": tid})
        if url and url != "about:blank" and opener.id in self.tabs:
            await self._safe("Page.navigate", {"url": url}, session=opener.session)
            await self.toast(f"Window limit ({MAX_TABS}) reached: opened here")

    def _event(self, sid: str, method: str, p: dict) -> None:
        if method == "Runtime.bindingCalled":
            if p.get("name") == "__vrbPlay":
                self._on_play(sid, p.get("payload", ""))
            elif p.get("name") == "__vrbFocus":
                self._on_focus(sid, p.get("payload", ""))
            return
        if method == "Target.attachedToTarget":
            asyncio.create_task(self._child(sid, p))
            return
        if method == "Target.detachedFromTarget":
            child = p.get("sessionId", "")
            t = self.sess_tab.pop(child, None)
            if t is not None:
                t.children.pop(child, None)
            self.cdp.handlers.pop(child, None)
            return
        tab = self.sess_tab.get(sid)
        if tab is None or sid != tab.session:
            if method == "Page.screencastFrame":
                asyncio.create_task(self._ack({**p, "_sid": sid}))
            return
        active = tab.id == self.active
        if method == "Page.screencastFrame":
            if active:
                self._on_frame({**p, "_sid": sid})
            else:
                asyncio.create_task(self._ack({**p, "_sid": sid}))
        elif method == "Page.frameNavigated":
            fr = p.get("frame", {})
            if not fr.get("parentId"):
                tab.url = fr.get("url", tab.url)
                if tab.focus is not None:              # the focused field went with the old document
                    tab.focus = None
                    tab.focus_seq += 1
                    if active:
                        asyncio.create_task(self.broadcast({"t": "kbd", "open": False}))
                self._page_moved(tab)
        elif method == "Page.navigatedWithinDocument":
            if p.get("frameId") == tab.target:
                tab.url = p.get("url", tab.url)
                self._page_moved(tab)
        elif method == "Page.frameStartedLoading" and p.get("frameId") == tab.target:
            tab.loading = True
            if active:
                self._info()
            self._send_tabs()
        elif method == "Page.frameStoppedLoading" and p.get("frameId") == tab.target:
            tab.loading = False
            if active:
                self._info()
                if self.want_cast and not self.casting:
                    asyncio.create_task(self.set_cast(True))
            self._send_tabs()
        elif method == "Page.javascriptDialogOpening":
            asyncio.create_task(self._safe("Page.handleJavaScriptDialog", {"accept": True}, session=sid))

    def _page_moved(self, tab: Tab) -> None:
        if tab.id == self.active:
            self._info()
            asyncio.create_task(self._history(tab))
            self._maybe_cancel_resolve()
        self._send_tabs()
        if _is_watch_url(tab.url) and self.resolver.want_prefetch(tab.url):
            asyncio.create_task(self.resolver.prefetch(tab.url, lambda: self._cookies(YT_COOKIE_URLS)))

    def _maybe_cancel_resolve(self) -> None:
        """The page moved on (YouTube SPA click, link, history) while a video was being fetched for
        the old one: drop it instead of jumping into VR with a video the user left."""
        if self.mode != "resolving":
            return
        from video_resolve import youtube_id
        old_vid, new_vid = youtube_id(self.resolve_top), youtube_id(self.url)
        if old_vid or new_vid:
            moved = old_vid != new_vid
        else:
            moved = self.url.split("#")[0] != self.resolve_top.split("#")[0]
        if moved:
            self._cancel_resolve(f"page changed to {self.url[:80]}")

    def _cancel_resolve(self, why: str) -> None:
        log(f"[video] {why} while getting a video ready: cancelled")
        # synchronously, so a play event for the NEW video right after this starts a fresh resolve
        if self.resolve_task and not self.resolve_task.done():
            self.resolve_task.cancel()
        self.mode = "browse"
        self.video_msg = None
        asyncio.create_task(self.broadcast({"t": "video", "state": "exit"}))
        asyncio.create_task(self.update_cast())

    async def _child(self, parent: str, p: dict) -> None:
        """Out-of-process iframes (embedded players) get the same play and focus hooks."""
        child = p.get("sessionId", "")
        info = p.get("targetInfo", {})
        tab = self.sess_tab.get(parent)
        self.cdp.handlers[child] = self._event
        try:
            if info.get("type") == "iframe" and tab is not None:
                tab.children[child] = info.get("url", "")
                self.sess_tab[child] = tab
                if info.get("targetId"):
                    self.frame_tab[info["targetId"]] = tab
                await self._hook(child)
                await self.cdp.call("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True,
                                                             "flatten": True}, session=child)
        except Exception:
            pass
        finally:
            if p.get("waitingForDebugger"):
                with contextlib.suppress(Exception):
                    await self.cdp.call("Runtime.runIfWaitingForDebugger", session=child)
            if info.get("type") != "iframe" or tab is None:
                with contextlib.suppress(Exception):
                    await self.cdp.call("Target.detachFromTarget", {"sessionId": child})
                self.cdp.handlers.pop(child, None)

    async def _history(self, tab: Tab | None = None) -> None:
        tab = tab or self.tab
        try:      # fails quietly while a navigation swaps the renderer process
            h = await self.cdp.call("Page.getNavigationHistory", session=tab.session, timeout=5)
        except Exception:
            return
        if h:
            tab.can_back = h.get("currentIndex", 0) > 0
            if tab.id == self.active:
                self._info()

    async def _safe(self, method: str, params: dict | None = None, session: str | None = None, timeout: float = 30):
        try:
            return await self.cdp.call(method, params, session or self.session, timeout=timeout)
        except Exception as e:
            log(f"[browser] {method} failed: {e!r}"[:200])
            return None

    # ---------------------------------------------------------------- focus -> virtual keyboard (D6)
    def _on_focus(self, sid: str, payload: str) -> None:
        if not isinstance(payload, str) or len(payload) > 2000:
            return
        try:
            d = json.loads(payload)
        except ValueError:
            return
        tab = self.sess_tab.get(sid)
        if not isinstance(d, dict) or tab is None:
            return
        active = tab.id == self.active
        if d.get("kind") == "focus":
            inp = str(d.get("input") or "text").lower()
            enter = str(d.get("enter") or "").lower()
            tab.focus = {"input": inp if inp in FOCUS_INPUTS else "text", "enter": enter if enter in FOCUS_ENTER else ""}
            tab.focus_seq += 1
            if active:
                self._info()
                # only right after real phone input (the page's own 'user' flag is not trusted), and one open
                # per phone click/key (a page cannot keep re-opening the keyboard)
                if time.monotonic() - self.last_click < 2.0 and tab.kbd_click != self.last_click:
                    tab.kbd_click = self.last_click
                    asyncio.create_task(self.broadcast({"t": "kbd", "open": True, "mode": "field", **tab.focus}))
        elif d.get("kind") == "blur":
            asyncio.create_task(self._blur_later(tab, tab.focus_seq))

    async def _blur_later(self, tab: Tab, seq: int) -> None:
        await asyncio.sleep(0.15)
        if tab.focus_seq != seq or tab.focus is None:
            return                                     # a new field got focus meanwhile
        tab.focus = None
        if tab.id == self.active and tab.id in self.tabs:
            self._info()
            await asyncio.sleep(0)                     # info (focus null) goes out first
            await self.broadcast({"t": "kbd", "open": False})

    # ---------------------------------------------------------------- info / broadcast
    def _info(self) -> None:
        t = self.tab
        msg = {"t": "info", "url": t.url, "title": t.title or t.url, "w": W, "h": H,
               "loading": t.loading, "canBack": t.can_back, "tab": self.active, "focus": t.focus}
        if msg != self.sent_info:
            self.sent_info = msg
            asyncio.create_task(self.broadcast(msg))

    def _tabs_msg(self) -> dict:
        return {"t": "tabs", "active": self.active, "max": MAX_TABS,
                "list": [{"id": t.id, "title": ("New window" if t.url in ("", "about:blank") else t.title or t.url)[:200],
                          "url": t.url[:2000], "loading": t.loading} for t in self.tabs.values() if t.session]}

    def _send_tabs(self) -> None:
        msg = self._tabs_msg()
        if msg != self.sent_tabs:
            self.sent_tabs = msg
            asyncio.create_task(self.broadcast(msg))

    async def broadcast(self, msg: dict) -> None:
        for c in list(self.clients):
            await c.text(msg)

    async def toast(self, text: str) -> None:
        log(f"[toast] {text}")
        await self.broadcast({"t": "toast", "text": text})

    # ---------------------------------------------------------------- screencast (active window only)
    @property
    def want_cast(self) -> bool:
        return bool(self.clients) and self.mode != "vr" and bool(self.session)

    async def set_cast(self, on: bool) -> None:
        if on and not self.casting:
            self.casting = True
            try:
                await self.cdp.call("Page.startScreencast", {"format": "jpeg", "quality": 80, "maxWidth": W,
                                                             "maxHeight": H, "everyNthFrame": 1}, self.session)
            except Exception as e:
                self.casting = False                    # mid-navigation: retried when loading stops
                log(f"[browser] screencast deferred: {e}")
        elif not on and self.casting:
            self.casting = False
            await self._safe("Page.stopScreencast")

    async def update_cast(self) -> None:
        await self.set_cast(self.want_cast)

    def _on_frame(self, params: dict) -> None:
        old, self.slot = self.slot, params
        if old is not None:
            asyncio.create_task(self._ack(old))
        if self.pump is None or self.pump.done():
            self.pump = asyncio.create_task(self._pump())

    async def _ack(self, params: dict) -> None:
        with contextlib.suppress(Exception):
            await self.cdp.call("Page.screencastFrameAck", {"sessionId": params["sessionId"]},
                                params.get("_sid") or self.session)

    async def _pump(self) -> None:
        import base64
        while self.slot is not None:
            wait = 1.0 / MAX_FPS - (time.monotonic() - self.last_frame_t)
            if wait > 0:
                await asyncio.sleep(wait)
            params, self.slot = self.slot, None
            if params is None:
                break
            self.last_frame_t = time.monotonic()
            if self.casting and params.get("_sid") == self.session:
                data = base64.b64decode(params["data"])
                self.last_jpeg = data
                for c in list(self.clients):
                    c.push_frame(data)
            await self._ack(params)

    # ---------------------------------------------------------------- input
    def _xy(self, m: dict) -> tuple[float, float]:
        z = max(0.5, min(3.0, self.zoom))
        try:
            x = min(1.0, max(0.0, float(m.get("x", 0.5))))
            y = min(1.0, max(0.0, float(m.get("y", 0.5))))
        except (TypeError, ValueError):
            x = y = 0.5
        return x * W / z, y * H / z

    def go(self, text: str) -> None:
        url = normalize_url(text, self.url)
        asyncio.create_task(self._navigate(url, self.session))

    async def _navigate(self, url: str, session: str | None = None) -> None:
        if self.mode != "browse":
            await self.leave_video(None)
        r = await self._safe("Page.navigate", {"url": url}, session=session)
        if r and r.get("errorText"):
            log(f"[browser] navigate {url}: {r['errorText']}")

    async def nav(self, to: str) -> None:
        if to in ("back", "forward", "reload") and self.mode != "browse":
            await self.leave_video(None)              # also cancels a running resolve
        if to in ("back", "forward"):
            h = await self._safe("Page.getNavigationHistory")
            if h:
                i = h["currentIndex"] + (-1 if to == "back" else 1)
                if 0 <= i < len(h["entries"]):
                    await self._safe("Page.navigateToHistoryEntry", {"entryId": h["entries"][i]["id"]})
        elif to == "reload":
            await self._safe("Page.reload")
        elif to == "home":
            self.go(HOME_URL)

    async def type_text(self, text: str, enter: bool = True) -> None:
        await self._safe("Input.insertText", {"text": text})
        if enter:
            for e in key_events("Enter"):
                await self._safe("Input.dispatchKeyEvent", e)

    def caps(self) -> dict:
        """Caps of the phone that most recently sent them and is still connected (A5 default otherwise)."""
        from video_resolve import DEFAULT_CAPS
        best = max((c for c in self.clients if c.caps and c.alive), key=lambda c: c.caps_t, default=None)
        return best.caps if best else DEFAULT_CAPS

    async def handle(self, client: Client, m: dict) -> None:
        t = m.get("t")
        if t == "ptr":
            x, y = self._xy(m)
            await self._safe("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        elif t == "click":
            x, y = self._xy(m)
            btn = "right" if m.get("button") == "right" else "left"
            self.last_click = time.monotonic()
            await self._safe("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            for typ in ("mousePressed", "mouseReleased"):
                await self._safe("Input.dispatchMouseEvent", {"type": typ, "x": x, "y": y, "button": btn,
                                                              "buttons": 1 if typ == "mousePressed" else 0,
                                                              "clickCount": 1})
        elif t == "scroll":
            x, y = self._xy(m)
            try:
                dy = max(-3000.0, min(3000.0, float(m.get("dy", 0))))
            except (TypeError, ValueError):
                return
            await self._safe("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                                          "deltaX": 0, "deltaY": dy})
        elif t == "key":
            key = m.get("key")
            if isinstance(key, str) and len(key) <= 20:
                self.last_click = time.monotonic()
                for e in key_events(key):
                    await self._safe("Input.dispatchKeyEvent", e)
        elif t == "text":
            s = m.get("s")
            if isinstance(s, str):
                s = _CTRL.sub("", s[:2000])
                if s:
                    self.last_click = time.monotonic()
                    await self._safe("Input.insertText", {"text": s})
        elif t == "nav":
            to = m.get("to")
            if to == "url":
                u = m.get("url")
                if isinstance(u, str) and u.strip() and len(u) <= 4000:
                    self.go(u)
            elif to in ("back", "forward", "reload", "home"):
                asyncio.create_task(self.nav(to))
        elif t == "tab":                               # F5: inline, so a following nav goes to the new tab
            act, tid = m.get("action"), m.get("id")
            tid = tid if isinstance(tid, str) and len(tid) <= 10 else None
            if act == "new":
                await self.new_tab()
            elif act == "switch" and tid:
                await self.switch_tab(tid)
            elif act == "close":
                await self.close_tab(tid)
        elif t == "vr":
            if m.get("action") == "enter":
                await self.manual_trigger()
            elif m.get("action") == "exit":
                await self.leave_video(m.get("time"), origin=client)
        elif t == "hello":
            def num(k, nd=0):
                try:
                    return round(float(m.get(k)), nd) if nd else int(float(m.get(k)))
                except (TypeError, ValueError):
                    return "?"
            eye = m.get("eye") if isinstance(m.get("eye"), list) and len(m.get("eye")) == 2 else ["?", "?"]
            with contextlib.suppress(Exception):
                eye = [int(float(eye[0])), int(float(eye[1]))]
            log(f"[phone] {client.ip} canvas {num('w')}x{num('h')} dpr {num('dpr', 2)} eye {eye[0]}x{eye[1]} "
                f"fov {num('fov', 1)} ppd {num('ppd', 1)}{' stereo' if m.get('stereo') else ''}")
        elif t == "caps":
            from video_resolve import sanitize_caps
            caps = sanitize_caps(m)
            if caps:
                client.caps, client.caps_t = caps, time.monotonic()
                log(f"[phone] {client.ip} caps mse={caps['mse']} mtex={caps['mtex']} "
                    + " ".join(f"{k}:{v.get('mse') or v.get('file')}" for k, v in caps["v"].items())
                    + f" aac={caps['a']['aac']} opus={caps['a']['opus']}")
        elif t == "proj":
            await self._on_proj(m)
        elif t == "vfail":
            await self._on_vfail(m)

    async def _on_proj(self, m: dict) -> None:
        from video_resolve import PROJECTIONS, STEREO_OF, write_meta
        vid, proj, src = m.get("vid"), m.get("projection"), m.get("source")
        if not isinstance(vid, str) or vid not in self.issued_vids or proj not in PROJECTIONS \
                or src not in ("user", "image"):
            return
        scores = m.get("scores") if isinstance(m.get("scores"), dict) else None
        if scores is not None:
            scores = {str(k)[:20]: v for k, v in list(scores.items())[:20]
                      if isinstance(v, (int, float)) and not isinstance(v, bool)}
        if src == "user":
            await asyncio.to_thread(write_meta, vid, user_projection=proj, projection=proj,
                                    projection_source="user", stereo=STEREO_OF.get(proj))
        else:
            from video_resolve import read_meta
            saved = await asyncio.to_thread(read_meta, vid) or {}
            if saved.get("v") == 2 and saved.get("user_projection"):
                await asyncio.to_thread(write_meta, vid, image_scores=scores)     # the user's choice stays
            else:
                await asyncio.to_thread(write_meta, vid, projection=proj, projection_source="image",
                                        stereo=STEREO_OF.get(proj), image_scores=scores)
        if self.video_msg and self.video_msg.get("vid") == vid:
            self.video_msg.update(projection=proj, projection_source=src, image_test=False)
            if proj in STEREO_OF:
                self.video_msg["stereo"] = STEREO_OF[proj]
        log(f"[video] projection of {vid} set to {proj} ({src}{' ' + json.dumps(scores) if scores else ''})")

    async def _on_vfail(self, m: dict) -> None:
        mode = m.get("mode") if m.get("mode") in ("mse", "split") else "?"
        reason = str(m.get("reason") or "")[:200]
        log(f"[video] phone playback problem ({mode}): {reason}")
        cp = self.cur_play
        if self.mode != "vr" or not cp:
            return
        lag = reason.startswith("lag")
        if mode == "mse" and not lag:                  # the phone switched itself to two elements
            cp["mode"] = "split"
            if self.video_msg:
                self.video_msg["mode"] = "split"
            return
        if lag:
            from video_resolve import pick_streams
            pick = pick_streams(cp["info"], self.caps(), cp["kind"], below=cp["h"])
            if not pick:
                log("[video] no lower quality to fall back to; keep playing")
                return
            try:
                start = float(m.get("time"))
            except (TypeError, ValueError):
                start = cp["start"]
            log(f"[video] the phone cannot keep up ({mode}): switching to {pick['v'].get('height')}p")
            await self._ready_streams(cp["info"], pick, cp["page"], cp["title"], max(0.0, start), force_mode=mode)

    # ---------------------------------------------------------------- video: detection
    def _on_play(self, sid: str, payload: str) -> None:
        if len(payload) > 20000:
            return
        try:
            d = json.loads(payload)
        except ValueError:
            return
        if not isinstance(d, dict):
            return
        tab = self.sess_tab.get(sid)
        if tab is not None and tab.id != self.active:
            asyncio.create_task(self._safe("Runtime.evaluate", {"expression": PAUSE_JS}, session=sid, timeout=5))
            log(f"[video] play ignored (background window {tab.id}), paused it")
            return
        now = time.monotonic()
        user = now - self.last_click < 3.0             # phone input only (the page's flag is not trusted)
        d["user"] = user
        page = str(d.get("page", ""))
        from video_resolve import is_youtube, youtube_id
        why = None
        if self.mode == "resolving" and user and _video_key(d) != self.resolve_key:
            log(f"[video] another video was started while getting one ready: switching to {page[:80]}")
            d["session"] = sid
            self.start_resolve(d)
            return
        if self.mode in ("resolving", "vr"):
            # the page (e.g. YouTube autoplay) started it again: keep it quiet while the phone plays
            asyncio.create_task(self._safe("Runtime.evaluate", {"expression": PAUSE_JS}, session=sid, timeout=5))
            why = f"mode {self.mode}, paused it again"
        elif self.mode != "browse":
            why = f"mode {self.mode}"
        elif now < self.suppress_until and not (user and now - self.last_click < 2.0):
            # the page resumed by itself after B: keep it paused (no surprise laptop sound)
            asyncio.create_task(self._safe("Runtime.evaluate", {"expression": PAUSE_JS}, session=sid, timeout=5))
            why = "just left VR, paused it"
        elif d.get("muted") and not user:
            why = "muted autoplay"
        elif is_youtube(page) or is_youtube(self.url):
            if not (youtube_id(page) or youtube_id(self.url)):
                why = "YouTube preview (no video page)"
        else:
            dur = float(d.get("dur") or -1)
            if 0 < dur <= 30 and not user:
                why = "short clip (ad/preview)"
            elif int(d.get("area") or 0) < 200 * 120 and not user:
                why = "tiny video"
        if why:
            log(f"[video] play ignored ({why}): {page[:80]}")
            return
        d["session"] = sid
        log(f"[video] play detected: {page[:100]} ({'user' if user else 'auto'}, t={float(d.get('t') or 0):.1f}s)")
        self.start_resolve(d)

    async def manual_trigger(self) -> None:
        """X button / 'Play in VR': whatever video this page has (or just the page URL)."""
        found = None
        for sid in [self.session, *self.children]:
            r = await self._safe("Runtime.evaluate", {"expression": "window.__vrbInfo ? __vrbInfo() : null",
                                                      "returnByValue": True}, session=sid, timeout=5)
            val = ((r or {}).get("result") or {}).get("value")
            if val:
                with contextlib.suppress(ValueError):
                    d = json.loads(val)
                    if found is None or int(d.get("area") or 0) > int(found.get("area") or 0):
                        d["session"] = sid
                        found = d
        if found is None:
            if not self.url.startswith("http"):          # new window (about:blank), data: page ...
                await self.toast("No video on this page")
                return
            found = {"page": self.url, "top": True, "src": "", "t": 0, "session": self.session, "title": self.title}
        if self.mode == "resolving" and self.resolve_task and not self.resolve_task.done() \
                and _video_key(found) == self.resolve_key:
            await self.toast("Still getting this video ready...")
            return
        log(f"[video] manual VR request on {found.get('page', '')[:100]}")
        self.start_resolve(found)

    def start_resolve(self, d: dict) -> None:
        if self.resolve_task and not self.resolve_task.done():
            self.resolve_task.cancel()
        self.mode = "resolving"
        self.video_msg = None                         # a reconnecting phone must not get the old video
        self.resolve_key = _video_key(d)
        self.resolve_top = self.url
        self.video_session = d.get("session") or self.session
        self.resolve_task = asyncio.create_task(self._resolve(d))

    # ---------------------------------------------------------------- video: resolution (A3)
    async def _cookies(self, urls: list[str]) -> list[dict]:
        r = await self._safe("Network.getCookies", {"urls": [u for u in urls if u.startswith("http")]})
        return list((r or {}).get("cookies") or [])

    async def _resolve(self, d: dict) -> None:
        from video_resolve import ResolveError, cached_flat_file, is_youtube, youtube_id, youtube_start, \
            youtube_watch_url
        page = str(d.get("page") or self.url)
        title = str(d.get("title") or self.title or "video")
        title = re.sub(r"^\(\d+\)\s*", "", title)          # YouTube's "(3) " notification counter
        title = re.sub(r"\s*-\s*YouTube$", "", title)
        start = float(d.get("t") or 0)
        if d.get("ad") or (start < 1.0 and youtube_start(page)):
            start = youtube_start(page) or 0.0          # YouTube seeks to &t= only after 'play' fires
        t_play = time.monotonic()
        try:
            # pause the page video without waiting for it (a busy page can take seconds to answer)
            asyncio.create_task(self._safe("Runtime.evaluate", {"expression": PAUSE_JS}, session=self.video_session,
                                           timeout=5))
            if self.video_session != self.session:
                asyncio.create_task(self._safe("Runtime.evaluate", {"expression": PAUSE_JS}, timeout=5))
            self.resolve_title = title
            await self.broadcast({"t": "video", "state": "resolving", "title": title})
            last = [0.0, -1.0]

            async def progress(pct: float | None, text: str) -> None:
                now = time.monotonic()
                if pct is None:
                    await self.broadcast({"t": "video", "state": "progress", "text": text})
                elif int(pct) != int(last[1]) or now - last[0] > 0.7:
                    last[0], last[1] = now, pct
                    await self.broadcast({"t": "video", "state": "progress", "pct": round(pct, 1), "text": text})

            src = str(d.get("src") or "")
            # 1. not YouTube and the page plays a plain video file: that file IS the video
            if src.startswith(("http://", "https://")) and not is_youtube(page) and not is_youtube(self.url):
                if await self._direct(src, page, title, d, start, need_video_type=True):
                    return
            # 2. an old downloaded flat mp4: instant
            vid = youtube_id(page) or youtube_id(self.url)
            if vid:
                hit = await asyncio.to_thread(cached_flat_file, f"youtube_{vid}")
                if hit:
                    await self._ready_cached(hit, f"youtube_{vid}", title, start)
                    return
            # 3. yt-dlp lookup (the iframe's page for embedded players, then the top page)
            cands = []
            for u in (page, self.url):
                u = youtube_watch_url(u) or u
                if u.startswith("http") and u not in cands:
                    cands.append(u)
            for u in cands:
                yt = is_youtube(u)
                same = _same_site((urlparse(src).hostname or "").lower(), (urlparse(u).hostname or "").lower())
                cookie_urls = YT_COOKIE_URLS if yt else ([u, src] if same else [u])
                cached = self.resolver.cached_info(u) is not None
                if not cached:
                    await progress(None, "Looking up the video...")
                try:
                    t0 = time.monotonic()
                    # a cached lookup needs no cookies (a CDP call can take seconds while the page is busy)
                    info = await self.resolver.lookup(u, [] if cached else await self._cookies(cookie_urls))
                except ResolveError as e:
                    log(f"[video] yt-dlp: no ({e})")
                    continue
                except Exception as e:               # not CancelledError (BaseException): that must propagate
                    log(f"[video] yt-dlp failed: {e!r}")
                    continue
                log(f"[video] lookup {'(cached) ' if cached else ''}{time.monotonic() - t0:.1f}s: "
                    f"{str(info.get('title'))[:60]}")
                try:
                    if await self._from_info(info, u, yt, title, start, progress, cookie_urls):
                        log(f"[video] ready {time.monotonic() - t_play:.1f}s after the play event")
                        return
                except ResolveError as e:
                    log(f"[video] {e}")
            # 4. the page video's own file
            if src.startswith(("http://", "https://")):
                if await self._direct(src, page, title, d, start):
                    return
            # 5. nothing the phone can play: show the live page on the cinema screen
            await self._screen(title)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log(f"[video] resolve failed: {e!r}")
            self.mode = "browse"
            await self.broadcast({"t": "video", "state": "error", "text": f"Could not play this video ({e})"[:200]})
            await self.update_cast()

    async def _from_info(self, info: dict, page: str, yt: bool, title: str, start: float, progress,
                         cookie_urls: list[str]) -> bool:
        """A3.3: separate streams -> progressive -> last-resort download."""
        from video_resolve import content_kind, pick_progressive, pick_streams
        title = str(info.get("title") or title)
        kind = content_kind(info)
        caps = self.caps()
        pick = pick_streams(info, caps, kind)
        if pick:
            await self._ready_streams(info, pick, page, title, start)
            return True
        prog = pick_progressive(info, caps, kind)
        if prog:
            headers = dict(prog.get("http_headers") or {})
            if not yt:
                ck = await self._cookies([prog["url"]])
                if ck:
                    headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in ck)
            ent = {"kind": "proxy", "url": prog["url"], "headers": headers}
            await self._ready_file(ent, info, prog, page, title, start, "progressive")
            return True
        log("[video] no format the phone can stream: last resort, downloading it")
        r = await self.resolver.download(info, await self._cookies(cookie_urls), progress)
        await self._ready_file({"kind": "file", "path": r["path"]}, info,
                               {"format_id": r.get("fid"), "width": r["w"], "height": r["h"], "ext": "mp4",
                                "vcodec": "avc1"}, page, title, start, "downloaded")
        return True

    # ---------------------------------------------------------------- media table
    def _add_media(self, m: dict) -> str:
        now = time.time()
        for k in [k for k, v in self.media.items() if now - v.get("_t", now) > MEDIA_TTL]:
            self.media.pop(k, None)
        while len(self.media) >= MEDIA_MAX:
            self.media.pop(min(self.media, key=lambda k: self.media[k].get("_t", 0)))
        mid = secrets.token_hex(8)
        m["_t"] = now
        m["_sem"] = asyncio.Semaphore(UPSTREAM_PER_MID)
        m["_mid"] = mid
        self.media[mid] = m
        return mid

    def _yt_entry(self, f: dict, page: str, info: dict) -> dict:
        from video_resolve import mime_of, url_expire
        return {"kind": "yt", "url": f["url"], "headers": dict(f.get("http_headers") or {}),
                "fid": str(f.get("format_id")), "vid_key": page, "mime": mime_of(f),
                # yt-dlp's filesize is exact for YouTube; other extractors can be wrong: learn it from upstream
                "size": (int(f.get("filesize") or 0) or None)
                        if str(info.get("extractor_key") or "").lower().startswith("youtube") else None,
                "expire": url_expire(f["url"]),
                "index": None, "chunked": True}

    async def fetch(self, m: dict, ent: dict, a: int, b: int) -> bytes:
        """One closed upstream range of a media entry (refreshes an expired googlevideo URL)."""
        if ent.get("dead"):
            raise OSError("the video address can no longer be refreshed")
        async with m["_sem"]:
            if ent["kind"] == "yt" and time.time() > ent["expire"] - 120:
                await self._refresh(m)
            for attempt in (0, 1):
                used = ent["url"]
                try:
                    data, total = await asyncio.to_thread(_fetch_range, used, ent.get("headers") or {}, a, b)
                    break
                except urllib.error.HTTPError as e:
                    if ent["kind"] == "yt" and e.code in (403, 410) and attempt == 0:
                        if ent["url"] == used:            # nobody refreshed it meanwhile
                            log(f"[media] upstream {e.code}: refreshing the video address")
                            await self._refresh(m, force=True)
                        continue
                    raise
            if total and total != ent.get("size"):
                if ent.get("size"):
                    log(f"[media] {ent.get('fid')}: size {ent['size']} -> {total} (upstream)")
                ent["size"] = total                       # upstream is authoritative
            return data

    async def _refresh(self, m: dict, force: bool = False) -> None:
        """A8: new googlevideo URLs for every track of this mid (one refresh at a time per mid)."""
        task = m.get("_refresh")
        if task is None or task.done():
            if any(e.get("dead") for e in self._tracks(m)):
                raise OSError("the video address can no longer be refreshed")
            if time.monotonic() - m.get("_refresh_failed", -1e9) < 60:
                raise OSError("refresh failed less than a minute ago")
            if not force and all(e["kind"] != "yt" or time.time() < e["expire"] - 120 for e in self._tracks(m)):
                return
            task = m["_refresh"] = asyncio.create_task(self._do_refresh(m))
        await asyncio.shield(task)

    async def _do_refresh(self, m: dict) -> None:
        from video_resolve import url_expire
        ents = [e for e in self._tracks(m) if e["kind"] == "yt"]
        if not ents:
            return
        page = ents[0]["vid_key"]
        try:
            info = await self.resolver.lookup(page, await self._cookies(YT_COOKIE_URLS), force=True)
        except Exception as e:
            m["_refresh_failed"] = time.monotonic()
            log(f"[media] refresh failed: {e}")
            raise
        fmts = {str(f.get("format_id")): f for f in info.get("formats") or []}
        missing = [e["fid"] for e in ents if not (fmts.get(e["fid"]) or {}).get("url")]
        if missing:
            # the format set changed (other client, DRC variants...): the byte offsets of our index belong to
            # the old file, so do not swap in another format. Fail fast from now on (502, no more yt-dlp runs).
            for e in ents:
                e["dead"] = True
            log(f"[media] refresh: format(s) {', '.join(missing)} no longer offered; this stream has ended")
            raise OSError("format no longer offered")
        for e in ents:
            f = fmts[e["fid"]]
            e["url"], e["headers"], e["expire"] = f["url"], dict(f.get("http_headers") or {}), url_expire(f["url"])
        log(f"[media] refreshed the video address ({len(ents)} track(s))")

    @staticmethod
    def _tracks(m: dict) -> list[dict]:
        return [m[k] for k in ("v", "a", "f") if k in m]

    async def index_of(self, m: dict, ent: dict) -> dict | None:
        """G3 index (computed once per entry; False = none)."""
        import media_probe
        if ent.get("index") is None:
            lock = ent.setdefault("_ilock", asyncio.Lock())
            async with lock:
                if ent.get("index") is None:
                    try:
                        head = await self._head(m, ent, 65536)
                        ent["index"] = await media_probe.build_index(
                            lambda a, b: self._ranged(m, ent, a, b), ent.get("size"), ent["mime"], head=head)
                        ent["index"]["size"] = ent.get("size")
                    except Exception as e:
                        log(f"[media] no segment index for {ent.get('fid')}: {e!r}"[:200])
                        ent["index"] = False
        return ent["index"] or None

    async def _head(self, m: dict, ent: dict, n: int) -> bytes:
        """First n bytes (kept, so the index and the header read share one fetch)."""
        async with ent.setdefault("_hlock", asyncio.Lock()):
            h = ent.get("_head") or b""
            if len(h) < n:
                want = max(n, HEAD_BYTES)
                if ent.get("size"):
                    want = min(want, ent["size"])
                h = ent["_head"] = await self.fetch(m, ent, 0, want - 1)
        return h[:n]

    async def _ranged(self, m: dict, ent: dict, a: int, b: int) -> bytes:
        h = ent.get("_head") or b""
        if b < len(h):
            return h[a:b + 1]
        if ent.get("size"):
            b = min(b, ent["size"] - 1)
        out = []
        for s in range(a, b + 1, CHUNK):
            out.append(self.fetch(m, ent, s, min(b, s + CHUNK - 1)))
        return b"".join(await asyncio.gather(*out))

    async def _header_read(self, m: dict | None, ent: dict | None, url_fmt: dict | None) -> tuple[dict | None, bool]:
        """C2 step 1: (parse_projection result, read ok). Uses the entry if it is mp4, else url_fmt."""
        import media_probe
        try:
            if ent is not None and m is not None:
                head = await self._head(m, ent, 262144)
                return await media_probe.read_projection(lambda a, b: self._ranged(m, ent, a, b), head=head), True
            if url_fmt is not None:
                hdrs = dict(url_fmt.get("http_headers") or {})

                async def fetch(a, b):
                    return (await asyncio.to_thread(_fetch_range, url_fmt["url"], hdrs, a, b))[0]
                return await media_probe.read_projection(fetch), True
        except Exception as e:
            log(f"[video] header read failed: {e!r}"[:200])
        return None, False

    # ---------------------------------------------------------------- video: "ready" (G1)
    async def _ready_streams(self, info: dict, pick: dict, page: str, title: str, start: float,
                             force_mode: str | None = None) -> None:
        from video_resolve import (describe_format, detect_projection, header_format, info_key, read_meta,
                                   write_meta)
        import media_probe
        caps = self.caps()
        mode = force_mode or ("mse" if caps.get("mse") != "none" else "split")
        v, a = pick["v"], pick["a"]
        m = {"v": self._yt_entry(v, page, info), "a": self._yt_entry(a, page, info)}
        mid = self._add_media(m)
        key = info_key(info)
        hf = header_format(info, v)
        same = hf is v
        t0 = time.monotonic()
        jobs = [self._header_read(m if same else None, m["v"] if same else None, None if same else hf)]
        if mode == "mse":
            jobs += [self.index_of(m, m["v"]), self.index_of(m, m["a"])]
        res = await asyncio.gather(*jobs, return_exceptions=True)
        container, header_ok = res[0] if not isinstance(res[0], BaseException) else (None, False)
        if mode == "mse" and (isinstance(res[1], BaseException) or not res[1]
                              or isinstance(res[2], BaseException) or not res[2]):
            log("[video] no segment index: the phone plays it with two elements (split)")
            mode = "split"
        mesh_label = None
        if container and container.get("mesh_raw"):
            try:
                mesh = await asyncio.to_thread(media_probe.decode_mesh, container["mesh_raw"])
                mesh_label = mesh.pop("label")
                mesh.pop("yaw_span", None)
                m["_mesh"] = {"label": mesh_label, **mesh}
            except Exception as e:
                log(f"[video] mesh decode failed: {e!r}")
                container = {**container, "proj": None}
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        saved = await asyncio.to_thread(read_meta, key)
        det = detect_projection(info, hf or v, container, header_ok, saved, w, h, mesh_label)
        if not m.get("_mesh"):
            det["mesh"] = False
        await asyncio.to_thread(write_meta, key, title=title, w=w, h=h, projection=det["projection"],
                                stereo=det["stereo"], projection_source=det["projection_source"], mesh=det["mesh"])
        log(f"[video] probe {time.monotonic() - t0:.2f}s: projection {det['projection']} ({det['projection_source']})")
        k = f"?k={self.token}"
        idx = mode == "mse"
        fam = pick.get("family", "")
        vdesc = {"src": f"/media/{mid}/v{k}", "index": f"/media/{mid}/v/index{k}" if idx else None,
                 "mime": m["v"]["mime"], "codec": {"vp9mp4": "vp9"}.get(fam, fam) or "unknown",
                 "w": w, "h": h, "fps": round(float(v.get("fps") or 30)), "itag": str(v.get("format_id"))}
        adesc = {"src": f"/media/{mid}/a{k}", "index": f"/media/{mid}/a/index{k}" if idx else None,
                 "mime": m["a"]["mime"], "codec": "opus" if "opus" in str(a.get("acodec")) else "aac"}
        dur = float(info.get("duration") or 0) or ((m["v"].get("index") or {}).get("dur") if idx else None)
        fmt = f"{describe_format(v)} + {describe_format(a)} ({mode})"
        self.cur_play = {"info": info, "kind": pick["kind"], "page": page, "title": title, "start": start,
                         "h": min(w, h) if w and h else h, "mode": mode}
        await self._send_ready(mid, mode, key, title, start, dur, vdesc, adesc, det, fmt)

    async def _ready_file(self, ent: dict, info: dict | None, f: dict | None, page: str, title: str, start: float,
                          how: str, vid_key: str | None = "auto") -> None:
        """Mode "file": one file with picture and sound (progressive, downloaded, cached or the page's own)."""
        from video_resolve import codec_family, detect_projection, info_key, local_moov, read_meta, write_meta
        import media_probe
        f = f or {}
        m = {"f": ent}
        mid = self._add_media(m)
        container, header_ok = None, False
        if ent["kind"] == "file":
            moov = await asyncio.to_thread(local_moov, ent["path"])
            if moov:
                with contextlib.suppress(Exception):
                    container, header_ok = media_probe.parse_projection(moov), True
        elif "mp4" in str(f.get("ext") or ent.get("ctype") or "mp4"):
            try:
                container, header_ok = await asyncio.wait_for(self._header_read(m, ent, None), 6)
            except asyncio.TimeoutError:
                pass
        mesh_label = None
        if container and container.get("mesh_raw"):
            with contextlib.suppress(Exception):
                mesh = await asyncio.to_thread(media_probe.decode_mesh, container["mesh_raw"])
                mesh_label = mesh.pop("label")
                mesh.pop("yaw_span", None)
                m["_mesh"] = {"label": mesh_label, **mesh}
        w, h = int(f.get("width") or 0), int(f.get("height") or 0)
        key = (info_key(info) if info else None) if vid_key == "auto" else vid_key
        saved = await asyncio.to_thread(read_meta, key) if key else None
        det = detect_projection(info or {"title": title}, f, container, header_ok, saved, w, h, mesh_label)
        if not m.get("_mesh"):
            det["mesh"] = False
        if key:
            await asyncio.to_thread(write_meta, key, title=title, w=w, h=h, projection=det["projection"],
                                    stereo=det["stereo"], projection_source=det["projection_source"], mesh=det["mesh"])
        k = f"?k={self.token}"
        fam = codec_family(f) if f.get("vcodec") else None
        vdesc = {"src": f"/media/{mid}/f{k}", "index": None,
                 "mime": ent.get("ctype") or "video/mp4", "codec": {"vp9mp4": "vp9"}.get(fam or "", fam or "unknown"),
                 "w": w, "h": h, "fps": round(float(f.get("fps") or 30)), "itag": str(f["format_id"]) if f.get("format_id") else None}
        dur = float((info or {}).get("duration") or 0) or None
        self.cur_play = None
        await self._send_ready(mid, "file", key, title, start, dur, vdesc, None, det,
                               f"{f.get('format_id') or '-'} {w}x{h} ({how}, file)")

    async def _ready_cached(self, hit: dict, key: str, title: str, start: float) -> None:
        meta = hit.get("meta") or {}
        src = meta.get("projection_source")
        src = src if src in ("user", "image", "container", "note", "title", "guess") else "guess"
        m = {"f": {"kind": "file", "path": hit["path"]}}
        mid = self._add_media(m)
        w, h = int(hit.get("w") or 0), int(hit.get("h") or 0)
        det = {"projection": "flat", "stereo": "mono", "projection_source": src, "mesh": False, "mesh_label": None}
        vdesc = {"src": f"/media/{mid}/f?k={self.token}", "index": None, "mime": "video/mp4", "codec": "avc1",
                 "w": w, "h": h, "fps": 30, "itag": None}
        self.cur_play = None
        await self._send_ready(mid, "file", key, str(hit.get("title") or title), start, None, vdesc, None, det,
                               f"{w}x{h} (cached file)")

    async def _send_ready(self, mid: str, mode: str, key: str | None, title: str, start: float, dur,
                          vdesc: dict, adesc: dict | None, det: dict, fmt: str) -> None:
        if key:
            self.issued_vids.add(key)
        msg = {"t": "video", "state": "ready", "mode": mode, "vid": key, "title": title,
               "start": round(max(0.0, start), 2), "duration": round(dur, 3) if dur else None,
               "src": vdesc["src"], "v": vdesc, "a": adesc, "w": vdesc["w"], "h": vdesc["h"],
               "projection": det["projection"], "stereo": det["stereo"],
               "projection_source": det["projection_source"],
               "mesh": f"/media/{mid}/mesh?k={self.token}" if det.get("mesh") else None,
               "mesh_label": det.get("mesh_label") if det.get("mesh") else None,
               "image_test": det["projection_source"] in ("title", "guess"), "format": fmt}
        self.mode = "vr"
        self.video_msg = msg
        await self.broadcast(msg)
        await self.update_cast()                            # page picture not needed while the phone plays
        log(f"[video] READY ({mode}): {title[:60]} | {fmt} | {det['projection']} ({det['projection_source']}) "
            f"start={start:.1f}s")

    async def _direct(self, src: str, page: str, title: str, d: dict, start: float,
                      need_video_type: bool = False) -> bool:
        """Serve the page video's own http(s) file through /media (browser cookies, UA and Referer)."""
        src_host, page_host = (urlparse(src).hostname or "").lower(), (urlparse(page).hostname or "").lower()
        top_host = (urlparse(self.url).hostname or "").lower()
        if not await self._src_on_page(d.get("session") or self.session, src):
            log("[video] direct file refused: no <video> on the page plays that address")
            return False
        if src_host not in (page_host, top_host) and await asyncio.to_thread(_private_host, src_host):
            log("[video] direct file refused: a private/local address that is not the page's own host")
            return False
        headers = {"User-Agent": self.ua, "Referer": page}
        # the browser's cookies only for the page's own site (no cross-site cookie-bearing GETs from the laptop)
        if _same_site(src_host, top_host) or _same_site(src_host, page_host):
            ck = await self._cookies([src])
            if ck:
                headers["Cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in ck)
        ok, ctype = await asyncio.to_thread(_probe_url, src, headers)
        lc = ctype.lower()
        if ok and any(bad in lc for bad in ("mpegurl", "dash+xml", "text/", "html", "json", "xml")):
            log(f"[video] page video is not a file ({ctype})")
            return False                     # HLS/DASH playlist or a web page: the phone cannot play it as a file
        if ok and need_video_type and not lc.startswith("video/"):
            return False                     # unknown type: let yt-dlp have a go first
        if not ok:
            log("[video] direct file not reachable")
            return False
        w, h = int(d.get("w") or 0), int(d.get("h") or 0)
        log(f"[video] direct file ({ctype}, {w}x{h})")
        ent = {"kind": "proxy", "url": src, "headers": headers, "ctype": ctype.split(";")[0].strip() or "video/mp4"}
        await self._ready_file(ent, None, {"width": w, "height": h, "ext": "mp4" if "mp4" in lc else lc},
                               page, title, start, "page file", vid_key=None)
        return True

    async def _src_on_page(self, sid: str, src: str) -> bool:
        """Is `src` really the address of a <video> in that frame (not just something a page reported)?"""
        expr = ("(() => { const s = %s; for (const v of document.getElementsByTagName('video'))"
                " if (String(v.currentSrc || v.src || '') === s) return true; return false; })()" % json.dumps(src))
        r = await self._safe("Runtime.evaluate", {"expression": expr, "returnByValue": True}, session=sid, timeout=5)
        return ((r or {}).get("result") or {}).get("value") is True

    async def _screen(self, title: str) -> None:
        self.mode = "screen"
        msg = {"t": "video", "state": "screen", "title": title}
        self.video_msg = msg
        self.suppress_until = time.monotonic() + 3
        await self.broadcast(msg)
        await self.update_cast()
        js = ("(() => { const v = window.__vrbMain && __vrbMain(); if (!v) return false;"
              " try { v.play(); } catch (_) {}"
              " try { if (!document.fullscreenElement && v.requestFullscreen) v.requestFullscreen(); } catch (_) {}"
              " return true; })()")
        await self._safe("Runtime.evaluate", {"expression": js, "userGesture": True, "awaitPromise": False},
                         session=self.video_session, timeout=5)
        await self.toast("Showing the browser picture on the big screen - the sound plays on the laptop")
        log(f"[video] SCREEN mode: {title[:60]}")

    async def leave_video(self, at, origin: "Client | None" = None) -> None:
        if self.resolve_task and not self.resolve_task.done():
            self.resolve_task.cancel()
        was = self.mode
        self.mode = "browse"
        self.video_msg = None
        self.cur_play = None
        self.suppress_until = time.monotonic() + 2.5
        js = "(() => { const v = window.__vrbMain && __vrbMain(); if (!v) return false;"
        try:
            t = float(at) if at is not None else None
        except (TypeError, ValueError):
            t = None
        if t is not None and t >= 0:
            js += f" try {{ v.currentTime = {t:.3f}; }} catch (_) {{}}"
        js += (" try { v.pause(); } catch (_) {}"
               " try { if (document.fullscreenElement) document.exitFullscreen(); } catch (_) {} return true; })()")
        if was != "browse":
            await self._safe("Runtime.evaluate", {"expression": js, "userGesture": True},
                             session=self.video_session or self.session, timeout=5)
            log(f"[video] back to the browser (from {was}{'' if t is None else f', at {t:.1f}s'})")
            # every phone (except the one whose B started this) must leave VR too
            for c in list(self.clients):
                if c is not origin or (was == "vr" and t is None):
                    await c.text({"t": "video", "state": "exit"})
        await self.update_cast()
        if self.last_jpeg:
            for c in list(self.clients):
                c.push_frame(self.last_jpeg)

    # ---------------------------------------------------------------- phone connect
    async def add_client(self, c: Client) -> None:
        self.clients.add(c)
        self.sent_info = None
        self._info()
        await c.text(self._tabs_msg())
        if self.video_msg:
            await c.text(self.video_msg)
        elif self.mode == "resolving":                 # so its progress bar (and B = cancel) work
            await c.text({"t": "video", "state": "resolving", "title": self.resolve_title or "video"})
        if self.last_jpeg and self.mode != "vr":
            c.push_frame(self.last_jpeg)
        await self.update_cast()

    async def remove_client(self, c: Client) -> None:
        c.alive = False
        c.ev.set()
        self.clients.discard(c)
        await self.update_cast()

    def status_line(self) -> str:
        ids = list(self.tabs)
        win = f"window {ids.index(self.active) + 1}/{len(ids)}" if self.active in ids else "no window"
        return f"{len(self.clients)} phone(s) | {win} | {self.mode} | {self.title or self.url}"


def _video_key(d: dict) -> str:
    """Identity of a played video: the YouTube id, else page URL (no #fragment) + the file URL."""
    from video_resolve import youtube_id
    page = str(d.get("page") or "")
    vid = youtube_id(page)
    if vid:
        return "yt:" + vid
    return page.split("#")[0] + "|" + str(d.get("src") or "")[:300]


def _devtools_url(port: int) -> str | None:
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
            return json.loads(r.read()).get("webSocketDebuggerUrl")
    except Exception:
        return None


def _private_host(host: str) -> bool:
    """Loopback / LAN / link-local / reserved address (or a name that resolves to one)."""
    import ipaddress
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(ai[4][0].split("%")[0]) for ai in socket.getaddrinfo(host, None)]
        except OSError:
            return False                               # cannot resolve: the fetch fails anyway
    return any(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_unspecified
               or a.is_multicast for a in addrs)


def _site(host: str) -> str:
    """Rough registrable domain ("www.example.co.uk" -> "example.co.uk"); IP addresses stay as they are."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if re.fullmatch(r"[\d.]+|[0-9a-f:]+", host):
        return host
    p = host.split(".")
    if len(p) >= 3 and len(p[-1]) == 2 and p[-2] in ("co", "com", "net", "org", "gov", "ac", "edu", "ne", "or"):
        return ".".join(p[-3:])
    return ".".join(p[-2:])


def _same_site(a: str, b: str) -> bool:
    return bool(a) and bool(b) and _site(a) == _site(b)


class _SameHostCookies(urllib.request.HTTPRedirectHandler):
    """urllib copies every request header to a redirect target, on ANY host. The Cookie header we
    add holds the browser's (signed-in) cookies for the original host only: drop it, and any
    Authorization, as soon as a redirect leaves that host (e.g. site -> CDN, or an open redirect)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None:
            old, nu = urlparse(req.full_url), urlparse(new.full_url)
            if ((nu.hostname or "").lower() != (old.hostname or "").lower() and _private_host(nu.hostname or "")
                    and not _private_host(old.hostname or "")):
                raise urllib.error.HTTPError(newurl, 403, "redirect to a private address refused", headers, fp)
            downgrade = old.scheme == "https" and nu.scheme != "https"
            if (nu.hostname or "").lower() != (old.hostname or "").lower() or downgrade:
                for h in ("Cookie", "Authorization"):
                    new.headers.pop(h, None)
                    new.unredirected_hdrs.pop(h, None)
        return new


_HTTP = urllib.request.build_opener(_SameHostCookies)


def _probe_url(url: str, headers: dict) -> tuple[bool, str]:
    """Does the page video's URL answer us (bytes 0-0)?"""
    req = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
    try:
        with _HTTP.open(req, timeout=15) as r:
            ctype = r.headers.get("Content-Type", "")
            return r.status in (200, 206), ctype
    except Exception:
        return False, ""


def _fetch_range(url: str, headers: dict, a: int, b: int) -> tuple[bytes, int | None]:
    """One closed upstream range bytes=a-b -> (data, total size from Content-Range | None).
    HTTPError propagates (403/410 = expired googlevideo URL)."""
    req = urllib.request.Request(url, headers={**headers, "Range": f"bytes={a}-{b}"})
    with _HTTP.open(req, timeout=20) as r:
        total = None
        m = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", "") or "")
        if m:
            total = int(m.group(1))
        if r.status == 200:                            # Range ignored: only usable from byte 0
            if a != 0:
                raise OSError("upstream ignores Range")
            total = int(r.headers.get("Content-Length") or 0) or None
            return r.read(b + 1), total
        data = r.read()
    return data, total


# ================================================================ web app
def _cleanup_response_class():
    from starlette.responses import StreamingResponse

    class _CleanupStreamingResponse(StreamingResponse):
        """StreamingResponse that always runs `on_end` (also when the client left before the body started)."""

        def __init__(self, content, on_end, **kw) -> None:
            super().__init__(content, **kw)
            self._on_end = on_end

        async def __call__(self, scope, receive, send) -> None:
            try:
                await super().__call__(scope, receive, send)
            finally:
                self._on_end()
                with contextlib.suppress(Exception):
                    await self.body_iterator.aclose()
    return _CleanupStreamingResponse


def build_app(vb: VRBrowser, ph) -> "FastAPI":
    _CleanupStreamingResponse = _cleanup_response_class()
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    key_b = vb.token.encode()

    def key_ok(k: str | None) -> bool:
        return bool(k) and hmac.compare_digest(k.encode("utf-8", "replace"), key_b)

    nostore = {"Cache-Control": "no-store"}

    @app.get("/")
    async def index(request: Request):
        if not key_ok(request.query_params.get("k")):
            ip = request.client.host if request.client else "?"
            log(f"[web] {ip} opened the page without the access key -> 403")
            return HTMLResponse("<h1>403</h1><p>Open the full address printed in the laptop window (it ends in ?k=...).</p>",
                                status_code=403, headers=nostore)
        page = WEB / "index.html"
        if not page.is_file():
            return HTMLResponse("<h1>web/index.html is missing</h1>", status_code=500, headers=nostore)
        ip = request.client.host if request.client else "?"
        log(f"[web] page loaded by {ip} ({request.headers.get('user-agent', '')[:80]})")
        return Response(page.read_bytes(), media_type="text/html; charset=utf-8", headers=nostore)

    @app.get("/ca.crt")
    async def ca_crt():
        return Response(ph.ca_der(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="cross-reality-home-ca.crt"'})

    def get_media(mid: str, request: Request):
        if not key_ok(request.query_params.get("k")):
            return None, Response(status_code=403)
        m = vb.media.get(mid)
        if not m:
            return None, Response(status_code=404)
        return m, None

    @app.get("/media/{mid}.mp4")
    async def media_v1(mid: str, request: Request):
        m, err = get_media(mid, request)
        if err:
            return err
        return await serve_track(m, "f", request)

    @app.get("/media/{mid}/{track}")
    async def media_track(mid: str, track: str, request: Request):
        m, err = get_media(mid, request)
        if err:
            return err
        if track == "mesh":
            if not m.get("_mesh"):
                return Response(status_code=404)
            return JSONResponse(m["_mesh"], headers=nostore)
        return await serve_track(m, track, request)

    @app.get("/media/{mid}/{track}/index")
    async def media_index(mid: str, track: str, request: Request):
        m, err = get_media(mid, request)
        if err:
            return err
        ent = m.get(track) if track in ("v", "a", "f") else None
        if not ent or ent.get("kind") != "yt":
            return Response(status_code=404)
        idx = await vb.index_of(m, ent)
        if not idx:
            return Response(status_code=404, headers=nostore)
        return JSONResponse(idx, headers=nostore)

    async def serve_track(m: dict, track: str, request: Request):
        ent = m.get(track) if track in ("v", "a", "f") else None
        if not ent:
            return Response(status_code=404)
        rng = request.headers.get("range")
        if ent["kind"] == "file":
            return file_response(Path(ent["path"]), rng)
        if ent["kind"] == "yt":
            return await yt_response(m, ent, rng)
        return await proxy_response(ent, rng)

    async def yt_response(m: dict, ent: dict, rng: str | None):
        """A8: closed 2 MiB upstream chunks, 3 in parallel, honest Content-Range, refresh on expiry."""
        try:
            if not ent.get("size"):
                await vb.fetch(m, ent, 0, 0)                  # learns the size from Content-Range
        except Exception as e:
            log(f"[media] upstream failed: {e!r}"[:200])
            return Response(status_code=502)
        size = int(ent.get("size") or 0)
        if size <= 0:
            return Response(status_code=502)
        hdr = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        status = 200
        if rng:
            mm = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng)
            if not mm or (not mm.group(1) and not mm.group(2)):
                return Response(status_code=416, headers={**hdr, "Content-Range": f"bytes */{size}"})
            if mm.group(1):
                a = int(mm.group(1))
                if mm.group(2):
                    b = min(int(mm.group(2)), size - 1, a + MAX_RANGE - 1)
                else:
                    b = min(size - 1, a + OPEN_RANGE - 1)
            else:
                a, b = max(0, size - int(mm.group(2))), size - 1
                b = min(b, a + MAX_RANGE - 1)
            if a >= size or a > b:
                return Response(status_code=416, headers={**hdr, "Content-Range": f"bytes */{size}"})
            status = 206
            hdr["Content-Range"] = f"bytes {a}-{b}/{size}"
        else:
            a, b = 0, size - 1
        hdr["Content-Length"] = str(b - a + 1)
        spans = [(s, min(b, s + CHUNK - 1)) for s in range(a, b + 1, CHUNK)]
        pending: collections.deque = collections.deque()
        nxt = 0

        def fill() -> None:
            nonlocal nxt
            while len(pending) < PARALLEL_CHUNKS and nxt < len(spans):
                pending.append(asyncio.create_task(vb.fetch(m, ent, *spans[nxt])))
                nxt += 1

        def cancel_pending() -> None:
            for t in pending:
                t.cancel()
            pending.clear()

        fill()
        try:
            first = await pending[0]                      # an error now is still a clean 502
        except BaseException as e:                        # also CancelledError (the phone gave up)
            cancel_pending()
            if not isinstance(e, Exception):
                raise
            log(f"[media] upstream failed: {e!r}"[:200])
            return Response(status_code=502)

        async def body():
            try:
                pending.popleft()
                yield first
                while pending or nxt < len(spans):
                    fill()
                    data = await pending.popleft()
                    yield data
                    fill()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log(f"[media] stream stopped: {e!r}"[:200])
            finally:
                cancel_pending()

        ctype = ent["mime"].split(";")[0].strip()
        # if the phone aborts before the body starts (MSE seeks abort their fetch), Starlette never runs the
        # generator, so its finally does not run either: cancel the prefetched chunks when the response ends
        return _CleanupStreamingResponse(body(), cancel_pending, status_code=status, media_type=ctype, headers=hdr)


    @app.websocket("/ws/vr")
    async def ws_vr(ws: WebSocket):
        origin = ws.headers.get("origin")
        host = ws.headers.get("host", "")
        if not key_ok(ws.query_params.get("k")) or (origin and origin != f"https://{host}"):
            log(f"[ws] refused {ws.client.host if ws.client else '?'} (key or origin)")
            await ws.close(code=1008)
            return
        await ws.accept()
        c = Client(ws)
        log(f"[ws] phone connected {c.ip}")
        tasks = [asyncio.create_task(c.frame_loop()), asyncio.create_task(c.pad_loop())]
        await vb.add_client(c)
        try:
            while c.alive:
                raw = await ws.receive_text()
                if len(raw) > 10000:
                    continue
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(m, dict):
                    await vb.handle(c, m)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as e:
            log(f"[ws] {c.ip}: {e!r}")
        finally:
            await vb.remove_client(c)
            for t in tasks:
                t.cancel()
            log(f"[ws] phone disconnected {c.ip} ({c.frames} frames sent)")

    def parse_range(rng: str | None, size: int) -> tuple[int, int] | None:
        m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", rng or "")
        if not m or (not m.group(1) and not m.group(2)):
            return None
        if m.group(1):
            a = int(m.group(1))
            b = int(m.group(2)) if m.group(2) else size - 1
        else:
            a, b = max(0, size - int(m.group(2))), size - 1
        return a, min(b, size - 1)

    def file_response(path: Path, rng: str | None):
        try:
            size = path.stat().st_size
        except OSError:
            return Response(status_code=404)
        hdr = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        r = parse_range(rng, size) if rng else None
        if rng and (r is None or r[0] >= size or r[0] > r[1]):
            return Response(status_code=416, headers={**hdr, "Content-Range": f"bytes */{size}"})
        a, b = r if r else (0, size - 1)

        async def chunks():
            # async (not a sync generator in Starlette's thread pool): when the phone aborts the request the
            # generator is closed at once, so the file is not left open (Windows could not delete/rename it)
            f = await asyncio.to_thread(open, path, "rb")
            try:
                f.seek(a)
                left = b - a + 1
                while left > 0:
                    data = await asyncio.to_thread(f.read, min(512 * 1024, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data
            finally:
                f.close()

        hdr["Content-Length"] = str(b - a + 1)
        if r:
            hdr["Content-Range"] = f"bytes {a}-{b}/{size}"
        return _CleanupStreamingResponse(chunks(), lambda: None, status_code=206 if r else 200, media_type="video/mp4",
                                         headers=hdr)

    async def proxy_response(ent: dict, rng: str | None):
        headers = dict(ent.get("headers") or {})
        if rng:
            headers["Range"] = rng
        req = urllib.request.Request(ent["url"], headers=headers)
        try:
            up = await asyncio.to_thread(_HTTP.open, req, timeout=20)
        except urllib.error.HTTPError as e:
            return Response(status_code=e.code if e.code in (403, 404, 416) else 502)
        except Exception as e:
            log(f"[media] proxy failed: {e!r}")
            return Response(status_code=502)
        hdr = {"Accept-Ranges": "bytes", "Cache-Control": "no-store"}
        for k in ("Content-Length", "Content-Range"):
            if up.headers.get(k):
                hdr[k] = up.headers[k]
        ctype = up.headers.get("Content-Type", "")
        ctype = ctype if ctype.startswith("video/") else "video/mp4"
        status, skip, limit = up.status, 0, None
        size = int(up.headers.get("Content-Length") or 0) if up.status == 200 else 0
        if rng and up.status == 200 and size:
            # the site ignored Range (sent the whole file): cut the asked-for part out ourselves,
            # because iOS Safari only plays video that answers Range with 206
            r = parse_range(rng, size)
            if r is None or r[0] >= size or r[0] > r[1]:
                up.close()
                return Response(status_code=416, headers={**hdr, "Content-Range": f"bytes */{size}"})
            if r[0] > PROXY_MAX_SKIP:
                # skipping means downloading (and throwing away) everything before the offset: far into a
                # big file the phone would time out first. Fail fast instead (the phone goes back to the browser)
                up.close()
                log(f"[media] the site ignores Range; not skipping {r[0] // 1_000_000} MB to reach the asked-for part")
                return Response(status_code=502)
            skip, limit = r[0], r[1] - r[0] + 1
            status = 206
            hdr["Content-Range"] = f"bytes {r[0]}-{r[1]}/{size}"
            hdr["Content-Length"] = str(limit)

        def chunks():
            try:
                left_skip, left = skip, limit
                while left_skip > 0:
                    data = up.read(min(256 * 1024, left_skip))
                    if not data:
                        return
                    left_skip -= len(data)
                while left is None or left > 0:
                    data = up.read(256 * 1024 if left is None else min(256 * 1024, left))
                    if not data:
                        break
                    if left is not None:
                        left -= len(data)
                    yield data
            finally:
                up.close()

        return StreamingResponse(chunks(), status_code=status, media_type=ctype, headers=hdr)

    return app


# ================================================================ sign-in mode
def login_mode() -> int:
    why = profile_in_use()
    if why:
        print(f"  !! Cannot open the sign-in window: {why}.")
        return 1
    if not BRAVE.is_file():
        print(f"  !! Brave not found at {BRAVE}")
        return 1
    lock_dir(PROFILE)
    print("=" * 64)
    print("  SIGN-IN MODE")
    print("  A normal Brave window opens with the VR browser's own profile.")
    print("  Sign in to YouTube (and any other site you want), then CLOSE that window.")
    print("  The VR browser keeps you signed in after that.")
    print("=" * 64, flush=True)
    p = subprocess.Popen([str(BRAVE), f"--user-data-dir={PROFILE}", "--no-first-run",
                          "--no-default-browser-check", HOME_URL])
    p.wait()
    # Brave can hand over to a child and exit early; wait until nothing uses the profile any more
    while profile_users():
        time.sleep(1)
    print("  Sign-in window closed. Now start vr_browser_test.bat.")
    return 0


# ================================================================ main
def print_banner(urls: list[str]) -> None:
    line = "=" * 70
    print(line)
    print("  VR BROWSER TEST   (headless Brave -> phone headset)")
    print(line)
    if not urls:
        print("  !! No Wi-Fi IPv4 found. Is the laptop on Wi-Fi? Try --ip 192.168.x.y")
    for i, u in enumerate(urls):
        print(("  OPEN ON THE PHONE:  " if i == 0 else "  or:                ") + u)
    if urls:
        try:
            import qrcode
            qr = qrcode.QRCode(border=1)
            qr.add_data(urls[0])
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            pass
        except Exception:
            pass
    print()
    print("  The address contains the access key: do not share it.")
    print("  Not signed in to YouTube? Close this window and run vr_browser_test\\login.bat once.")
    print(line, flush=True)


async def main_async(args) -> int:
    import uvicorn
    loop = asyncio.get_running_loop()

    def quiet_resets(lp, ctx) -> None:
        # Windows: the phone's <video> drops Range connections all the time (seeking, buffering);
        # asyncio's proactor then prints a harmless ConnectionResetError traceback. Hide just that.
        if isinstance(ctx.get("exception"), (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        lp.default_exception_handler(ctx)

    loop.set_exception_handler(quiet_resets)
    ph = _load_phone_host()
    if not port_free(args.port):
        print(f"  !! Port {args.port} is already in use. Is phone_host_test.bat (or another copy of this")
        print("     server) still running? Close it and start again.")
        return 1
    why = profile_in_use()
    if why:
        print(f"  !! Cannot start the browser: {why}.")
        return 1
    ips = [args.ip] if args.ip else ph.detect_ips()
    ph.ensure_cert(ips)
    token = load_token(ph.CERT_DIR)
    lock_dir(PROFILE)
    lock_dir(CACHE)
    # left-over cookie / info files from a crash
    for f in list(CACHE.glob("cookies_*.txt")) + list(CACHE.glob("info_*.json")):   # (info_*: last-resort download)
        f.unlink(missing_ok=True)

    vb = VRBrowser(token, args.zoom)
    log(f"[video] {vb.resolver.describe()}")
    await vb.start()
    import pad_relay
    pad_relay.start()
    urls = [f"https://{ip}:{args.port}/?k={token}" for ip in ips]
    print_banner(urls)
    if not args.no_keyboard:
        import typing_window
        loop = asyncio.get_running_loop()

        async def back():
            await vb.nav("back")

        async def home():
            vb.go(HOME_URL)

        async def go(text: str):
            vb.go(text)

        async def new_window():
            await vb.new_tab()

        typing_window.start(loop, typing_window.Actions(
            type_text=vb.type_text, go=go, back=back, home=home, play_vr=vb.manual_trigger,
            status=vb.status_line, new_window=new_window))
    server = uvicorn.Server(uvicorn.Config(build_app(vb, ph), host=args.bind, port=args.port, log_level="warning",
                                           access_log=False, ws="websockets",
                                           ssl_certfile=str(ph.CERT_FILE), ssl_keyfile=str(ph.KEY_FILE)))
    wd = asyncio.create_task(vb.watchdog())
    try:
        await server.serve()
    finally:
        wd.cancel()
        # Ctrl+C: uvicorn re-raises SIGINT after serve() returns and asyncio.run then cancels this task,
        # so the first await below would raise CancelledError before Brave could save its cookies.
        # Withdraw that cancel for the shutdown (asyncio.run still ends with KeyboardInterrupt).
        me = asyncio.current_task()
        while me is not None and me.cancelling():
            me.uncancel()
        try:
            await asyncio.wait_for(vb.shutdown(), 20)
        except BaseException as e:            # noqa: BLE001 - last resort: the job object kills Brave
            log(f"[browser] clean shutdown failed: {e!r}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--ip", help="LAN IP to advertise instead of auto-detect")
    ap.add_argument("--zoom", type=float, default=1.25, help="page zoom (bigger text, fewer columns)")
    ap.add_argument("--login", action="store_true", help="open a normal Brave window to sign in once")
    ap.add_argument("--no-keyboard", action="store_true", help="do not open the laptop typing window")
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True, errors="replace")
    sys.path.insert(0, str(HERE))
    if args.login:
        sys.exit(login_mode())
    try:
        code = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        code = 0
    print("server stopped")
    sys.exit(code)


if __name__ == "__main__":
    main()

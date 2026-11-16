"""Headless web browser for the library's browser terminals (Phase 4).

One Edge (or Chrome) process is started lazily, without a window, and driven over the
Chrome DevTools Protocol (CDP). Every Godot panel that connects to WS /ws/browser gets
its own tab (its own window target, so it always renders) which is closed again on
disconnect.

Client -> server (JSON text):
    {"cmd":"open", "url":"...", "w":1280, "h":720, "zoom":1.25}   url may be words -> web search (/search)
    {"cmd":"mouse", "type":"move|down|up|wheel", "x":..,"y":.., "button":"left|right", "dx":..,"dy":..}
    {"cmd":"text", "text":"..."}
    {"cmd":"key", "key":"Enter|Backspace|Tab|ArrowUp|...|a", "down":true, "mods":0}   mods: 1 alt 2 ctrl 4 meta 8 shift
    {"cmd":"nav", "action":"back|forward|reload|home"}
    {"cmd":"active", "on":true|false}                            screencast on/off
Server -> client:
    BINARY  one JPEG per frame (Page.startScreencast, at most MAX_FPS per tab)
    TEXT    {"type":"info", "url":..., "title":..., "loading":bool}

Coordinates are page-image pixels (0..w, 0..h); the zoom factor is applied here.

Environment:
    BROWSER_MODE=headless (default) | offscreen   offscreen = a normal window parked at -32000,-32000
    BROWSER_EXE=<path>                            override the browser executable
    BROWSER_PROFILE=<dir>                         profile folder (default cache/browser_profile); one
                                                  browser per profile, so test services use their own
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from media_service import CACHE, load_library

HERE = Path(__file__).resolve().parent
START_HTML = HERE / "web" / "start.html"
PROFILE = Path(os.environ.get("BROWSER_PROFILE") or (CACHE / "browser_profile"))   # override for test services
MAX_FPS = 30.0
CANDIDATES = [
    r"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Microsoft/Edge/Application/msedge.exe",
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
]

router = APIRouter()


def find_browser() -> str | None:
    env = os.environ.get("BROWSER_EXE")
    if env and Path(env).is_file():
        return env
    for c in CANDIDATES:
        if Path(c).is_file():
            return c
    return None


def normalize_url(text: str, home: str) -> str:
    t = (text or "").strip()
    if not t or t == "home":
        return home
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", t) and not re.match(r"^[\w.-]+:\d+", t):
        return t                                            # has a scheme (http:, about:, data:, file:)
    if " " not in t and ("." in t or t.startswith("localhost") or re.match(r"^[\w.-]+:\d+", t)):
        return ("http://" if t.startswith(("localhost", "127.")) else "https://") + t
    return search_url(t)


# Google answers the headless browser with its /sorry captcha page, so typed words go to an
# engine that works headless. library.json "search_url" overrides it (the query is appended).
DEFAULT_SEARCH = "https://duckduckgo.com/?q="


def search_url(words: str) -> str:
    base = str(load_library().get("search_url") or DEFAULT_SEARCH)
    return base + quote_plus(words)


# ---------------------------------------------------------------- key table
# key -> (code, windowsVirtualKeyCode, text)
_KEYS: dict[str, tuple[str, int, str]] = {
    "Enter": ("Enter", 13, "\r"), "Backspace": ("Backspace", 8, ""), "Tab": ("Tab", 9, "\t"),
    "Escape": ("Escape", 27, ""), "Delete": ("Delete", 46, ""), "Insert": ("Insert", 45, ""),
    "ArrowUp": ("ArrowUp", 38, ""), "ArrowDown": ("ArrowDown", 40, ""),
    "ArrowLeft": ("ArrowLeft", 37, ""), "ArrowRight": ("ArrowRight", 39, ""),
    "Home": ("Home", 36, ""), "End": ("End", 35, ""), "PageUp": ("PageUp", 33, ""),
    "PageDown": ("PageDown", 34, ""), " ": ("Space", 32, " "), "Space": ("Space", 32, " "),
}
_KEYS["Tab"] = ("Tab", 9, "")          # no text: a "\t" char would be typed into text areas
for _i in range(1, 13):
    _KEYS[f"F{_i}"] = (f"F{_i}", 111 + _i, "")
_EDIT_CMDS = {"a": "selectAll", "c": "copy", "v": "paste", "x": "cut", "z": "undo", "y": "redo"}


def key_params(key: str, down: bool, mods: int) -> list[dict]:
    if key in _KEYS:
        code, vk, text = _KEYS[key]
        if key == "Space":
            key = " "
    elif len(key) == 1:
        ch = key
        if ch.isalpha():
            code, vk = "Key" + ch.upper(), ord(ch.upper())
        elif ch.isdigit():
            code, vk = "Digit" + ch, ord(ch)
        else:
            code, vk = "", 0
        text = ch
    else:
        code, vk, text = key, 0, ""
    if mods & 3:                                            # alt / ctrl: shortcuts, no text
        text = ""
    p = {"type": ("keyDown" if text else "rawKeyDown") if down else "keyUp", "key": key, "code": code,
         "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk, "modifiers": mods}
    if down and text:
        p["text"] = p["unmodifiedText"] = text
    if down and mods & 2 and key.lower() in _EDIT_CMDS:
        p["commands"] = [_EDIT_CMDS[key.lower()]]
    return [p]


def _kill_leftovers() -> None:
    """A service that was force-killed may leave a browser on our profile (it would block a new one)."""
    try:
        import psutil
    except ImportError:
        return
    prof = str(PROFILE).lower()
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmd = " ".join(p.info["cmdline"] or []).lower()
            if prof in cmd and "--type=" not in cmd:
                print(f"[browser] killing leftover browser pid {p.pid}")
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


_JOB = None


def _kill_with_us(proc: subprocess.Popen) -> None:
    """Put the browser in a Windows job object that is killed when this service exits, even if the
    service is force-killed (Edge's own child processes inherit the job)."""
    global _JOB
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE
        if _JOB is None:
            job = k32.CreateJobObjectW(None, None)

            class IO(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in
                            ("r", "w", "o", "rb", "wb", "ob")]

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
        h = k32.OpenProcess(0x0101, False, proc.pid)   # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        ok = k32.AssignProcessToJobObject(wintypes.HANDLE(_JOB), wintypes.HANDLE(h))
        k32.CloseHandle(wintypes.HANDLE(h))
        if not ok:
            raise OSError(ctypes.get_last_error())
    except Exception as e:
        print(f"[browser] job object not set ({e!r}); the browser may outlive a killed service")


# ---------------------------------------------------------------- CDP browser
class Browser:
    """One browser process + one browser-level CDP websocket with flattened tab sessions."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.sessions: dict[str, "Tab"] = {}            # sessionId -> Tab
        self.targets: dict[str, "Tab"] = {}             # targetId -> Tab
        self.adopted: set[str] = set()                  # popup targetIds already redirected
        self.lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and self.ws is not None \
            and self.reader is not None and not self.reader.done()

    async def ensure(self) -> None:
        async with self.lock:
            if self.alive:
                return
            await self._stop()
            await self._start()

    async def _start(self) -> None:
        from websockets.asyncio.client import connect
        exe = find_browser()
        if not exe:
            raise RuntimeError("no Edge/Chrome found (set BROWSER_EXE)")
        PROFILE.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_kill_leftovers)            # psutil scan + taskkill would block the loop
        port_file = PROFILE / "DevToolsActivePort"
        port_file.unlink(missing_ok=True)
        mode = os.environ.get("BROWSER_MODE", "headless").lower()
        args = [exe, f"--user-data-dir={PROFILE}", "--remote-debugging-port=0",
                "--remote-allow-origins=*", "--window-size=1280,720",
                "--no-first-run", "--no-default-browser-check", "--disable-default-apps",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows", "--disable-features=CalculateNativeWinOcclusion",
                "--disable-sync", "--hide-crash-restore-bubble", "about:blank"]
        if mode == "offscreen":
            args.insert(1, "--window-position=-32000,-32000")
        else:
            args.insert(1, "--headless=new")
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7 if mode == "offscreen" else 0   # SW_SHOWMINNOACTIVE / SW_HIDE
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW)
        if not os.environ.get("BROWSER_NO_JOB"):
            _kill_with_us(self.proc)
        print(f"[browser] started {Path(exe).name} pid {self.proc.pid} mode {mode}")
        deadline = time.monotonic() + 20
        url = None
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"browser exited with code {self.proc.returncode}")
            try:
                lines = port_file.read_text().split()
                if len(lines) >= 2:
                    url = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                    break
            except OSError:
                pass
            await asyncio.sleep(0.1)
        if not url:
            raise RuntimeError("browser did not open its DevTools port")
        self.ws = await connect(url, max_size=None, ping_interval=None, open_timeout=10)
        self.reader = asyncio.create_task(self._read())
        await self.call("Target.setDiscoverTargets", {"discover": True})

    async def _stop(self) -> None:
        for t in list(self.targets.values()):
            t.closed = True
        self.sessions.clear()
        self.targets.clear()
        if self.ws is not None:
            try:
                await asyncio.wait_for(self.call("Browser.close"), 3)
            except Exception:
                pass
            try:
                await self.ws.close()
            except Exception:
                pass
        if self.reader:
            self.reader.cancel()
        self.ws = self.reader = None
        if self.proc is not None:
            proc, self.proc = self.proc, None
            # Off the event loop: a blocking wait here would freeze the video streams for 3 s.
            await asyncio.to_thread(self._reap, proc)
        for f in self.pending.values():
            if not f.done():
                f.set_exception(RuntimeError("browser stopped"))
        self.pending.clear()

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        if proc.poll() is None:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def call(self, method: str, params: dict | None = None, session: str | None = None):
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
            return await asyncio.wait_for(fut, 30)
        finally:
            self.pending.pop(mid, None)                 # timed out / cancelled calls must not pile up

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                if "id" in msg:
                    fut = self.pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RuntimeError(f"{msg['error'].get('message')}"))
                        else:
                            fut.set_result(msg.get("result", {}))
                    continue
                method, params = msg.get("method", ""), msg.get("params", {})
                sid = msg.get("sessionId")
                if sid and sid in self.sessions:
                    self.sessions[sid].on_event(method, params)
                elif method == "Target.targetInfoChanged":
                    self._target_info(params["targetInfo"])
                elif method == "Target.targetCreated":
                    self._target_info(params["targetInfo"])
        except Exception as e:
            print(f"[browser] CDP reader stopped: {e!r}")
        finally:
            for f in self.pending.values():
                if not f.done():
                    f.set_exception(RuntimeError("CDP connection lost"))
            self.pending.clear()
            # Browser crashed / was killed: drop its panels so they show "disconnected" and the
            # next E reconnects (which starts a new browser). Empty after a deliberate _stop().
            for tab in list(self.targets.values()):
                tab.closed = True
                asyncio.ensure_future(tab.drop_client())

    def _target_info(self, info: dict) -> None:
        tab = self.targets.get(info.get("targetId", ""))
        if tab:
            tab.on_target_info(info)
            return
        # A popup (target=_blank / window.open) from one of our tabs: load it in that tab instead.
        opener = self.targets.get(info.get("openerId", ""))
        if opener and info.get("type") == "page" and info["targetId"] not in self.adopted:
            url = info.get("url", "")
            if url and url != "about:blank":
                self.adopted.add(info["targetId"])          # targetInfoChanged repeats; adopt once
                opener.popups.discard(info["targetId"])
                asyncio.create_task(self._adopt_popup(opener, info["targetId"], url))
            elif info["targetId"] not in opener.popups:
                # Still blank: it gets its URL a moment later, or never (blocked / script-only
                # windows). Never leave it open: it would be invisible but could play sound.
                opener.popups.add(info["targetId"])
                asyncio.create_task(self._close_blank_popup(opener, info["targetId"]))

    async def _close_blank_popup(self, tab: "Tab", target_id: str) -> None:
        await asyncio.sleep(5)
        if target_id in tab.popups:
            tab.popups.discard(target_id)
            self.adopted.add(target_id)
            try:
                await self.call("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass

    async def _adopt_popup(self, tab: "Tab", target_id: str, url: str) -> None:
        try:
            await self.call("Target.closeTarget", {"targetId": target_id})
        except Exception:
            pass
        await tab.navigate(url)

    async def shutdown(self) -> None:
        async with self.lock:
            await self._stop()


BROWSER = Browser()


# ---------------------------------------------------------------- one tab per client
class Tab:
    def __init__(self, ws: WebSocket, home: str) -> None:
        self.client = ws
        self.home = home
        self.target = ""
        self.session = ""
        self.w, self.h, self.zoom = 1280, 720, 1.25
        self.active = False
        self.casting = False
        self.closed = False
        self.url = ""
        self.title = ""
        self.loading = False
        self.last_frame = 0.0
        self.frames = 0
        self.sent_info = None
        self.buttons = 0
        self.send_lock = asyncio.Lock()
        self.slot: dict | None = None               # newest unsent screencast frame
        self.pump: asyncio.Task | None = None
        self.nav_task: asyncio.Task | None = None
        self.popups: set[str] = set()               # blank popup targets opened by this tab
        self.watch: asyncio.Task | None = None

    async def call(self, method: str, params: dict | None = None):
        return await BROWSER.call(method, params, self.session)

    async def create(self) -> None:
        await BROWSER.ensure()
        r = await BROWSER.call("Target.createTarget", {"url": "about:blank", "newWindow": True,
                                                       "width": self.w, "height": self.h})
        self.target = r["targetId"]
        BROWSER.targets[self.target] = self
        r = await BROWSER.call("Target.attachToTarget", {"targetId": self.target, "flatten": True})
        self.session = r["sessionId"]
        BROWSER.sessions[self.session] = self
        await self.call("Page.enable")
        await self.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
        await self.apply_viewport()
        self.watch = asyncio.create_task(self._watch_title())

    async def _watch_title(self) -> None:
        """Edge sends no event when a script changes document.title (YouTube, chat pages,
        players...), so ask once a second while the panel is in use."""
        while not self.closed:
            await asyncio.sleep(1.0)
            if self.active and not self.closed:
                try:
                    info = (await BROWSER.call("Target.getTargetInfo", {"targetId": self.target}))["targetInfo"]
                except Exception:
                    continue
                if not self.closed:
                    self.on_target_info(info)

    async def apply_viewport(self) -> None:
        z = max(0.25, min(4.0, self.zoom))
        await self.call("Emulation.setDeviceMetricsOverride", {
            "width": round(self.w / z), "height": round(self.h / z), "deviceScaleFactor": z, "mobile": False})

    async def drop_client(self) -> None:
        try:
            await self.client.close(code=1011)
        except Exception:
            pass

    async def close(self) -> None:
        self.closed = True
        if self.watch:
            self.watch.cancel()
        BROWSER.sessions.pop(self.session, None)
        BROWSER.targets.pop(self.target, None)
        if BROWSER.alive:
            for t in [self.target, *self.popups]:
                if t:
                    try:
                        await BROWSER.call("Target.closeTarget", {"targetId": t})
                    except Exception:
                        pass
        self.popups.clear()

    # ---- screencast
    async def set_active(self, on: bool) -> None:
        self.active = on
        if on and not self.casting:
            self.casting = True
            try:
                await self.call("Page.startScreencast", {"format": "jpeg", "quality": 70, "maxWidth": self.w,
                                                         "maxHeight": self.h, "everyNthFrame": 1})
            except Exception as e:                   # e.g. mid-navigation; retried when loading stops
                self.casting = False
                print(f"[browser] startScreencast deferred: {e}")
        elif not on and self.casting:
            self.casting = False
            await self._safe("Page.stopScreencast")

    async def restart_cast(self) -> None:
        if self.casting:
            self.casting = False
            await self._safe("Page.stopScreencast")
        if self.active:
            await self.set_active(True)

    def _on_frame(self, params: dict) -> None:
        """Latest frame wins: a frame still waiting to be sent is acked and dropped, so the
        client gets at most MAX_FPS and never a backlog."""
        old = self.slot
        self.slot = params
        if old is not None:
            asyncio.create_task(self._ack(old))
        if self.pump is None or self.pump.done():
            self.pump = asyncio.create_task(self._pump())

    async def _ack(self, params: dict) -> None:
        if not self.closed:
            try:
                await self.call("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:
                pass

    async def _pump(self) -> None:
        while self.slot is not None and not self.closed:
            wait = 1.0 / MAX_FPS - (time.monotonic() - self.last_frame)
            if wait > 0:
                await asyncio.sleep(wait)
            params, self.slot = self.slot, None
            if params is None:
                break
            self.last_frame = time.monotonic()
            try:
                if self.casting:
                    data = base64.b64decode(params["data"])
                    async with self.send_lock:
                        await self.client.send_bytes(data)
                    self.frames += 1
            except Exception:
                pass
            await self._ack(params)

    # ---- events
    def on_event(self, method: str, p: dict) -> None:
        if method == "Page.screencastFrame":
            self._on_frame(p)
        elif method == "Page.frameNavigated":
            fr = p.get("frame", {})
            if not fr.get("parentId"):
                self.url = fr.get("url", self.url)
                self._info()
        elif method == "Page.navigatedWithinDocument":
            if p.get("frameId") == self.target:
                self.url = p.get("url", self.url)
                self._info()
                asyncio.create_task(self._refresh_title())
        elif method == "Page.frameStartedLoading" and p.get("frameId") == self.target:
            self.loading = True
            self._info()
        elif method == "Page.frameStoppedLoading" and p.get("frameId") == self.target:
            self.loading = False
            self._info()
            asyncio.create_task(self._refresh_title())
            if self.active and not self.casting:
                asyncio.create_task(self.set_active(True))
        elif method == "Page.domContentEventFired":
            asyncio.create_task(self._refresh_title())
        elif method == "Page.javascriptDialogOpening":
            asyncio.create_task(self._safe("Page.handleJavaScriptDialog", {"accept": True}))

    def on_target_info(self, info: dict) -> None:
        self.url = info.get("url", self.url)
        self.title = info.get("title", self.title)
        self._info()

    async def _refresh_title(self) -> None:
        for delay in (0.0, 1.0):                     # titles set by scripts arrive a bit later
            await asyncio.sleep(delay)
            if self.closed:
                return
            try:
                info = (await BROWSER.call("Target.getTargetInfo", {"targetId": self.target}))["targetInfo"]
            except Exception:
                return
            self.on_target_info(info)

    async def _safe(self, method: str, params: dict | None = None):
        try:
            return await self.call(method, params)
        except Exception as e:
            print(f"[browser] {method} failed: {e}")

    def _info(self) -> None:
        msg = {"type": "info", "url": self.url, "title": self.title or self.url, "loading": self.loading}
        if msg == self.sent_info or self.closed:
            return
        self.sent_info = msg
        asyncio.create_task(self._send_text(json.dumps(msg)))

    async def _send_text(self, text: str) -> None:
        try:
            async with self.send_lock:
                await self.client.send_text(text)
        except Exception:
            pass

    # ---- commands
    async def navigate(self, url: str) -> None:
        url = normalize_url(url, self.home)
        r = await self._safe("Page.navigate", {"url": url})
        if r and r.get("errorText"):
            print(f"[browser] navigate {url}: {r['errorText']}")

    def _go(self, url: str) -> None:
        """Page.navigate only answers once the server responds (seconds on a slow site); run it
        beside the command loop so clicks, Back and active on/off are not stuck behind it."""
        self.nav_task = asyncio.create_task(self.navigate(url))

    async def handle(self, m: dict) -> None:
        cmd = m.get("cmd")
        if cmd == "open":
            changed = False
            for k, attr, cast in (("w", "w", int), ("h", "h", int), ("zoom", "zoom", float)):
                if k in m and m[k] is not None:
                    v = cast(m[k])
                    if v != getattr(self, attr):
                        setattr(self, attr, v)
                        changed = True
            if changed:
                await self.apply_viewport()
                await self.restart_cast()
            self._go(m.get("url") or self.home)
        elif cmd == "mouse":
            z = max(0.25, self.zoom)
            x, y = float(m.get("x", 0)) / z, float(m.get("y", 0)) / z
            btn = m.get("button", "left")
            bit = 2 if btn == "right" else 1
            t = m.get("type", "move")
            if t == "wheel":
                await self._safe("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": x, "y": y,
                                 "deltaX": float(m.get("dx", 0)), "deltaY": float(m.get("dy", 0))})
                return
            if t == "down":
                self.buttons |= bit
            p = {"type": {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased"}.get(t, "mouseMoved"),
                 "x": x, "y": y, "buttons": self.buttons}
            if t in ("down", "up"):
                p["button"] = btn
                p["clickCount"] = 1
            elif self.buttons:
                p["button"] = "left" if self.buttons & 1 else "right"
            if t == "up":
                self.buttons &= ~bit
                p["buttons"] = self.buttons
            await self._safe("Input.dispatchMouseEvent", p)
        elif cmd == "text":
            if m.get("text"):
                await self._safe("Input.insertText", {"text": str(m["text"])})
        elif cmd == "key":
            for p in key_params(str(m.get("key", "")), bool(m.get("down", True)), int(m.get("mods", 0))):
                await self._safe("Input.dispatchKeyEvent", p)
        elif cmd == "nav":
            a = m.get("action")
            if a in ("back", "forward"):
                h = await self._safe("Page.getNavigationHistory")
                if h:
                    i = h["currentIndex"] + (-1 if a == "back" else 1)
                    if 0 <= i < len(h["entries"]):
                        await self._safe("Page.navigateToHistoryEntry", {"entryId": h["entries"][i]["id"]})
            elif a == "reload":
                await self._safe("Page.reload")
            elif a == "home":
                self._go(self.home)
        elif cmd == "active":
            await self.set_active(bool(m.get("on", True)))


@router.websocket("/ws/browser")
async def ws_browser(ws: WebSocket):
    await ws.accept()
    host = ws.url.hostname or "127.0.0.1"
    home = f"http://{host}:{ws.url.port or 8090}/start"
    custom = str(load_library().get("browser_home") or "")
    if custom and not custom.rstrip("/").endswith("/start"):   # library.json may set its own home page
        home = custom
    tab = Tab(ws, home)
    try:
        await tab.create()
    except Exception as e:
        print(f"[browser] could not open a tab: {e!r}")
        await tab.close()                                # a half-made tab must not stay open
        try:
            await ws.send_text(json.dumps({"type": "error", "message": str(e) or type(e).__name__}))
            await ws.close()
        except Exception:
            pass
        return
    try:
        while True:
            raw = await ws.receive_text()
            try:
                m = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await tab.handle(m)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"[browser] panel connection error: {e!r}")
    finally:
        await tab.close()


@router.get("/search")
def search(q: str = ""):
    """The start page's search box (same engine as words typed in the address bar)."""
    return RedirectResponse(search_url(q) if q.strip() else "/start", status_code=302)


@router.get("/start")
def start_page():
    if START_HTML.is_file():
        return FileResponse(START_HTML, media_type="text/html")
    return HTMLResponse("<h1 style='font-size:48px'>Home Space browser</h1>")


async def shutdown() -> None:
    await BROWSER.shutdown()

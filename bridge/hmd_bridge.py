"""
Cross Reality HMD bridge: Windows laptop <-> Android phone (Chrome) over USB.

    phone Chrome ──WS /ws/pose──▶ bridge ──UDP 127.0.0.1:9100──▶ Godot (head orientation)
    phone Chrome ◀─WS /ws/video── bridge ◀── ffmpeg (screen capture → H.264, FLV over pipe)

Quick start (from the project folder):
    .venv\\Scripts\\python bridge\\hmd_bridge.py --adb --open /check          # Phase 0: sensor check
    .venv\\Scripts\\python bridge\\hmd_bridge.py --adb --open / --video test  # Phase 1: test pattern
    .venv\\Scripts\\python bridge\\hmd_bridge.py --adb --open / --video game     # stream the running Godot game window

Video sources:  off | test (ffmpeg pattern) | game (the game window itself, recommended) | desktop (GDI screen region) | dda
Encoders:       qsv (Intel Quick Sync on the UHD 620, low CPU) | x264 (CPU fallback)
                Note: the GeForce MX130 has no NVENC, so h264_nvenc will not work on this laptop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

try:
    import uvicorn
    from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import FileResponse, JSONResponse
except ImportError:
    sys.exit("Missing packages. Run:  .venv\\Scripts\\python -m pip install -r bridge\\requirements.txt")

HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
REPORT_PATH = HERE / "sensor_report.json"

# UDP packet to Godot: qx, qy, qz, qw (float32) + phone timestamp ms (float64) = 24 bytes
POSE_PACKET = struct.Struct("<4fd")
RECENTER_PACKET = b"RECENTER"

# Video message types sent to the phone (first byte of each binary WS message)
MSG_CONFIG, MSG_KEY, MSG_DELTA = 0, 1, 2

ARGS: argparse.Namespace  # set in main()


# ── Pose relay ───────────────────────────────────────────────────────────────

class PoseRelay:
    def __init__(self, godot_addr: tuple[str, int]):
        self.addr = godot_addr
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.count = 0
        self.last_report = time.perf_counter()
        self.source = "?"
        self.last_q = (0.0, 0.0, 0.0, 1.0)

    def send_pose(self, q: list[float], t: float, source: str) -> None:
        self.sock.sendto(POSE_PACKET.pack(*q, t), self.addr)
        self.count += 1
        self.source = source
        self.last_q = tuple(q)

    def recenter(self) -> None:
        self.sock.sendto(RECENTER_PACKET, self.addr)
        print("\n[pose] recenter requested from phone")

    def maybe_print_rate(self) -> None:
        now = time.perf_counter()
        if now - self.last_report >= 2.0:
            rate = self.count / (now - self.last_report)
            q = ", ".join(f"{v:+.2f}" for v in self.last_q)
            print(f"\r[pose] {rate:5.1f} Hz  src={self.source:<8} q=({q})   ", end="", flush=True)
            self.count, self.last_report = 0, now


# ── Video pipeline (ffmpeg → FLV → per-frame messages) ───────────────────────

def find_ffmpeg() -> str | None:
    """PATH first, then winget's install folders (a terminal opened before the
    install, e.g. inside VS Code, still has the old PATH)."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    winget = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet"
    candidates = [winget / "Links" / "ffmpeg.exe"]
    candidates += sorted((winget / "Packages").glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"), reverse=True)
    candidates += [HERE.parent / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe", Path("C:/ffmpeg/bin/ffmpeg.exe")]
    return next((str(c) for c in candidates if c.is_file()), None)


def find_game_window(title_prefix: str) -> tuple[int, str]:
    """Find the running game's own window (Windows only).

    When run from the Godot editor, the editor's floating "Game" window has the same
    title, so skip windows from any process that also owns a '... - Godot Engine'
    editor window, and prefer the window with the smallest client area (the game itself).
    """
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    proto = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def text(h):
        n = user32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        return buf.value

    def pid(h):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        return p.value

    def area(h):
        r = wt.RECT()
        user32.GetClientRect(h, ctypes.byref(r))
        return r.right * r.bottom

    windows = []
    user32.EnumWindows(proto(lambda h, _: windows.append(h) or True), 0)
    visible = [h for h in windows if user32.IsWindowVisible(h)]
    editor_pids = {pid(h) for h in visible if text(h).endswith("Godot Engine")}
    matches = [h for h in visible if text(h).startswith(title_prefix) and area(h) > 0]
    games = [h for h in matches if pid(h) not in editor_pids] or matches
    if not games:
        raise RuntimeError(f"game window '{title_prefix}' not found - start the game (F5 in Godot) first")
    best = min(games, key=area)
    return int(best), text(best)


def ffmpeg_command(a: argparse.Namespace) -> list[str]:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install it with:  winget install Gyan.FFmpeg  (then reopen the terminal)")
    w, h = a.size
    fps = str(a.fps)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer", "-flags", "low_delay"]
    vf_prefix = ""

    if a.video == "test":
        cmd += ["-re", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}"]
    elif a.video == "desktop":
        # GDI capture: always works, but measured ~42 fps max at 1600x720 on this laptop
        x, y = a.offset
        cmd += ["-f", "gdigrab", "-framerate", fps, "-draw_mouse", "0",
                "-offset_x", str(x), "-offset_y", str(y), "-video_size", f"{w}x{h}", "-i", "desktop"]
    elif a.video == "dda":
        # Desktop Duplication: faster, lower CPU. Needs an interactive desktop session.
        x, y = a.offset
        cmd += ["-f", "lavfi", "-i", f"ddagrab=output_idx=0:framerate={fps}:video_size={w}x{h}"
                                     f":offset_x={x}:offset_y={y}:draw_mouse=0"]
        vf_prefix = "hwdownload,format=bgra,"
    elif a.video == "game":
        # Windows Graphics Capture of the game window itself: works when the window is
        # covered or moved, ignores display scaling. Measured ~53 fps on this laptop.
        hwnd, title = find_game_window(a.title)
        print(f"\n[video] capturing window {hwnd:#x} '{title}'")
        cmd += ["-f", "lavfi", "-i", f"gfxcapture=hwnd={hwnd}:capture_cursor=0:max_framerate={fps}"]
        vf_prefix = "hwdownload,format=bgra,"
    else:
        raise ValueError(a.video)

    gop = str(a.fps)  # one keyframe per second → fast recovery after dropped frames
    bitrate = a.bitrate
    if a.encoder == "qsv":
        cmd += ["-vf", f"{vf_prefix}scale={w}:{h},format=nv12",
                "-c:v", "h264_qsv", "-preset", "veryfast", "-profile:v", "main",
                "-bf", "0", "-g", gop, "-async_depth", "1",
                "-b:v", bitrate, "-maxrate", bitrate]
    else:
        cmd += ["-vf", f"{vf_prefix}scale={w}:{h},format=yuv420p",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "main",
                "-bf", "0", "-g", gop, "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate]

    # FLV gives exact per-frame framing + keyframe flags, so frames go out as soon as they're encoded
    cmd += ["-an", "-f", "flv", "-flvflags", "no_duration_filesize", "-flush_packets", "1", "pipe:1"]
    return cmd


def read_exact(stream, n: int) -> bytes:
    # The pipe is unbuffered: one read() may return only part of a large keyframe
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return bytes(buf)


def flv_reader(proc: subprocess.Popen, emit) -> None:
    """Runs in a thread. Parses FLV tags from ffmpeg stdout, calls emit(kind, payload)."""
    out = proc.stdout
    try:
        header = read_exact(out, 9)
        if header[:3] != b"FLV":
            raise RuntimeError("ffmpeg did not produce FLV output")
        data_offset = int.from_bytes(header[5:9], "big")       # normally 9
        read_exact(out, max(data_offset - 9, 0) + 4)            # rest of header + PreviousTagSize0
        while True:
            tag = read_exact(out, 11)
            size = int.from_bytes(tag[1:4], "big")
            body = read_exact(out, size)
            read_exact(out, 4)  # PreviousTagSize
            if tag[0] & 0x1F != 9 or size < 5 or body[0] & 0x0F != 7:
                continue  # not an H.264 video tag
            frame_type, packet_type = body[0] >> 4, body[1]
            payload = body[5:]
            if packet_type == 0:
                emit(MSG_CONFIG, payload)            # AVCDecoderConfigurationRecord (avcC)
            elif packet_type == 1 and payload:
                emit(MSG_KEY if frame_type == 1 else MSG_DELTA, payload)  # length-prefixed NAL units
    except (EOFError, ValueError, OSError):
        pass
    finally:
        emit(None, b"")


async def stream_video(ws: WebSocket) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=4)
    state = {"need_key": False, "dropped": 0, "sent": 0}

    def deliver(kind, payload):  # runs on the event loop thread
        if kind is None:                   # end of stream: must always get through
            while queue.full():
                queue.get_nowait()
            queue.put_nowait(None)
            return
        if kind == MSG_DELTA and state["need_key"]:
            state["dropped"] += 1
            return
        if queue.full():
            if kind == MSG_DELTA:          # phone is behind: skip to the next keyframe
                state["need_key"] = True
                state["dropped"] += 1
                return
            while not queue.empty():       # keyframe/config: throw away stale frames
                queue.get_nowait()
                state["dropped"] += 1
        if kind == MSG_KEY:
            state["need_key"] = False
        queue.put_nowait(bytes([kind]) + payload)

    def emit(kind, payload):  # runs on the reader thread
        loop.call_soon_threadsafe(deliver, kind, payload)

    try:
        cmd = ffmpeg_command(ARGS)
    except RuntimeError as e:
        print(f"\n[video] {e}")
        await ws.send_text(json.dumps({"error": str(e)}))
        return

    print(f"\n[video] starting: {ARGS.video} {ARGS.size[0]}x{ARGS.size[1]}@{ARGS.fps} via {ARGS.encoder}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, bufsize=0)
    threading.Thread(target=flv_reader, args=(proc, emit), daemon=True).start()
    t0 = time.perf_counter()
    try:
        while True:
            msg = await queue.get()
            if msg is None:
                print(f"\n[video] ffmpeg stopped (exit code {proc.poll()})")
                break
            await ws.send_bytes(msg)
            state["sent"] += 1
            if time.perf_counter() - t0 >= 5:
                print(f"\n[video] sent {state['sent'] / (time.perf_counter() - t0):.1f} fps, "
                      f"dropped {state['dropped']}")
                t0, state["sent"], state["dropped"] = time.perf_counter(), 0, 0
    except Exception:  # disconnect, keepalive timeout, closed socket
        print("\n[video] phone disconnected")
    finally:
        proc.kill()


# ── Web app ──────────────────────────────────────────────────────────────────

relay: PoseRelay


class PhoneControl:
    """Drives the phone over adb so you never take the headset off:
    the page asks for a tap when it needs fullscreen, and the bridge taps for it."""

    def __init__(self):
        self.adb: str | None = None
        self.serial: str | None = None
        self.last_tap = 0.0
        self.pending_tap = False

    def connect(self) -> None:  # runs in a thread
        import adb_setup
        self.serial = adb_setup.setup(ARGS.port, ARGS.open, ARGS.sensors)
        self.adb = adb_setup.find_adb() if self.serial else None
        if self.pending_tap and not ARGS.open:   # a fresh page will ask again by itself
            self.tap("page was waiting")
        self.pending_tap = False

    def tap(self, reason: str, force: bool = False) -> None:
        if not (self.adb and self.serial):
            self.pending_tap = True
            return
        if not force and time.time() - self.last_tap < 1.5:
            return
        self.last_tap = time.time()
        import adb_setup
        threading.Thread(target=adb_setup.tap, args=(self.adb, self.serial), daemon=True).start()
        print(f"\n[phone] tapped the screen ({reason})")

    def reopen(self) -> None:
        if self.adb and self.serial:
            import adb_setup
            threading.Thread(target=adb_setup.open_page,
                             args=(self.adb, self.serial, ARGS.port, ARGS.open or "/"), daemon=True).start()


phone = PhoneControl()

# Lens/timewarp settings from Godot (UDP 9101) -> every connected phone page
LENS: dict = {}
POSE_CLIENTS: set = set()


class LensSettingsReceiver(asyncio.DatagramProtocol):
    def datagram_received(self, data, addr):
        global LENS
        try:
            new = json.loads(data.decode("utf-8"))
        except ValueError:
            return
        if new != LENS:
            LENS = new
            for ws in list(POSE_CLIENTS):
                asyncio.ensure_future(send_lens(ws))


async def send_lens(ws) -> None:
    try:
        await ws.send_text(json.dumps({"lens": LENS}))
    except Exception:
        POSE_CLIENTS.discard(ws)


def console_commands() -> None:
    """Keys typed in the bridge window (headset stays on)."""
    print("Bridge keys:  Enter = tap phone (fullscreen)   r + Enter = reload phone page"
          "   c + Enter = recenter view")
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "":
            phone.tap("Enter pressed", force=True)
        elif cmd == "r":
            phone.reopen()
        elif cmd == "c":
            relay.recenter()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    if ARGS.adb:
        # Port forward + open page after the server is listening
        loop = asyncio.get_running_loop()
        loop.call_later(0.5, lambda: threading.Thread(target=phone.connect, daemon=True).start())
        threading.Thread(target=console_commands, daemon=True).start()
    await asyncio.get_running_loop().create_datagram_endpoint(
        LensSettingsReceiver, local_addr=("127.0.0.1", ARGS.settings_port))
    print(f"Bridge on http://localhost:{ARGS.port}  |  pose -> udp://{ARGS.godot_host}:{ARGS.godot_port}"
          f"  |  video: {ARGS.video}")
    yield


app = FastAPI(lifespan=lifespan)
NO_CACHE = {"Cache-Control": "no-store"}

# --wifi: the bridge is reachable by everything on the home network, so the stream, the pose
# input and the report need a secret key (?k=...) that only the printed URL carries. It is kept
# in phone_host/cert/bridge_token.txt (git-ignored) so a home-screen app keeps working.
ACCESS_KEY: str | None = None
VIDEO_WS = None      # only one video viewer at a time (each one costs an ffmpeg + encoder)


def _key_ok(conn) -> bool:
    if ACCESS_KEY is None:
        return True
    import hmac
    given = conn.query_params.get("k", "")
    if not hmac.compare_digest(given.encode(), ACCESS_KEY.encode()):
        return False
    origin = conn.headers.get("origin")
    host = conn.headers.get("host", "")
    return origin is None or origin == f"https://{host}"   # no cross-site WebSocket use


def load_access_key() -> str:
    import secrets
    path = HERE.parent / "phone_host" / "cert" / "bridge_token.txt"
    try:
        key = path.read_text(encoding="utf-8").strip()
        if len(key) >= 16:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(16)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key, encoding="utf-8")
    return key


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "hmd.html", headers=NO_CACHE)


def _phone_host():
    """phone_host/server.py: certificate + icon helpers shared with the Wi-Fi test server."""
    sys.path.insert(0, str(HERE.parent / "phone_host"))
    import server
    return server


@app.get("/manifest.webmanifest")
async def manifest(request: Request):
    # "Add to Home Screen" opens the page as an app: full screen, landscape (needs a trusted cert on iOS)
    from fastapi.responses import Response
    if not _key_ok(request):
        return Response(status_code=403)
    start = f"/?k={ACCESS_KEY}" if ACCESS_KEY else "/"
    body = {"name": "Cross Reality HMD", "short_name": "HMD", "start_url": start, "scope": "/",
            "display": "fullscreen", "orientation": "landscape", "background_color": "#000000",
            "theme_color": "#000000",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
    return Response(json.dumps(body), media_type="application/manifest+json")


@app.get("/icon-{size}.png")
async def icon(size: int):
    from fastapi.responses import Response
    if size not in (180, 192, 512):
        return Response(status_code=404)
    return Response(_phone_host().make_icon(size), media_type="image/png")


@app.get("/ca.crt")
async def ca_crt():
    # Install once on a phone (and trust it) to drop the certificate warnings for good
    from fastapi.responses import Response
    return Response(_phone_host().ca_der(), media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="cross-reality-home-ca.crt"'})


@app.get("/check")
async def check():
    return FileResponse(WEB_DIR / "check.html", headers=NO_CACHE)


@app.get("/api/config")
async def config():
    return {"video": ARGS.video, "width": ARGS.size[0], "height": ARGS.size[1], "fps": ARGS.fps}


@app.post("/api/report")
async def report(request: Request):
    if not _key_ok(request):
        return JSONResponse({"error": "missing or wrong access key"}, status_code=403)
    data = await request.json()
    REPORT_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print("\n" + "=" * 60 + "\nSENSOR CHECK REPORT from phone")
    for line in data.get("summary", []):
        print("  " + line)
    print(f"(full report saved to {REPORT_PATH})\n" + "=" * 60)
    return JSONResponse({"saved": str(REPORT_PATH)})


@app.websocket("/ws/pose")
async def ws_pose(ws: WebSocket):
    if not _key_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    print("\n[pose] phone connected")
    POSE_CLIENTS.add(ws)
    if LENS:
        await send_lens(ws)
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if "q" in msg:
                relay.send_pose(msg["q"], float(msg.get("t", 0.0)), msg.get("src", "?"))
                relay.maybe_print_rate()
            elif msg.get("cmd") == "recenter":
                relay.recenter()
            elif msg.get("cmd") == "need_tap":
                phone.tap("page needs a tap for fullscreen")
    except Exception:  # disconnect, keepalive timeout, bad message
        print("\n[pose] phone disconnected")
    finally:
        POSE_CLIENTS.discard(ws)


@app.websocket("/ws/video")
async def ws_video(ws: WebSocket):
    global VIDEO_WS
    if not _key_ok(ws):
        await ws.close(code=1008)
        return
    await ws.accept()
    old, VIDEO_WS = VIDEO_WS, ws       # a reconnecting phone replaces its old stream
    if old is not None:
        try:
            await old.close()
        except Exception:
            pass
    if ARGS.video == "off":
        await ws.send_text(json.dumps({"error": "video is off (start the bridge with --video game|test|desktop|dda)"}))
        await ws.close()
        return
    try:
        await stream_video(ws)
    finally:
        if VIDEO_WS is ws:
            VIDEO_WS = None


# ── CLI ──────────────────────────────────────────────────────────────────────

def parse_pair(text: str, sep: str) -> tuple[int, int]:
    a, b = text.lower().split(sep)
    return int(a), int(b)


def main() -> None:
    global ARGS, relay
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--adb", action="store_true", help="set up the USB port forward with adb on start")
    ap.add_argument("--open", metavar="PATH", help="with --adb: page to open on the phone (/ or /check)")
    ap.add_argument("--sensors", action="store_true", help="with --adb: print the phone's hardware sensors")
    ap.add_argument("--godot-host", default="127.0.0.1")
    ap.add_argument("--godot-port", type=int, default=9100)
    ap.add_argument("--settings-port", type=int, default=9101, help="UDP port for lens settings from Godot")
    ap.add_argument("--video", choices=["off", "test", "game", "desktop", "dda"], default="off")
    ap.add_argument("--encoder", choices=["qsv", "x264"], default="qsv")
    ap.add_argument("--size", type=lambda s: parse_pair(s, "x"), default=(1600, 720),
                    help="stream size WxH (default 1600x720; match the phone's screen)")
    ap.add_argument("--offset", type=lambda s: parse_pair(s, ","), default=(0, 0),
                    help="desktop capture top-left X,Y")
    ap.add_argument("--title", default="Cross Reality HMD", help="game window title prefix for --video game")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--bitrate", default="8M")
    ap.add_argument("--wifi", action="store_true",
                    help="no cable: serve HTTPS on the home Wi-Fi (default port 8444) with the phone_host certificate")
    ARGS = ap.parse_args()
    if ARGS.wifi and ARGS.port == 8080:
        ARGS.port = 8444

    sys.path.insert(0, str(HERE))
    relay = PoseRelay((ARGS.godot_host, ARGS.godot_port))
    tls = {}
    host = "127.0.0.1"
    if ARGS.wifi:
        # Same self-signed certificate as the phone-hosted test, so a phone that accepted it
        # there only has to accept it once more for this port (browsers trust per host:port).
        phone_host_server = _phone_host()
        ips = phone_host_server.detect_ips()
        phone_host_server.ensure_cert(ips)
        tls = dict(ssl_certfile=str(phone_host_server.CERT_FILE), ssl_keyfile=str(phone_host_server.KEY_FILE))
        host = "0.0.0.0"
        global ACCESS_KEY
        ACCESS_KEY = load_access_key()
        print("=" * 64)
        print("  WIRELESS: open on the phone (same Wi-Fi) - the key in the address is required:")
        for ip in ips:
            print(f"     https://{ip}:{ARGS.port}/?k={ACCESS_KEY}")
        print("  First time: accept the certificate warning, then tap the start button yourself.")
        print(f"  No more warnings + iPhone home-screen app full screen: open https://{ips[0] if ips else '<ip>'}:{ARGS.port}/ca.crt")
        print("  on the phone once and trust it - see phone_host/README.md 'Trust this laptop'.")
        print("=" * 64)
    uvicorn.run(app, host=host, port=ARGS.port, log_level="warning",
                ws_ping_interval=20, ws_ping_timeout=60, **tls)  # tolerate phone hiccups


if __name__ == "__main__":
    main()

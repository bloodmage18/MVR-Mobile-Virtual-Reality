"""Video module for media_service.py: headless ffmpeg decode -> raw RGB frames over a WebSocket,
audio on the laptop's default output (the earbuds). No windows are opened on the laptop.

    GET /api/videos  -> {"videos": [{"name", "path", "size", "duration"}...]}
    WS  /ws/video    client -> server (JSON text):
                        {"cmd":"open","path":..., "max_w":1280, "autoplay":true}
                        {"cmd":"play"} {"cmd":"pause"} {"cmd":"stop"}
                        {"cmd":"seek","t":seconds} {"cmd":"seek_rel","dt":seconds} {"cmd":"volume","v":0..1}
                     server -> client:
                        text   {"type":"status","state":"playing|paused|stopped|ended|error","t","duration",
                                "w","h","fps","name","path","volume","error"}   (~4 Hz and on every change)
                        binary raw RGB8 frame, w*h*3 bytes (w, h from the last status; both even)
    Optional TCP frame channel: open with "transport":"tcp" and the status gains "frame_port".
    Connect a plain TCP socket to 127.0.0.1:frame_port; while it is connected, frames go there
    instead of the WebSocket, each as a 16-byte header b"VFRM" + uint32 w, h, len (little endian)
    followed by len bytes of RGB8. Godot's WebSocketPeer needs ~50 ms to take in one 2.7 MB
    message (it reads in 4 KB pieces), so the Godot station uses this channel; a plain
    StreamPeerTCP read costs a few ms per frame.

Pipeline per play segment (restarted with -ss on play/seek/resume, killed on pause/stop):
    ffmpeg -ss t -i file -an -vf fps,scale -f rawvideo rgb24 pipe   -> video reader thread
    ffmpeg -ss t -i file -vn -f s16le pipe -> sounddevice stream    (or ffplay -nodisp as fallback)
The audio stream is the master clock: the video thread paces each frame against the time the
sound card is actually playing, so picture and sound stay in sync. Frames that are late are
dropped, and the sender only ever holds the newest frame (at most ~2 frames in flight).
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from media_service import CACHE, find_ffmpeg, find_ffplay, load_library

router = APIRouter()

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
SAMPLES = CACHE / "samples"
SAMPLE_FILE = SAMPLES / "test_pattern_720p.mp4"
PROBE_CACHE_FILE = CACHE / "video_probe.json"
MAX_FPS = 30.0                       # upload cap (the laptop also renders + encodes)
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW

try:                                  # preferred audio path: PCM into the default device, audio = clock
    import numpy as np
    import sounddevice as sd
except Exception:                     # noqa: BLE001  -> ffplay fallback
    np = sd = None

_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()
_sessions: set["Session"] = set()


# ---------------------------------------------------------------- helpers

def _spawn(args: list[str], **kw) -> subprocess.Popen:
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=NO_WINDOW, **kw)
    with _children_lock:
        _children.add(p)
    return p


def _kill(p: subprocess.Popen | None) -> None:
    if p is None:
        return
    try:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=3)
    except Exception:  # noqa: BLE001
        pass
    for s in (p.stdout,):
        try:
            if s:
                s.close()
        except Exception:  # noqa: BLE001
            pass
    with _children_lock:
        _children.discard(p)


def _ffprobe() -> str | None:
    ff = find_ffmpeg()
    if not ff:
        return None
    p = Path(ff).with_name("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
    return str(p) if p.is_file() else None


_probe_cache: dict | None = None
_probe_lock = threading.Lock()


def _load_probe_cache() -> dict:
    global _probe_cache
    if _probe_cache is None:
        try:
            _probe_cache = json.loads(PROBE_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            _probe_cache = {}
    return _probe_cache


def probe(path: str) -> dict:
    """{"duration", "w", "h", "fps", "audio"} via ffprobe, cached by (path, size, mtime)."""
    st = os.stat(path)
    key = os.path.normcase(os.path.abspath(path))
    with _probe_lock:
        cache = _load_probe_cache()
        hit = cache.get(key)
        if hit and hit.get("size") == st.st_size and hit.get("mtime") == st.st_mtime:
            return hit["info"]
    info = {"duration": 0.0, "w": 0, "h": 0, "fps": 30.0, "audio": False}
    exe = _ffprobe()
    if exe:
        try:
            out = subprocess.run([exe, "-v", "error", "-print_format", "json", "-show_format",
                                  "-show_streams", path], capture_output=True, timeout=20,
                                 creationflags=NO_WINDOW).stdout
            data = json.loads(out or b"{}")
            info["duration"] = float(data.get("format", {}).get("duration") or 0.0)
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" and not info["w"] and \
                        not (s.get("disposition") or {}).get("attached_pic"):
                    info["w"], info["h"] = int(s.get("width") or 0), int(s.get("height") or 0)
                    for k in ("avg_frame_rate", "r_frame_rate"):
                        num, _, den = str(s.get(k) or "0/0").partition("/")
                        try:
                            f = float(num) / float(den or 1)
                        except (ValueError, ZeroDivisionError):
                            f = 0.0
                        if 1.0 <= f <= 240.0:
                            info["fps"] = f
                            break
                    if not info["duration"]:
                        info["duration"] = float(s.get("duration") or 0.0)
                elif s.get("codec_type") == "audio":
                    info["audio"] = True
        except Exception as e:  # noqa: BLE001
            print(f"[video] ffprobe failed for {path}: {e!r}")
    with _probe_lock:
        cache = _load_probe_cache()
        cache[key] = {"size": st.st_size, "mtime": st.st_mtime, "info": info}
        try:
            CACHE.mkdir(exist_ok=True)
            PROBE_CACHE_FILE.write_text(json.dumps(cache), encoding="utf-8")
        except OSError:
            pass
    return info


def ensure_sample() -> None:
    """30 s 1280x720 test pattern with a beeping tone, so there is always something to play."""
    if SAMPLE_FILE.is_file():
        return
    ff = find_ffmpeg()
    if not ff:
        return
    SAMPLES.mkdir(parents=True, exist_ok=True)
    tmp = SAMPLE_FILE.with_suffix(".tmp.mp4")
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=30",
                    "-f", "lavfi", "-i", "sine=frequency=440:beep_factor=4:duration=30",
                    "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-shortest", str(tmp)],
                   timeout=120, creationflags=NO_WINDOW)
    if tmp.is_file():
        tmp.replace(SAMPLE_FILE)


def _scan(folder: Path) -> list[Path]:
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            if Path(f).suffix.lower() in VIDEO_EXT and not f.endswith(".tmp.mp4"):
                found.append(Path(dirpath) / f)
    return found


def list_videos() -> list[dict]:
    files: list[Path] = []
    for folder in load_library().get("videos", []) or []:
        p = Path(folder)
        if p.is_dir():
            files += _scan(p)
    if not files:
        ensure_sample()
        if SAMPLES.is_dir():
            files = _scan(SAMPLES)
    out = []
    for f in files:
        try:
            info = probe(str(f))
            out.append({"name": f.stem, "path": str(f).replace("\\", "/"),
                        "size": f.stat().st_size, "duration": round(info["duration"], 2)})
        except OSError:
            continue
    return out


def _num(v, default: float = 0.0) -> float:
    """A finite float from client JSON (strings, null, NaN, inf -> default)."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


_SEEKS = ("seek", "seek_rel")


def _coalesce(msgs: list[dict]) -> list[dict]:
    """Merge runs of seek/seek_rel into one, so a held arrow key or seek spam restarts the
    decoders once instead of once per message (each restart costs ~0.1 s)."""
    out: list[dict] = []
    for m in msgs:
        c = m.get("cmd")
        prev = out[-1] if out else None
        if c in _SEEKS and prev is not None and prev.get("cmd") in _SEEKS:
            if c == "seek":
                out[-1] = m
            elif prev["cmd"] == "seek":
                out[-1] = {"cmd": "seek", "t": _num(prev.get("t")) + _num(m.get("dt"))}
            else:
                out[-1] = {"cmd": "seek_rel", "dt": _num(prev.get("dt")) + _num(m.get("dt"))}
        else:
            out.append(m)
    return out


def _fit(w: int, h: int, max_w: int) -> tuple[int, int]:
    if w <= 0 or h <= 0:
        w, h = 1280, 720
    max_w = max(64, int(max_w or 1280))
    if w > max_w:
        h = h * max_w / w
        w = max_w
    return max(2, int(w) // 2 * 2), max(2, int(round(h)) // 2 * 2)


# ---------------------------------------------------------------- audio

class Clock:
    """Media time. Follows the sound card while audio plays, else the wall clock (no jumps)."""

    def __init__(self, t0: float):
        self._anchor = t0 - time.monotonic()
        self.audio_pos: float | None = None   # (media time at cb, monotonic at cb) set by audio
        self._audio_mono = 0.0
        self.frozen = True                    # until audio produces its first samples (or gives up)

    def audio_update(self, media_t: float, mono: float) -> None:
        self.audio_pos, self._audio_mono = media_t, mono

    def now(self) -> float:
        m = time.monotonic()
        if self.audio_pos is not None:
            t = self.audio_pos + (m - self._audio_mono)
            self._anchor = t - m
            return t
        return self._anchor + m

    def audio_gone(self) -> None:
        self.now()
        self.audio_pos = None

    def unfreeze(self, t: float) -> None:
        self._anchor = t - time.monotonic()
        self.frozen = False


class SDAudio:
    """ffmpeg -> s16le PCM pipe -> sounddevice RawOutputStream (default output). Drives the Clock."""

    def __init__(self, path: str, t: float, volume: float, clock: Clock):
        self.vol = volume
        self.clock = clock
        self.t0 = t
        self.rate = int(sd.query_devices(kind="output")["default_samplerate"] or 48000)
        self.buf = bytearray()
        self.lock = threading.Lock()
        self.space = threading.Condition(self.lock)
        self.frames_out = 0
        self.eof = False
        self.stopped = False
        self.proc = _spawn([find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-ss", f"{t:.3f}", "-i", path, "-map", "0:a:0", "-vn", "-sn", "-dn",
                            "-f", "s16le", "-ac", "2", "-ar", str(self.rate), "pipe:1"],
                           stdout=subprocess.PIPE, bufsize=0)
        self.stream = None
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.stream = sd.RawOutputStream(samplerate=self.rate, channels=2, dtype="int16",
                                             callback=self._cb, latency="low")
            self.stream.start()
        except BaseException:
            self.stop()            # no output device etc.: don't leave the decoder + reader behind
            raise

    def _read(self) -> None:
        limit = self.rate * 4 // 2   # keep at most ~0.5 s decoded ahead
        while not self.stopped:
            try:
                chunk = self.proc.stdout.read(8192)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            with self.space:
                while len(self.buf) > limit and not self.stopped:
                    self.space.wait(0.2)
                self.buf += chunk
        self.eof = True

    def _cb(self, outdata, frames, tinfo, status) -> None:  # noqa: ANN001
        need = frames * 4
        with self.space:
            n = min(need, len(self.buf)) // 4 * 4
            data = bytes(self.buf[:n])
            del self.buf[:n]
            self.space.notify()
        if n:
            a = np.frombuffer(data, dtype=np.int16)
            if self.vol < 0.999:
                a = (a * self.vol).astype(np.int16)
            outdata[:n] = a.tobytes()
        if n < need:
            outdata[n:] = b"\x00" * (need - n)
        latency = max(0.0, float(tinfo.outputBufferDacTime - tinfo.currentTime)) if tinfo else 0.0
        if n:
            # the first sample of this buffer reaches the speaker `latency` seconds from now
            media_t = self.t0 + self.frames_out / self.rate
            self.clock.audio_update(media_t, time.monotonic() + latency)
            if self.clock.frozen:
                self.clock.frozen = False
            self.frames_out += n // 4
        elif self.eof and self.clock.audio_pos is not None:
            self.clock.audio_gone()            # sound ended before the picture: wall clock takes over

    def set_volume(self, v: float) -> None:
        self.vol = v

    def stop(self) -> None:
        self.stopped = True
        with self.space:
            self.space.notify_all()
        try:
            if self.stream is not None:
                self.stream.abort()
                self.stream.close()
        except Exception:  # noqa: BLE001
            pass
        _kill(self.proc)


class FFplayAudio:
    """Fallback: ffplay -nodisp, started at the same moment as the video. Wall clock drives video."""

    def __init__(self, path: str, t: float, volume: float, clock: Clock):
        self.path = path
        self.clock = clock
        self.vol = volume
        self.proc = None
        self._start(t)
        clock.unfreeze(t)

    def _start(self, t: float) -> None:
        exe = find_ffplay()
        if exe:
            self.proc = _spawn([exe, "-nodisp", "-autoexit", "-vn", "-sn", "-loglevel", "quiet",
                                "-volume", str(int(self.vol * 100)), "-ss", f"{t:.3f}", self.path],
                               stdout=subprocess.DEVNULL)

    def set_volume(self, v: float) -> None:   # ffplay has no live volume: restart at the current time
        self.vol = v
        _kill(self.proc)
        self._start(self.clock.now())

    def stop(self) -> None:
        _kill(self.proc)


# ---------------------------------------------------------------- session

class Session:
    def __init__(self, ws: WebSocket, loop: asyncio.AbstractEventLoop):
        self.ws = ws
        self.loop = loop
        self.send_lock = asyncio.Lock()
        self.cmd_lock = threading.RLock()
        self.frame_evt = asyncio.Event()
        self.status_evt = asyncio.Event()
        self.latest: tuple[int, bytes] | None = None
        self.gen = 0
        self.state = "stopped"
        self.error = ""
        self.path = ""
        self.name = ""
        self.info: dict = {}
        self.w = self.h = 0
        self.fps = 30.0
        self.t = 0.0
        self.volume = 1.0
        self.max_w = 1280
        self.video: subprocess.Popen | None = None
        self.vthread: threading.Thread | None = None
        self.audio = None
        self.clock: Clock | None = None
        self.stop_evt = threading.Event()
        self.frames_sent = 0
        self.tcp_listener: socket.socket | None = None
        self.tcp_conn: socket.socket | None = None
        self.tcp_port = 0
        self.tcp_slot: tuple[int, bytes, int, int] | None = None
        self.tcp_evt = threading.Event()
        self.tcp_lock = threading.Lock()
        self.closed = False

    # ---- optional TCP frame channel
    def ensure_tcp(self) -> None:
        if self.tcp_listener:
            return
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.bind(("127.0.0.1", 0))
        ls.listen(1)
        self.tcp_listener = ls
        self.tcp_port = ls.getsockname()[1]
        threading.Thread(target=self._tcp_accept, daemon=True).start()
        threading.Thread(target=self._tcp_send, daemon=True).start()

    def _tcp_accept(self) -> None:
        while not self.closed:
            try:
                c, _ = self.tcp_listener.accept()
            except OSError:
                return
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with self.tcp_lock:
                old, self.tcp_conn = self.tcp_conn, c
            if old:
                try:
                    old.close()
                except OSError:
                    pass

    def _tcp_send(self) -> None:
        while not self.closed:
            self.tcp_evt.wait(0.5)
            self.tcp_evt.clear()
            with self.tcp_lock:
                item, self.tcp_slot = self.tcp_slot, None
                conn = self.tcp_conn
            if not item or not conn or item[0] != self.gen:
                continue
            _, data, w, h = item
            try:
                conn.sendall(struct.pack("<4sIII", b"VFRM", w, h, len(data)))
                conn.sendall(data)              # blocks while the client is busy; newer frames
                self.frames_sent += 1           # replace the slot meanwhile (dropped, not queued)
            except OSError:
                with self.tcp_lock:
                    if self.tcp_conn is conn:
                        self.tcp_conn = None
                try:
                    conn.close()
                except OSError:
                    pass

    def _close_tcp(self) -> None:
        self.closed = True
        self.tcp_evt.set()
        for sck in (self.tcp_conn, self.tcp_listener):
            try:
                if sck:
                    sck.close()
            except OSError:
                pass
        self.tcp_conn = self.tcp_listener = None

    # ---- status
    def status(self) -> dict:
        clock = self.clock            # runs on the event loop while commands swap self.clock in a worker
        t = clock.now() if (self.state == "playing" and clock and not clock.frozen) else self.t
        dur = self.info.get("duration", 0.0)
        if dur:
            t = min(t, dur)
        return {"type": "status", "state": self.state, "t": round(max(0.0, t), 3), "duration": round(dur, 3),
                "w": self.w, "h": self.h, "fps": round(self.fps, 3), "name": self.name, "path": self.path,
                "volume": round(self.volume, 3), "error": self.error, "frame_port": self.tcp_port}

    def poke_status(self) -> None:
        self.loop.call_soon_threadsafe(self.status_evt.set)

    # ---- pipeline
    def _halt(self) -> None:
        """Kill the current decode segment (video + audio). Keeps self.t where it was shown."""
        self.stop_evt.set()
        if self.audio:
            self.audio.stop()
            self.audio = None
        _kill(self.video)
        self.video = None
        if self.vthread and self.vthread is not threading.current_thread():
            self.vthread.join(timeout=3)
        self.vthread = None
        if self.clock and not self.clock.frozen:
            self.t = max(0.0, self.clock.now())
        self.clock = None
        self.gen += 1
        self.latest = None

    def _start(self, t: float, single_frame: bool = False) -> None:
        self._halt()
        dur = self.info.get("duration", 0.0)
        if dur:
            t = min(max(0.0, t), max(0.0, dur - 0.25))
        self.t = t
        self.stop_evt = threading.Event()
        gen = self.gen
        vf = f"fps={self.fps:.6f},scale={self.w}:{self.h}:flags=bilinear"
        args = [find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-nostdin",
                "-ss", f"{t:.3f}", "-i", self.path, "-map", "0:v:0", "-an", "-sn", "-dn",
                "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo"]
        if single_frame:
            args += ["-frames:v", "1"]
        self.video = _spawn(args + ["pipe:1"], stdout=subprocess.PIPE, bufsize=0)
        self.clock = Clock(t)
        if not single_frame:
            if self.info.get("audio"):
                try:
                    self.audio = (SDAudio if sd else FFplayAudio)(self.path, t, self.volume, self.clock)
                except Exception as e:  # noqa: BLE001  (no audio device etc.: play silent)
                    print(f"[video] audio failed, playing without sound: {e!r}")
                    self.audio = None
            if not self.audio:
                self.clock.unfreeze(t)
        self.vthread = threading.Thread(target=self._video_loop, args=(self.video, self.clock, gen, t,
                                                                       self.stop_evt, single_frame),
                                        daemon=True)
        self.vthread.start()

    def _video_loop(self, proc, clock: Clock, gen: int, t0: float, stop: threading.Event,
                    single: bool) -> None:
        size = self.w * self.h * 3
        period = 1.0 / self.fps
        n = 0
        start_wait = time.monotonic()
        out = proc.stdout
        while not stop.is_set():
            buf = bytearray(size)
            mv = memoryview(buf)
            got = 0
            while got < size:
                try:
                    r = out.readinto(mv[got:])
                except (OSError, ValueError):
                    r = 0
                if not r:
                    break
                got += r
            if got < size:
                break
            target = t0 + n * period
            n += 1
            if single:
                self._publish(gen, bytes(buf))
                break
            # wait for the sound card to start (max 1.5 s, then the wall clock takes over)
            while clock.frozen and not stop.is_set():
                if time.monotonic() - start_wait > 1.5:
                    clock.unfreeze(t0)
                    break
                stop.wait(0.005)
            now = clock.now()
            if target > now:
                if stop.wait(target - now):
                    break
            elif now - target > 1.5 * period and n > 1:
                continue                       # late: drop this frame, decode the next one
            self.t = target
            self._publish(gen, bytes(buf))
        if not stop.is_set() and not single and gen == self.gen:
            threading.Thread(target=self._ended, args=(gen,), daemon=True).start()

    def _publish(self, gen: int, data: bytes) -> None:
        if self.tcp_conn is not None:
            with self.tcp_lock:
                self.tcp_slot = (gen, data, self.w, self.h)   # overwrites an unsent frame
            self.tcp_evt.set()
            return
        def put():
            if gen == self.gen:
                self.latest = (gen, data)      # overwrites an unsent frame: never queue
                self.frame_evt.set()
        self.loop.call_soon_threadsafe(put)

    def _ended(self, gen: int) -> None:
        with self.cmd_lock:
            if gen != self.gen or self.state != "playing":
                return
            dur = self.info.get("duration", 0.0)
            self._halt()
            self.t = dur or self.t
            self.state = "ended"
        self.poke_status()

    # ---- commands (run on the event loop; process start/stop is blocking but short)
    def cmd_open(self, path: str, max_w: int, autoplay: bool) -> None:
        self._halt()
        self.error = ""
        p = Path(path)
        if not p.is_file() or p.suffix.lower() not in VIDEO_EXT:
            self.state, self.error = "error", f"not a video file: {path}"
            return
        if not find_ffmpeg():
            self.state, self.error = "error", "ffmpeg not found"
            return
        self.path = str(p).replace("\\", "/")
        self.name = p.stem
        self.max_w = max_w
        self.info = probe(str(p))
        if not self.info.get("w"):
            self.state, self.error = "error", "no video stream"
            return
        self.w, self.h = _fit(self.info["w"], self.info["h"], max_w)
        self.fps = min(MAX_FPS, self.info.get("fps") or 30.0)
        self.t = 0.0
        if autoplay:
            self.state = "playing"
            self._start(0.0)
        else:
            self.state = "paused"
            self._start(0.0, single_frame=True)

    def cmd_play(self) -> None:
        if not self.path or self.state == "playing":
            return
        if self.state in ("ended", "stopped"):
            self.t = 0.0
        self.state = "playing"
        self._start(self.t)

    def cmd_pause(self) -> None:
        if self.state != "playing":
            return
        self._halt()
        self.state = "paused"

    def cmd_seek(self, t: float) -> None:
        if not self.path or self.state in ("stopped", "error"):
            return
        if self.state == "playing":
            self._start(t)
        else:
            if self.state == "ended":
                self.state = "paused"
            self._start(t, single_frame=True)   # show the frame at the new position

    def cmd_stop(self) -> None:
        self._halt()
        self.t = 0.0
        if self.state != "error":
            self.state = "stopped"

    def cmd_volume(self, v: float) -> None:
        self.volume = min(1.0, max(0.0, float(v)))
        if self.audio:
            self.audio.set_volume(self.volume)

    def current_t(self) -> float:
        clock = self.clock
        if self.state == "playing" and clock and not clock.frozen:
            return clock.now()
        return self.t

    def handle(self, msgs: list[dict]) -> None:
        """Run a batch of commands (already coalesced) in order; a bad one is skipped, not fatal."""
        with self.cmd_lock:
            if self.closed:                     # a batch that lost the race with close(): start nothing
                return
            for msg in msgs:
                try:
                    self._handle(msg)
                except Exception as e:  # noqa: BLE001
                    print(f"[video] bad command {msg!r}: {e!r}")
        self.poke_status()

    def _handle(self, msg: dict) -> None:
        c = msg.get("cmd")
        if c == "open" and msg.get("transport") == "tcp":
            self.ensure_tcp()
        if c == "open":
            self.cmd_open(str(msg.get("path", "")), int(_num(msg.get("max_w"), 1280) or 1280),
                          bool(msg.get("autoplay", True)))
        elif c == "play":
            self.cmd_play()
        elif c == "pause":
            self.cmd_pause()
        elif c == "toggle":
            self.cmd_pause() if self.state == "playing" else self.cmd_play()
        elif c == "seek":
            self.cmd_seek(_num(msg.get("t")))
        elif c == "seek_rel":
            self.cmd_seek(self.current_t() + _num(msg.get("dt")))
        elif c == "stop":
            self.cmd_stop()
        elif c == "volume":
            self.cmd_volume(_num(msg.get("v"), 1.0))

    def close(self) -> None:
        with self.cmd_lock:
            self.closed = True
            self._halt()
            self._close_tcp()
            self.state = "stopped"

    # ---- async senders
    async def _send_text(self, d: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(d))

    async def frame_sender(self) -> None:
        try:
            while True:
                await self.frame_evt.wait()
                self.frame_evt.clear()
                item, self.latest = self.latest, None
                if item and item[0] == self.gen:
                    async with self.send_lock:
                        await self.ws.send_bytes(item[1])
                    self.frames_sent += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  client went away mid-send: the receive loop ends the session
            pass

    async def status_sender(self) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(self.status_evt.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
                self.status_evt.clear()
                await self._send_text(self.status())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def command_worker(self, inbox: list[dict], ready: asyncio.Event) -> None:
        """Runs queued commands off the event loop (process start/kill blocks for a few ms).
        Everything that arrived while the previous batch ran is handled as one coalesced batch."""
        loop = asyncio.get_running_loop()
        while True:
            await ready.wait()
            ready.clear()
            batch = _coalesce(inbox[:])
            inbox.clear()
            if batch:
                await loop.run_in_executor(None, self.handle, batch)


# ---------------------------------------------------------------- routes

@router.get("/api/videos")
def api_videos():
    return {"videos": list_videos()}


@router.websocket("/ws/video")
async def ws_video(ws: WebSocket):
    await ws.accept()
    loop = asyncio.get_running_loop()
    s = Session(ws, loop)
    _sessions.add(s)
    inbox: list[dict] = []
    ready = asyncio.Event()
    tasks = [asyncio.create_task(s.frame_sender()), asyncio.create_task(s.status_sender()),
             asyncio.create_task(s.command_worker(inbox, ready))]
    try:
        while True:
            m = await ws.receive()
            if m.get("type") == "websocket.disconnect":
                break
            raw = m.get("text")
            if raw is None:                    # binary from the client: not part of the protocol
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                inbox.append(msg)
                ready.set()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await loop.run_in_executor(None, s.close)   # waits for a command still running in the pool
        _sessions.discard(s)


async def shutdown() -> None:
    for s in list(_sessions):
        s.close()
    with _children_lock:
        procs = list(_children)
    for p in procs:
        _kill(p)

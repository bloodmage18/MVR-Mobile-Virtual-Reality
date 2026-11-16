"""Phone-hosted VR test: the laptop only SERVES the files, the phone renders the scene.

Run with the project venv:
    .venv\\Scripts\\python.exe phone_host\\server.py

- https://<wifi-ip>:8443/   the WebGL scene (HTTPS: phone motion sensors need a secure context)
- http://<wifi-ip>:8080/    a tiny page that says "use https://...:8443" (for people who type http)
- wss://<wifi-ip>:8443/ws/status   phones report what they are doing; printed here every few seconds
- POST /api/status          same report, used when the phone's WebSocket fails (iOS + self-signed cert)

A self-signed certificate is made once in phone_host/cert/ and remade if the Wi-Fi IP changes.
Options: --port 8443 --http-port 8080 --ip 192.168.x.y (override detection) --regen-cert
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import ipaddress
import json
import socket
import struct
import sys
import time
import zlib
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.staticfiles import StaticFiles

HERE = Path(__file__).resolve().parent
WEB = HERE / "web"
GAME = HERE / "game_build"
import mimetypes
mimetypes.add_type("application/wasm", ".wasm")     # Windows' registry may not know .wasm
mimetypes.add_type("application/octet-stream", ".pck")
CERT_DIR = HERE / "cert"
CERT_FILE = CERT_DIR / "cert.pem"
KEY_FILE = CERT_DIR / "key.pem"
CA_FILE = CERT_DIR / "ca.pem"            # public: install on phones (served as /ca.crt)
CA_KEY_FILE = CERT_DIR / "ca_key.pem"    # PRIVATE: never share (git-ignored with cert/)

# ── Network: find the Wi-Fi IPv4 the phones can reach ────────────────────────
def _usable(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    if a.version != 4 or a.is_loopback or a.is_link_local:      # 127.*, 169.254.*
        return False
    if a in ipaddress.ip_network("172.16.0.0/12"):             # WSL / Hyper-V / Docker
        return False
    if a in ipaddress.ip_network("26.0.0.0/8"):                # Radmin VPN
        return False
    return True


def detect_ips() -> list[str]:
    """LAN IPv4s, best first: the adapter called Wi-Fi, then other private LAN addresses."""
    found: list[tuple[int, str]] = []
    try:
        import psutil
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            if name in stats and not stats[name].isup:
                continue
            for a in addrs:
                if a.family == socket.AF_INET and _usable(a.address):
                    low = name.lower()
                    rank = 0 if low in ("wi-fi", "wifi", "wlan") else 1 if ("wi-fi" in low or "wlan" in low or "wireless" in low) else 2
                    if not ipaddress.ip_address(a.address).is_private:
                        rank += 2
                    found.append((rank, a.address))
    except Exception:
        pass
    if not found:   # no psutil: ask the OS which source address it would use for the LAN
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("192.168.1.1", 9))
            ip = s.getsockname()[0]
            s.close()
            if _usable(ip):
                found.append((1, ip))
        except OSError:
            pass
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if _usable(ip):
                    found.append((3, ip))
        except OSError:
            pass
    out: list[str] = []
    for _, ip in sorted(found):
        if ip not in out:
            out.append(ip)
    return out


# ── Certificate ──────────────────────────────────────────────────────────────
def cert_sans(path: Path) -> tuple[set[str], set[str]]:
    from cryptography import x509
    cert = x509.load_pem_x509_certificate(path.read_bytes())
    ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return ({str(i) for i in ext.get_values_for_type(x509.IPAddress)},
            set(ext.get_values_for_type(x509.DNSName)))


CA_NETS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"]


def _load_ca():
    """Load the CA and refuse it unless it still is OUR constrained CA (files are on a shared drive)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    ca = x509.load_pem_x509_certificate(CA_FILE.read_bytes())
    key = serialization.load_pem_private_key(CA_KEY_FILE.read_bytes(), password=None)
    nc = ca.extensions.get_extension_for_class(x509.NameConstraints)   # raises if missing
    pub = serialization.PublicFormat.SubjectPublicKeyInfo
    if (not nc.critical or not nc.value.permitted_subtrees or nc.value.excluded_subtrees
            or ca.public_key().public_bytes(serialization.Encoding.DER, pub)
            != key.public_key().public_bytes(serialization.Encoding.DER, pub)):
        raise RuntimeError("local CA files do not match / lost their name constraints")
    return ca, key


def _lock_down_cert_dir() -> None:
    """The cert folder inherits 'Authenticated Users: modify' from the drive. Only this user may touch it."""
    import getpass
    import subprocess
    if sys.platform != "win32":
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        # 1) the folder: only this user, inherited by everything inside
        subprocess.run(["icacls", str(CERT_DIR), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:(OI)(CI)F", "/Q"],
                       capture_output=True, timeout=20, creationflags=flags)
        # 2) the files: drop their own entries and inherit the folder's (OI/CI flags are folder-only;
        #    applying them to files with /T leaves the files with NO access at all)
        for f in CERT_DIR.iterdir():
            subprocess.run(["icacls", str(f), "/reset", "/Q"], capture_output=True, timeout=20, creationflags=flags)
    except Exception as e:
        print(f"  (could not restrict {CERT_DIR} permissions: {e})")


def ensure_ca():
    """A small LOCAL certificate authority, made once on this laptop. Installing CA_FILE on a phone
    (and trusting it) makes the phone trust this laptop's server certificates: no warnings, and iOS
    home-screen apps work. Name constraints limit it to private home-network addresses and
    .local/localhost names, so even if its key leaked it could not impersonate real websites."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    if CA_FILE.exists() or CA_KEY_FILE.exists():
        try:
            return _load_ca()
        except Exception as e:
            # Never silently replace an existing CA (phones may trust it, or someone swapped it)
            raise SystemExit(f"  !! local CA in {CERT_DIR} is unusable ({e}).\n"
                             f"     If you did not change it, delete ca.pem, ca_key.pem, cert.pem and key.pem there and restart\n"
                             f"     (phones then need the new /ca.crt installed again).")
    CERT_DIR.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"Cross Reality home CA ({host})"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cross Reality Project Test")])
    now = dt.datetime.now(dt.timezone.utc)
    nets = CA_NETS
    constraints = x509.NameConstraints(
        permitted_subtrees=[x509.IPAddress(ipaddress.ip_network(n)) for n in nets] +
                           [x509.DNSName("local"), x509.DNSName("localhost"), x509.DNSName(host.lower())],
        excluded_subtrees=None)
    ca = (x509.CertificateBuilder()
          .subject_name(name).issuer_name(name)
          .public_key(key.public_key())
          .serial_number(x509.random_serial_number())
          .not_valid_before(now - dt.timedelta(days=1))
          .not_valid_after(now + dt.timedelta(days=3650))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, content_commitment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(constraints, critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
          .sign(key, hashes.SHA256()))
    CA_KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    CA_FILE.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    _lock_down_cert_dir()
    print(f"  made local certificate authority {CA_FILE}  (install it on phones: /ca.crt)")
    return ca, key


def ca_der() -> bytes:
    from cryptography.hazmat.primitives import serialization
    ca, _ = _load_ca()          # serve only; never create a CA from a web request
    return ca.public_bytes(serialization.Encoding.DER)


def ensure_cert(ips: list[str], force: bool = False) -> None:
    """Server certificate for every LAN IP, localhost and the hostname, signed by the local CA."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca, ca_key = ensure_ca()
    _lock_down_cert_dir()
    # The CA may only vouch for private addresses; one public IP in the list would void the whole
    # certificate on every phone, so leave those out.
    allowed = [ipaddress.ip_network(n) for n in CA_NETS]
    dropped = [i for i in ips if not any(ipaddress.ip_address(i) in n for n in allowed)]
    for i in dropped:
        print(f"  note: {i} is not a home-network address; not putting it in the certificate")
    want_ips = (set(ips) - set(dropped)) | {"127.0.0.1"}
    if CERT_FILE.exists() and KEY_FILE.exists() and not force:
        try:
            have_ips, _ = cert_sans(CERT_FILE)
            leaf = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
            expires = leaf.not_valid_after_utc
            try:
                leaf.verify_directly_issued_by(ca)
                signed_by_ca = True
            except Exception:
                signed_by_ca = False
            if not signed_by_ca:
                print("  certificate is not signed by the local CA (older version); making a new one")
            elif expires < dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=2):
                print(f"  certificate expires {expires:%Y-%m-%d}; making a new one")
            elif want_ips <= have_ips:
                return
            else:
                print(f"  certificate does not cover {sorted(want_ips - have_ips)}; making a new one")
        except Exception as e:  # unreadable -> regenerate
            print(f"  certificate unreadable ({e}); making a new one")

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    host = socket.gethostname()
    dns = sorted({"localhost", host.lower(), f"{host}.local".lower()})
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{host.lower()}.local"),
                      x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cross Reality Project Test")])
    now = dt.datetime.now(dt.timezone.utc)
    san = x509.SubjectAlternativeName(
        [x509.DNSName(d) for d in dns] +
        [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(want_ips)])
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=390))      # Apple rejects > 398 days
            .add_extension(san, critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False,
                                         content_commitment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    KEY_FILE.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                           serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    # Serve the chain (server cert + CA) so phones can build the path to the installed CA
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    print(f"  made server certificate {CERT_FILE}  (IPs {sorted(want_ips)}, names {dns}), signed by the local CA")


# ── Tiny PNG icon (for Add to Home Screen), no image library needed ──────────
def make_icon(size: int) -> bytes:
    rows = []
    c = size / 2
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            dx, dy = (x - c) / size, (y - c) / size
            r2 = dx * dx + dy * dy
            if 0.09 < r2 < 0.13:                     # ring
                px = (230, 230, 240)
            elif abs(dx) < 0.05 and dy < 0 and r2 < 0.09 and dy > -0.28:
                px = (220, 40, 40)                   # red "north" needle
            elif abs(dx) < 0.05 and dy >= 0 and r2 < 0.09:
                px = (40, 80, 220)                   # blue "south" needle
            elif abs(dy) < 0.05 and dx > 0 and r2 < 0.09:
                px = (40, 170, 60)                   # green east
            elif abs(dy) < 0.05 and dx < 0 and r2 < 0.09:
                px = (220, 190, 30)                  # yellow west
            else:
                px = (12, 16, 30)
            row += bytes(px)
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


# ── Phone status tracking ────────────────────────────────────────────────────
class Phones:
    def __init__(self) -> None:
        self.state: dict[str, dict] = {}     # id -> {"ip", "last", "msg", "printed", "via"}

    @staticmethod
    def label(msg: dict) -> str:
        ua = msg.get("ua", "")
        if "iPhone" in ua: return "iPhone"
        if "iPad" in ua: return "iPad"
        if "Android" in ua:
            model = ua.split("Android", 1)[1].split(")")[0].split(";")
            return ("Android " + model[-1].strip()).strip() if len(model) > 1 else "Android"
        if "Windows" in ua: return "Windows"
        return msg.get("platform") or "device"

    def update(self, pid: str, ip: str, msg: dict, via: str) -> None:
        now = time.time()
        st = self.state.get(pid)
        if st is None:
            st = self.state[pid] = {"ip": ip, "printed": 0.0, "first": now}
            ts = time.strftime("%H:%M:%S")
            print(f"[{ts}] + phone CONNECTED  {ip}  {self.label(msg)}  via {via}")
            print(f"           UA: {msg.get('ua', '?')}")
        st.update(last=now, msg=msg, via=via, ip=ip)
        if now - st["printed"] >= 4.0 or msg.get("event"):
            st["printed"] = now
            self.print_line(pid)

    def print_line(self, pid: str) -> None:
        st = self.state[pid]
        m = st["msg"]
        ts = time.strftime("%H:%M:%S")
        sensors = m.get("sensors", "?")
        pad = m.get("gamepad") or "no"
        extra = f"  event={m['event']}" if m.get("event") else ""
        if isinstance(m.get("lens"), dict):
            extra += f"  lens: separation={m['lens'].get('offset')} fov={m['lens'].get('fov')}"
        print(f"[{ts}] {st['ip']:<15} {self.label(m):<18} sensors={sensors:<34} "
              f"fps={m.get('fps', '?'):>3}  {'STEREO' if m.get('stereo') else 'mono  '}  "
              f"pad={pad}  orient={m.get('orientation', '?')}  link={st['via']}{extra}")

    def reap(self, max_age: float = 10.0) -> None:
        """Phones that report by POST never 'disconnect'; drop them after a silence."""
        now = time.time()
        for pid in [p for p, st in self.state.items() if st.get("via") != "wss" and now - st["last"] > max_age]:
            self.gone(pid)

    def gone(self, pid: str) -> None:
        st = self.state.pop(pid, None)
        if st:
            print(f"[{time.strftime('%H:%M:%S')}] - phone disconnected  {st['ip']}  {self.label(st.get('msg', {}))}")


phones = Phones()

# ── Lens tuning from the laptop keyboard (wear the headset, press keys here) ──
LIVE_WS: set = set()
LENS_KEYS = {
    "]": {"doffset": 0.005}, "[": {"doffset": -0.005},   # eye separation: images closer / further apart
    "=": {"dfov": 2}, "+": {"dfov": 2}, "-": {"dfov": -2},  # field of view
    "0": {"reset": True},
}


async def broadcast(msg: dict) -> int:
    sent = 0
    for ws in list(LIVE_WS):
        try:
            await ws.send_text(json.dumps(msg))
            sent += 1
        except Exception:
            LIVE_WS.discard(ws)
    return sent


def key_thread(loop: asyncio.AbstractEventLoop) -> None:
    """Windows console keys -> lens commands to every connected phone."""
    try:
        import msvcrt
    except ImportError:
        return
    while True:
        ch = msvcrt.getwch()
        cmd = LENS_KEYS.get(ch)
        if not cmd:
            continue
        fut = asyncio.run_coroutine_threadsafe(broadcast({"lens": cmd}), loop)
        try:
            n = fut.result(timeout=2)
        except Exception:
            n = 0
        print(f"  lens {cmd} -> {n} phone(s)" + ("" if n else "  (none connected over wss)"))


# ── App ──────────────────────────────────────────────────────────────────────
def build_app(urls: list[str]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        if request.url.path in ("/", "/index.html"):
            ip = request.client.host if request.client else "?"
            print(f"[{time.strftime('%H:%M:%S')}] page loaded by {ip}  ({request.headers.get('user-agent', '')[:90]})")
        return resp

    icons = {s: make_icon(s) for s in (180, 192, 512)}

    @app.get("/icon-{size}.png")
    async def icon(size: int):
        if size not in icons:
            return Response(status_code=404)
        return Response(icons[size], media_type="image/png")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return Response((WEB / "manifest.webmanifest").read_bytes(), media_type="application/manifest+json")

    @app.get("/ca.crt")
    async def ca_crt():
        return Response(ca_der(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="cross-reality-home-ca.crt"'})

    @app.get("/api/info")
    async def info():
        return {"urls": urls, "time": time.time()}

    @app.post("/api/status")
    async def status_post(request: Request):
        try:
            msg = await request.json()
        except Exception:
            return JSONResponse({"ok": False}, status_code=400)
        ip = request.client.host if request.client else "?"
        phones.update(str(msg.get("id", ip)), ip, msg, "https-post")
        return {"ok": True}

    @app.websocket("/ws/pad")
    async def ws_pad(ws: WebSocket):
        """Laptop controller -> phone game (see pad_relay.py)."""
        import pad_relay
        await ws.accept()
        ip = ws.client.host if ws.client else "?"
        print(f"[{time.strftime('%H:%M:%S')}] [pad] phone game {ip} listening for the laptop controller")
        seen, last_sent = -1, 0.0
        try:
            while True:
                ver, msg = pad_relay.relay.snapshot()
                now = time.time()
                if ver != seen or now - last_sent > 0.5:
                    await ws.send_text(msg)
                    seen, last_sent = ver, now
                await asyncio.sleep(1 / 120)
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception as e:  # connection reset etc.
            print(f"[pad] {ip}: {e!r}")

    @app.websocket("/ws/status")
    async def ws_status(ws: WebSocket):
        await ws.accept()
        LIVE_WS.add(ws)
        ip = ws.client.host if ws.client else "?"
        pid = None
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except ValueError:
                    print(f"[{time.strftime('%H:%M:%S')}] {ip} sent non-JSON: {raw[:80]!r}")
                    continue
                pid = str(msg.get("id", ip))
                phones.update(pid, ip, msg, "wss")
                await ws.send_text(json.dumps({"ok": True, "t": time.time()}))
        except WebSocketDisconnect:
            pass
        finally:
            LIVE_WS.discard(ws)
            if pid:
                phones.gone(pid)

    # The school horror game as a web build (phone_host/build_game.bat makes it): the phone runs it
    GAME.mkdir(exist_ok=True)
    app.mount("/game", StaticFiles(directory=GAME, html=True), name="game")
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")
    return app


def build_http_app(https_port: int) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/ca.crt")   # over plain http so a phone can install it before trusting anything
    async def ca_crt():
        return Response(ca_der(), media_type="application/x-x509-ca-cert",
                        headers={"Content-Disposition": 'attachment; filename="cross-reality-home-ca.crt"'})

    @app.get("/{path:path}")
    async def hint(request: Request, path: str):
        host = (request.headers.get("host") or "localhost").split(":")[0]
        url = f"https://{host}:{https_port}/"
        print(f"[{time.strftime('%H:%M:%S')}] {request.client.host if request.client else '?'} used plain http -> told to open {url}")
        return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Use HTTPS</title>
<style>body{{background:#0b1020;color:#eee;font:22px system-ui,sans-serif;text-align:center;padding:40px 16px}}
a{{display:inline-block;margin-top:24px;padding:22px 30px;background:#2f81f7;color:#fff;border-radius:14px;
font-size:28px;text-decoration:none;word-break:break-all}} small{{display:block;margin-top:28px;color:#9aa}}</style></head>
<body><h1>Use HTTPS</h1><p>The motion sensors only work over a secure connection.<br>Open:</p>
<a href="{url}">{url}</a>
<small>Your phone will warn that the certificate is not trusted (it is self-signed on this laptop).<br>
iPhone: Show Details &rarr; visit this website. Android Chrome: Advanced &rarr; Proceed.</small></body></html>""")

    return app


def print_banner(urls: list[str], http_port: int) -> None:
    line = "=" * 64
    print(line)
    print("  PHONE-HOSTED VR TEST   (the phone renders; this laptop only serves files)")
    print(line)
    if not urls:
        print("  !! No Wi-Fi IPv4 found. Is the laptop on Wi-Fi? Try --ip 192.168.x.y")
    for i, u in enumerate(urls):
        print(("  OPEN ON THE PHONE:  " if i == 0 else "  or:                ") + u)
    print()
    if urls:
        main = urls[0]
        box = "#" * (len(main) + 12)
        print("  " + box)
        print("  ##    " + main + "    ##")      # boxed so it is easy to spot across the room
        print("  " + box)
        print()
        try:
            import qrcode   # optional
            qr = qrcode.QRCode(border=1)
            qr.add_data(main)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            print("  (no 'qrcode' package, so no QR code: type the address above into the phone)")
        except Exception as e:
            print(f"  (QR code failed: {e})")
        host = main.split("//")[1].split(":")[0]
        print(f"  Typed http by mistake? http://{host}:{http_port}/ points to the right address.")
    print()
    print("  First visit: the phone warns the certificate is not trusted (self-signed).")
    print("    iPhone Safari : Show Details > visit this website > Visit Website")
    print("    Android Chrome: Advanced > Proceed to ... (unsafe)")
    print("  If the phone cannot load the page at all: check the phone is on the same Wi-Fi,")
    print("    then Windows Firewall; see phone_host/README.md (Firewall).")
    print(line)
    print("  Waiting for phones... (Ctrl+C to stop)")
    sys.stdout.flush()


async def main_async(args) -> None:
    ips = [args.ip] if args.ip else detect_ips()
    ensure_cert(ips, force=args.regen_cert)
    urls = [f"https://{ip}:{args.port}/" for ip in ips]
    print_banner(urls, args.http_port)

    common = dict(host=args.bind, log_level="warning", access_log=False, ws="websockets")
    https = uvicorn.Server(uvicorn.Config(build_app(urls), port=args.port,
                                          ssl_certfile=str(CERT_FILE), ssl_keyfile=str(KEY_FILE), **common))
    http = uvicorn.Server(uvicorn.Config(build_http_app(args.port), port=args.http_port, **common))
    http.capture_signals = contextlib.nullcontext   # let the https server own Ctrl+C
    task_http = asyncio.create_task(http.serve())

    async def reaper():
        while True:
            await asyncio.sleep(3)
            phones.reap()
    task_reap = asyncio.create_task(reaper())
    import threading
    import pad_relay
    pad_relay.start()
    threading.Thread(target=key_thread, args=(asyncio.get_running_loop(),), daemon=True).start()
    print("  Lens tuning (headset on, keys in THIS window):  [ ]  eye separation   - =  field of view   0  reset")
    await https.serve()
    http.should_exit = True
    task_reap.cancel()
    await task_http


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--http-port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--ip", help="LAN IP to advertise (and put in the certificate) instead of auto-detect")
    ap.add_argument("--regen-cert", action="store_true", help="make a new certificate even if one exists")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
    print("server stopped")


if __name__ == "__main__":
    main()

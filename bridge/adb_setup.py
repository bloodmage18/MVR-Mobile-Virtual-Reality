"""
Android <-> Windows USB link helper (stdlib only).

  python bridge/adb_setup.py               # check phone + forward port 8080 over USB
  python bridge/adb_setup.py --sensors     # list the phone's hardware sensors (is there a gyroscope?)
  python bridge/adb_setup.py --open /check # also open that page in Chrome on the phone
  python bridge/adb_setup.py --watch       # keep re-applying the port forward if the cable is replugged

`adb reverse` makes the phone's http://localhost:8080 reach this laptop's port 8080 over
the USB cable. Chrome treats localhost as a secure context, so motion sensors work with
no HTTPS certificate and no Wi-Fi.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_PORT = 8080
CHROME_PKG = "com.android.chrome"

INSTALL_HINT = """\
adb not found. Install Android Platform Tools (one of):
  winget install Google.PlatformTools
  or download https://developer.android.com/tools/releases/platform-tools,
  unzip to C:\\platform-tools (or <project>\\tools\\platform-tools)"""

PHONE_HINT = """\
On the Samsung phone:
  1. Settings > About phone > Software information > tap 'Build number' 7 times
  2. Settings > Developer options > turn on 'USB debugging'
  3. Plug in the cable, unlock the phone, tap 'Allow' on the USB debugging prompt
     (tick 'Always allow from this computer')
  4. If no prompt appears: pull down notifications > USB > choose 'Transferring files'"""


# ── adb discovery ────────────────────────────────────────────────────────────

def find_adb() -> str | None:
    found = shutil.which("adb")
    if found:
        return found
    project = Path(__file__).resolve().parent.parent
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        project / "tools" / "platform-tools" / "adb.exe",
        Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
        Path(local) / "Microsoft" / "WinGet" / "Links" / "adb.exe",
        Path("C:/platform-tools/adb.exe"),
        Path("C:/Android/platform-tools/adb.exe"),
    ]
    # winget unpacks into a versioned package folder
    pkgs = Path(local) / "Microsoft" / "WinGet" / "Packages"
    if pkgs.is_dir():
        candidates += list(pkgs.glob("Google.PlatformTools*/platform-tools/adb.exe"))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def run(adb: str, *args: str, serial: str | None = None, timeout: float = 15) -> subprocess.CompletedProcess:
    cmd = [adb] + (["-s", serial] if serial else []) + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          encoding="utf-8", errors="replace")


# ── device checks ────────────────────────────────────────────────────────────

def list_devices(adb: str) -> list[tuple[str, str]]:
    out = run(adb, "devices").stdout
    devices = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devices.append((parts[0], parts[1]))   # (serial, state)
    return devices


def pick_device(adb: str, quiet: bool = False) -> str | None:
    run(adb, "start-server", timeout=30)
    devices = list_devices(adb)
    ready = [s for s, st in devices if st == "device"]
    if ready:
        if len(ready) > 1 and not quiet:
            print(f"Several devices connected, using {ready[0]}")
        return ready[0]
    if not quiet:
        if any(st == "unauthorized" for _, st in devices):
            print("Phone found but NOT authorized: unlock it and tap 'Allow' on the USB debugging prompt.")
        elif any(st == "offline" for _, st in devices):
            print("Phone is 'offline': unplug/replug the cable, or run: adb kill-server")
        else:
            print("No Android device found over USB.\n")
            print(PHONE_HINT)
    return None


def describe_device(adb: str, serial: str) -> str:
    def prop(name: str) -> str:
        return run(adb, "shell", "getprop", name, serial=serial).stdout.strip()
    model = prop("ro.product.model")
    brand = prop("ro.product.brand")
    android = prop("ro.build.version.release")
    soc = prop("ro.soc.model") or prop("ro.board.platform") or prop("ro.hardware")
    size = run(adb, "shell", "wm", "size", serial=serial).stdout.strip().replace("Physical size: ", "")
    return f"{brand} {model} | Android {android} | SoC {soc} | screen {size}"


def forward_port(adb: str, serial: str, port: int) -> bool:
    res = run(adb, "reverse", f"tcp:{port}", f"tcp:{port}", serial=serial)
    if res.returncode != 0:
        print(f"adb reverse failed: {res.stderr.strip() or res.stdout.strip()}")
        return False
    return True


def reverse_active(adb: str, serial: str, port: int) -> bool:
    out = run(adb, "reverse", "--list", serial=serial).stdout
    return f"tcp:{port}" in out


def wake(adb: str, serial: str) -> None:
    """Turn the screen on and dismiss a swipe-only lock screen (a PIN/pattern still needs a hand)."""
    run(adb, "shell", "input", "keyevent", "KEYCODE_WAKEUP", serial=serial)
    run(adb, "shell", "wm", "dismiss-keyguard", serial=serial)


def tap(adb: str, serial: str) -> None:
    """A real touch from USB: Chrome accepts it as the user gesture for fullscreen.
    (360, 360) lies inside the 720x1600 screen in both portrait and landscape."""
    run(adb, "shell", "input", "tap", "360", "360", serial=serial)


def open_url(adb: str, serial: str, url: str) -> None:
    base = ["shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url]
    res = run(adb, *base, "-p", CHROME_PKG, serial=serial)
    if res.returncode != 0 or "Error" in res.stdout + res.stderr:
        print("Chrome not found; opening in the default browser. "
              "(Samsung Internet may block sensors. Install Chrome for best results.)")
        run(adb, *base, serial=serial)


# ── sensor inventory ─────────────────────────────────────────────────────────

SENSOR_KEYS = {
    "gyroscope":              ("android.sensor.gyroscope",              "REQUIRED for good head tracking"),
    "accelerometer":          ("android.sensor.accelerometer",          "tilt / gravity"),
    "magnetometer":           ("android.sensor.magnetic_field",         "compass (noisy, fallback yaw)"),
    "game_rotation_vector":   ("android.sensor.game_rotation_vector",   "gyro+accel fusion (best for VR)"),
    "rotation_vector":        ("android.sensor.rotation_vector",        "gyro+accel+mag fusion"),
    "geomag_rotation_vector": ("android.sensor.geomagnetic_rotation_vector", "accel+mag only (no gyro)"),
}


def sensor_inventory(adb: str, serial: str) -> dict[str, bool]:
    out = run(adb, "shell", "dumpsys", "sensorservice", serial=serial, timeout=30).stdout
    # Only the sensor list section; later sections mention types again
    section = out.split("Fusion States")[0].split("Recent Sensor events")[0].lower()
    return {key: (stype in section) for key, (stype, _) in SENSOR_KEYS.items()}


def print_sensors(adb: str, serial: str) -> dict[str, bool]:
    inv = sensor_inventory(adb, serial)
    print("\nHardware sensors reported by Android:")
    for key, present in inv.items():
        mark = "YES" if present else " no"
        print(f"  [{mark}] {key:<24} {SENSOR_KEYS[key][1]}")
    print()
    if inv["gyroscope"]:
        print("VERDICT: gyroscope present -> use phone head tracking (RelativeOrientationSensor).")
    elif inv["magnetometer"]:
        print("VERDICT: NO gyroscope -> phone tracking will be accel+compass only (laggy/noisy).")
        print("         See PLAN_ANDROID_WINDOWS.md > 'If there is no gyroscope' for options.")
    elif inv["accelerometer"]:
        print("VERDICT: accelerometer ONLY -> the phone can sense tilt (pitch/roll) but NOT turning (yaw).")
        print("         HMD view uses 'tilt' mode; turn with mouse/controller, or add an external IMU.")
    else:
        print("VERDICT: no motion sensors -> phone is a display only.")
    return inv


# ── entry points ─────────────────────────────────────────────────────────────

def setup(port: int = DEFAULT_PORT, open_path: str | None = None, sensors: bool = False,
          quiet: bool = False) -> str | None:
    """Find adb + phone, forward the port. Returns device serial or None."""
    adb = find_adb()
    if not adb:
        print(INSTALL_HINT)
        return None
    serial = pick_device(adb, quiet=quiet)
    if not serial:
        return None
    print(f"Phone: {describe_device(adb, serial)}")
    if not forward_port(adb, serial, port):
        return None
    print(f"USB link ready: phone http://localhost:{port} -> laptop port {port}")
    if sensors:
        print_sensors(adb, serial)
    if open_path:
        open_page(adb, serial, port, open_path)
    return serial


def open_page(adb: str, serial: str, port: int, path: str) -> None:
    wake(adb, serial)
    # Unique query string: otherwise Chrome may just refocus an old tab without reloading
    sep = "&" if "?" in path else "?"
    open_url(adb, serial, f"http://localhost:{port}{path}{sep}t={int(time.time())}")
    print(f"Opened http://localhost:{port}{path} on the phone")


def watch(port: int, interval: float = 2.0) -> None:
    """Re-apply adb reverse whenever the phone (re)appears. Ctrl+C to stop."""
    adb = find_adb()
    if not adb:
        print(INSTALL_HINT)
        return
    print("Watching for the phone (Ctrl+C to stop)...")
    last = None
    while True:
        serial = pick_device(adb, quiet=True)
        if serial and not reverse_active(adb, serial, port):
            if forward_port(adb, serial, port):
                print(f"[{time.strftime('%H:%M:%S')}] port {port} forwarded to {serial}")
        elif not serial and last:
            print(f"[{time.strftime('%H:%M:%S')}] phone disconnected")
        last = serial
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", metavar="PATH", help="page to open on the phone, e.g. /check or /")
    ap.add_argument("--sensors", action="store_true", help="list hardware sensors (gyro check)")
    ap.add_argument("--watch", action="store_true", help="keep re-forwarding when replugged")
    args = ap.parse_args()

    if args.watch:
        try:
            watch(args.port)
        except KeyboardInterrupt:
            pass
        return 0
    return 0 if setup(args.port, args.open, args.sensors) else 1


if __name__ == "__main__":
    sys.exit(main())

"""
Phase 0 — USB Tunnel Test
Connects to iPhone over Lightning cable via USBMuxd and sends test messages.

Run AFTER the HMD app is open on the iPhone.
May need to run as Administrator on Windows.
"""

import time
import socket
from pymobiledevice3.usbmux import USBMuxClient

PORT = 12345


def main():
    print("=" * 40)
    print("  Cross Reality HMD — Phase 0 Test")
    print("=" * 40)
    print()
    print("Scanning for connected iPhone...")

    with USBMuxClient() as mux:
        devices = mux.devices()

        if not devices:
            print()
            print("No device found. Check:")
            print("  1. iPhone is plugged in via USB")
            print("  2. You tapped 'Trust This Computer' on the iPhone")
            print("  3. iTunes / Apple Mobile Device Support is installed")
            return

        device = devices[0]
        print(f"Found: {device.serial}")
        print()
        print(f"Connecting to port {PORT} on device...")
        print("(Make sure the HMD Listener app is open on the iPhone)")
        print()

        try:
            sock = mux.connect(device, PORT)
        except Exception as e:
            print(f"Connection failed: {e}")
            print()
            print("Possible reasons:")
            print("  - HMD app is not open or not running")
            print("  - App is not listening on the correct port")
            return

        print("Connected.")
        print()

        messages = [
            "hello from PC",
            "phase 0 test",
            "tunnel is working",
        ]

        all_ok = True
        for msg in messages:
            print(f"  Sending  -> {msg}")
            sock.send(msg.encode("utf-8"))
            time.sleep(0.3)

            try:
                sock.settimeout(2.0)
                response = sock.recv(1024)
                decoded = response.decode("utf-8").strip()
                print(f"  Echo     <- {decoded}")
            except socket.timeout:
                print("  Echo     <- (no response — timeout)")
                all_ok = False

            print()

        sock.close()

        print("=" * 40)
        if all_ok:
            print("  PHASE 0 COMPLETE — USB tunnel works.")
        else:
            print("  Partial — tunnel connects but echo missing.")
            print("  Check the iOS app logs.")
        print("=" * 40)


if __name__ == "__main__":
    main()

# iOS track — ON HOLD (2026-11-14)

Paused because:
- No Mac available → no Xcode, so the native Swift app can't be built or signed reliably.
- The iPhone 12 overheats under load, and Windows tooling for it (iTunes drivers, pymobiledevice3, 7-day sideload expiry) is fragile.

Active work moved to Android + Windows: see `../PLAN_ANDROID_WINDOWS.md`.

## What's parked here
| File | What it was |
|---|---|
| `plan_ios_native.md` | Original native plan (Godot → NVENC → USBMuxd → Swift/VideoToolbox) |
| `bridge/usb_tunnel.py` | Phase 0 USBMuxd echo test (pymobiledevice3) |
| `bridge/requirements.txt` | `pymobiledevice3` |
| `ios_hmd/HMDListener.swift` | Minimal SwiftUI TCP echo server on port 12345 |

## If you resume iOS later
- Correction to the old plan: this laptop's GeForce MX130 has **no NVENC**. Use Intel Quick Sync (`h264_qsv`) or x264 instead (same as the Android plan).
- The cheapest way back in is the **browser client** in `../bridge/web/hmd.html`. It already works with any phone that can load `http://localhost:8080`. Over USB, iOS needs a pymobiledevice3 tunnel. Safari also needs `DeviceMotionEvent.requestPermission()` on a tap, and it supports WebCodecs on iOS 17+.

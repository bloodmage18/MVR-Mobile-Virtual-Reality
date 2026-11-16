# Wireless headset: plan

Goal: no cable between the phone (headset) and the laptop. The controller is already wireless. Start this **after the wired build is stable**. A dedicated phone app comes after that (step W4).

## Why the cable matters today

The cable does three jobs:

1. **Secure context.** `adb reverse` lets Chrome open `http://localhost:8080`. Chrome treats localhost as secure, so it allows the motion sensors and the WebCodecs video decoder. Opening `http://192.168.x.x` over Wi-Fi is **not** secure, and Chrome blocks both.
2. **Hands-free start.** adb wakes the phone, opens the page and taps the screen for fullscreen.
3. **Transport.** Pose and video travel over USB, with low and steady latency.

Each wireless step below replaces these jobs one at a time.

## Steps

| # | Step | What changes | Done when |
|---|---|---|---|
| W1 | **Wireless adb** (Android 11+ "Wireless debugging") | Pair once with `adb pair`, then `adb connect <ip>:<port>`. Everything else stays the same: `adb reverse`, localhost, wake/tap/open. The bridge already accepts a wireless adb device; it needs `--connect <ip:port>` and a helper that re-connects on start. | You play a full session with no cable. Measure latency and stutter against wired. |
| W2 | **Network tuning** | Use Windows Mobile Hotspot on 5 GHz, so the phone talks to the laptop directly with no router hop, or a 5 GHz router. Turn off phone Wi-Fi power saving. Set TCP_NODELAY on the WebSockets. Add a small jitter buffer on the phone, and let timewarp cover the latency. | Video holds at least 30 fps with no visible hitches over 10 minutes |
| W3 | **No adb at all** | (a) HTTPS from the bridge with a local certificate authority (mkcert) installed on the phone once. Chrome then trusts `https://<laptop>:8443`. (b) The laptop shows a QR code to open the page. The bridge advertises itself over mDNS. (c) The page starts with a long-press or a controller button instead of an adb tap. | The phone joins from the home screen with no developer options |
| W4 | **Phone app** (Android, Kotlin) | It replaces Chrome. MediaCodec does hardware video decode; SensorManager gives the motion data; poses go out over UDP (no TCP head-of-line stalls); the lens shader runs in OpenGL ES. It finds the laptop automatically, and the screen stays on with no fullscreen gesture. Same bridge protocol, so the Godot side doesn't change. | Lower latency and smoother than the browser client, with nothing to set up |

## Things to keep in mind

- **Battery.** The phone currently charges over USB. Wireless play drains it: screen at full brightness plus video decode plus Wi-Fi. A power bank in the headset strap solves this.
- **Wireless debugging switches itself off** when the phone changes Wi-Fi network or reboots. The bridge has to reconnect, and the port changes each time. The bridge can find it over mDNS (`adb mdns services`).
- **Heat.** It should be lower than with the iPhone, since there is no encoding on the phone. Watch it in long sessions anyway.
- **Latency budget.** USB today feels in sync. Expect Wi-Fi to add about 5–20 ms, with spikes. Pose packets are tiny; video is the heavy part (8 Mbps). Timewarp hides head-motion latency, not the latency of the image content.
- **Quick development shortcut** for testing W2 before W3: in Chrome, `chrome://flags/#unsafely-treat-insecure-origin-as-secure` set to `http://<laptop-ip>:8080`. Use this for testing only.

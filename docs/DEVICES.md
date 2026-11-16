# Devices, Stats, Restrictions and Issues

This file records every device used in **MVR - Mobile Virtual Reality**. The project turns a phone in a cardboard-style headset into a VR display driven by a Windows laptop. For each device it gives the specs, the measured numbers, the limits we hit and the issues we faced, with the status of each.

Period covered: **2026-11-14 to 2026-11-15**. Later dates win where the notes conflict. Real conflicts are flagged with **CONFLICT**.

**Evidence tags** (each claim carries one):

| Tag | Meaning |
|---|---|
| **[TEST]** | Measured or verified by a tool or automated test (ffmpeg run, adb query, headless browser, Godot headless run, curl, Python check). Most tests ran on the laptop with a *simulated* phone. |
| **[USER]** | Reported by the owner after using the real device. |
| **[DOC]** | A design value or default in code or docs. Not measured. |
| **[ASSUMED]** | A planning estimate or calculation. Not measured. |

A tag may carry a qualifier (e.g. [TEST adb], [TEST grep], [USER log] = owner-supplied log output, [USER/DOC] = owner report backed by docs).

> No glass-to-glass latency, phone or laptop temperature, Wi-Fi latency or battery drain has been measured with an instrument yet. All statements about those are user impressions or estimates.

---

## 1. Architecture overview (modes)

Where it started: the owner first tried a SteamVR phone bridge. It was "too processor heavy… kept lagging… heating up both laptop and phone" [USER], so this project builds a lightweight custom HMD instead.

| # | Mode | How it works | Transport |
|---|---|---|---|
| 1 | **Wired streaming** (`start_hmd.bat`, `start_home.bat`) | 1. Godot 4.7 window → ffmpeg `gfxcapture` → `h264_qsv` → FLV pipe → `hmd_bridge.py` (FastAPI) → WebSocket `/ws/video` → phone Chrome `hmd.html` (WebCodecs H.264 → WebGL2 with lens correction and timewarp).<br>2. Pose: phone `/ws/pose` → bridge → UDP `127.0.0.1:9100` → Godot.<br>3. Lens settings: Godot → UDP 9101 → bridge → phone. | USB, `adb reverse tcp:8080` (phone `http://localhost:8080`) |
| 2 | **Wi-Fi bridge** (`start_home_wifi.bat`, `hmd_bridge.py --wifi`) | Same pipeline as mode 1, served over HTTPS. Needs a `?k=` access key. | Wi-Fi, HTTPS 8444 |
| 3 | **Phone-hosted** (`phone_host_test.bat` → `phone_host/server.py`) | The phone renders a WebGL scene, or `/game/` (a Godot 4.7 Web export), itself. The laptop only serves files and relays the Xbox pad. | Wi-Fi, HTTPS 8443 (+ HTTP 8080 for redirect and `/ca.crt`) |
| 4 | **Home Space** (`start_home.bat`) | A 3-floor library Godot project plus `media_service.py`: video via ffmpeg, and a browser via a headless Edge CDP screencast. Streamed through mode 1 or 2. | Media service on 127.0.0.1:8090 |
| 5 | **VR browser test** (`vr_browser_test.bat`) | A headless Brave (dedicated persistent profile) screencasts JPEG frames to a stereo WebGL2 page on the phone. Videos are resolved with yt-dlp and played by the phone through the `/media` proxy. | Wi-Fi, HTTPS 8443 + `?k=` key |

Abandoned or on hold:
- Native iOS: Godot → NVENC → USBMuxd/pymobiledevice3 → Swift VideoToolbox.
- "Plan B": Safari over a pymobiledevice3 tunnel.

---

## 2. Device summary

| Device | Role | Key specs | Status |
|---|---|---|---|
| Windows laptop | Host: renders the game, encodes, runs the bridge and servers | i7-8650U 4C/8T 15 W, 16 GB, UHD 620 + MX130 (Optimus, **no NVENC**), 1920×1080 @125%, Windows 11 Pro | Active |
| Samsung Galaxy A08 (SM-A085F) | HMD phone (active since 2026-11-14) | MediaTek MT6789, 1600×720 @ 90 Hz, 6.7", **accelerometer only** | Active; tilt-only tracking |
| iPhone 12 | Original HMD, later used again for Wi-Fi tests | ~6.1", gyroscope, Safari, DPR 3 | Native iOS **on hold**; the web modes work and it is the owner's preferred headset phone |
| Headset (VR Shinecon) | Viewer with lenses | Manual lens knobs; lens spacing ≈ iPhone 12 half-screen centres (~65 mm) | Active; exact model/lens specs unknown |
| Xbox controller | Input | XInput on the laptop; wireless | Active through the laptop relay; phone Bluetooth path untested |
| Wi-Fi router / LAN | Wi-Fi modes | Home router with 2.4 + 5 GHz; laptop profile set to Private | Working |
| Earbuds | Audio | Connected to the **laptop** [USER] | Active |
| Keyboard + mouse | Input / tuning keys | Used on the laptop | Active |

---

## 3. Windows laptop (host)

### Specs [TEST]

| Item | Value |
|---|---|
| OS | Windows 11 Pro 10.0.26200 |
| CPU | Intel Core i7-8650U @ 1.90 GHz, 4 cores / 8 threads (15 W U-series, per spec) |
| RAM | 15.9 GB reported (16 GB) |
| iGPU | Intel UHD Graphics 620, driver 31.0.101.2134. Drives the display; provides Quick Sync. |
| dGPU | NVIDIA GeForce MX130, driver 582.28 (32.0.15.8228). **No NVENC.** Optimus hybrid. |
| Display | 1920×1080 panel at 125% scaling (AppliedDPI 120); logical bounds 1536×864 |

### Encoder settings [DOC, `hmd_bridge.py`]

- QSV: `-preset veryfast -profile:v main -bf 0 -g <fps> -async_depth 1 -b:v 8M -maxrate 8M`
- x264 fallback: `-preset ultrafast -tune zerolatency -profile:v main -bf 0 -g <fps>`, 8M bitrate/maxrate/bufsize
- Input flags `-fflags nobuffer -flags low_delay`. GOP = fps, so there is one keyframe per second for recovery.
- Bridge defaults: `--fps 60 --bitrate 8M --size 1600x720 --encoder qsv --video off`. The `.bat` files pass `--video game`. The window-title prefix is `--title "Cross Reality HMD"`.
- When the phone falls behind, the bridge skips ahead to the next keyframe.

### Measured performance

| What | Number | Context | Tag |
|---|---|---|---|
| h264_qsv encode | 300 frames, **142 fps**, 2.37× speed, ~8727 kbit/s | ffmpeg 9.0.2, 1600×720 testsrc2 @60, 5 s, 8M (2026-11-14 15:21) | [TEST] |
| libx264 ultrafast encode | 300 frames, 8.53× speed, ~8266 kbit/s, 0.587 s real time | Same input | [TEST] |
| gdigrab + QSV, capture only | **~42 fps** max (126 frames / 3 s) | Desktop GDI capture | [TEST] |
| gdigrab through the bridge | **~36.8 fps** (1 config, 4 key, 135 delta frames) | `--fps 40`, simulated phone | [TEST] |
| Test pattern through the bridge | QSV ~47.1 fps; x264 ~44.2 fps | `--fps 40` | [TEST] |
| gfxcapture + QSV, capture only | 211 frames / 4 s = **~53 fps**, 0.993× | Windows Graphics Capture by game window handle | [TEST] |
| gfxcapture through the bridge | **~51.8 fps** (1 config, 5 key, 255 delta frames) | Simulated phone; became the default (`--video game`) | [TEST] |
| Real session, desktop capture | Bridge sent 21.7 → 22.7 → steady **29.6–30.0 fps**, **0 dropped** | gdigrab 1600×720@40 QSV to the A08 over USB, 2026-11-14 16:08 | [USER log] |
| Bridge Wi-Fi throughput | Page 200, pose WSS ok, video WSS **142 msgs in 4 s at 5.4 Mbit/s** (~35 fps) | `--wifi`, laptop-side test, 2026-11-15 15:57 | [TEST] |
| Home Space video station | Godot screen plays the sample at **~29 fps** (capped at 30) | Headless, after the TCP frame-socket fix; 1280×720 sample | [TEST] |
| Godot WebSocketPeer receive | ~50 ms per 2.7 MB RGB frame + ~10 ms `get_packet`; Godot fell **49 → 2.4 fps** | Before the fix (see issues) | [TEST] |
| VR browser screencast | First JPEG 0.09 s after connect; JPEG quality 80, MAX_FPS 30 | Port 8453 smoke test, 2026-11-15 19:29 | [TEST]/[DOC] |
| VR-browser proxy upstream | One open-ended read **0.75 MB/s**; 2 MB chunks ×3 in parallel **21.7 MB/s** (4K VP9 needs ~2.1 MB/s) | YouTube media via the laptop | [TEST] |
| `yt-dlp -J` | ~7 s per lookup | VR browser | [TEST] |
| yt-dlp download (small video) | 10.9 s, format 133+140 (320×240) | Smoke test | [TEST] |
| Godot 4.7 template download | Single curl stalled at ~478 MB / **0.06 MB/s**; parallel range download did **383 MB in ~1.5 min** | 1,279,207,690-byte .tpz, 2026-11-15 15:58 | [TEST] |

Thermals: **not measured**.
- Plan advice [ASSUMED]: avoid x264 at 60 fps on the 15 W CPU for long sessions; prefer QSV, the Mobile renderer and a 60 fps cap.
- Home Space runs only one live media surface at a time, because Godot + QSV encode + video decode + a headless browser compete for the CPU [DOC].
- Library lighting is limited to 6 OmniLights per floor with no shadows, to suit the laptop's graphics [DOC].

### Restrictions

- **No NVENC** on the MX130. Hardware H.264 encoding has to use Quick Sync on the UHD 620.
- **Optimus:** Desktop Duplication (`ddagrab`) fails because ffmpeg runs on the MX130 while the screen belongs to the UHD 620. Setting ffmpeg to Power saving in Windows Graphics settings does not fix it. [TEST, 2026-11-15]
- `gdigrab` captures a screen region. It is slow, and it picks up any window that covers the game. The 125% scaling also shifts coordinates.
- **Godot renders on the UHD 620, not the MX130.** NVIDIA driver 582.28 does not register its Vulkan driver for the MX130, so Godot's Vulkan only sees Intel and `--gpu-index 1` does nothing. Workaround: set `VK_ADD_DRIVER_FILES` to NVIDIA's `nv-vk64.json` in the driver store (its folder name changes with every driver update); a clean NVIDIA driver reinstall should fix the registration. [TEST, 2026-11-15]
- **Window capture breaks when the game renders on the MX130** (see the GPU table below), so the current capture pipeline effectively forces Intel rendering.
- Godot Web export runs as "OpenGL ES 3.0 (WebGL 2.0) - Compatibility", Emscripten 4.0.20, **single-threaded, no GDExtension**. `index.wasm` is ~39.5 MB [TEST].
- No single Python interpreter has every package. The `.venv` has fastapi/uvicorn/cryptography/psutil; system Python 3.14 has yt-dlp. The server runs in `.venv` and calls yt-dlp as a subprocess.
- The console uses a non-UTF-8 legacy code page, so printing JSON can raise `UnicodeEncodeError`. Set `PYTHONIOENCODING=utf-8`.
- Only one server can hold port 8443: phone_host and the VR browser cannot run together.
- The Brave sign-in window and the VR browser server cannot run together, because both lock the same profile.
- **Use a dedicated Brave profile with no personal accounts signed in.** The access key grants control of that browser session.

### MX130 vs UHD 620 for rendering [TEST, 2026-11-15, school level]

| Measurement | MX130 | UHD 620 |
|---|---|---|
| Render speed, vsync off | 125–145 fps | 55–61 fps |
| Godot reads back each frame (blocking `get_image`) | ~37 fps | ~21 fps |
| Godot reads back each frame (async `texture_get_data_async`) | ~92 fps; steady 60 when capped (278 MB/s) | 52–61 fps |
| Window capture (`gfxcapture`) of the game window | Vulkan: 4 frames in 6 s (~17 fps with vsync off); D3D12: ffmpeg cannot open it; OpenGL: 1 frame | 52 fps |

**Conclusion:** the MX130 renders over twice as fast, but only a "direct push" pipeline can use it: Godot reads back each finished frame asynchronously and pipes raw frames to ffmpeg, which still encodes on Quick Sync. That skips window capture, lets the game window stay hidden, and leaves the Intel chip only the encoding (planned as Phase 5 in `home_space/PLAN.md`).
**Also noted:** many desktop apps (Start menu, Notepad, VS Code) run on the MX130, suggesting the NVIDIA Control Panel global preference is "High-performance". "Auto-select" with the NVIDIA chip chosen only for Godot would save heat and battery (not yet changed).

### Issues faced

| Date | Symptom | Cause | Fix / workaround | Status |
|---|---|---|---|---|
| Planning | Planned NVENC encode impossible | MX130 has no NVENC | `h264_qsv` primary, `libx264` fallback | **Fixed** [TEST] |
| 11-14 14:53 | FLV DataOffset misread; end-of-stream could be lost when the queue was full | Code bugs found in review before the first run | Fixed in code; parser test passes | **Fixed** [TEST] |
| 11-14 15:20 | "ffmpeg not found" right after the winget install | VS Code terminal had the old PATH | Bridge searches winget's Links/Packages folders | **Fixed** |
| 11-14 15:36 | ffmpeg restarted every ~2 s; no video on the phone | `read_exact` treated a partial pipe read of a big keyframe as end of stream | Loop until all n bytes arrive | **Fixed** [TEST; USER 30 fps, 0 drops] |
| 11-14 15:38 | `ddagrab`: "Selected output not supported" / "Generic error in an external library" | Optimus GPU mismatch | Settings > Display > Graphics > ffmpeg.exe > "Power saving" (manual; the automated registry change was blocked) | **Open / untested**; superseded by gfxcapture |
| 11-14 16:12 | Phone showed VS Code instead of the game | Fixed-region capture (0,0 1600×720); game window was elsewhere; 125% scaling | `--video game` = gfxcapture by window handle; skips the editor's same-titled floating Game window | **Fixed** [TEST ~52 fps] |
| 11-14 16:12 | Bridge crashed with "keepalive ping timeout" | Slow phone connection | Exceptions caught; ping interval 20 s / timeout 60 s | **Fixed** (not retested on phone) |
| 11-14 | gfxcapture bridge test logged "h264_qsv Error submitting a packet to the muxer: Invalid argument (-22)" | Probably shutdown; not investigated | None | **Open** (harmless so far) |
| 11-14 15:39 | Godot "could not bind UDP port 9100" | Another test instance held the port | None needed | Test artefact |
| 11-14 17:20 | Could not push to GitHub | `gh` CLI not installed; no remote | Owner published the repo later | **Resolved** |
| 11-15 15:52 | Home Space video: Godot 49 → 2.4 fps | WebSocketPeer too slow for 2.7 MB frames | Raw frames over local TCP (16-byte `VFRM` header + RGB8), worker thread keeps only the newest frame, 30 fps cap | **Fixed** [TEST ~29 fps]; untested in headset |
| 11-15 15:52 | Headless Edge gets Google's captcha / "sorry" page | Headless browser detected | Search uses DuckDuckGo | Workaround |
| 11-15 15:58 | Template download stalled at 0.06 MB/s | Stuck single connection | Parallel, resumable range download | **Fixed** [TEST] |
| 11-15 16:06 | Web export: "Cannot export project with preset 'Web' due to configuration errors" | Mobile VRAM texture compression enabled in the preset (presumed) | `vram_texture_compression/for_mobile=false` | **Fixed** (web build produced and used later) |
| 11-15 16:08 | Web game "Uncaught (in promise)" from pointer lock | Browsers refuse pointer lock without a gesture | No pointer lock on the web | **Fixed** [TEST] |
| 11-15 16:10 | Web lens / head-tracking code never ran | `JavaScriptBridge.eval` returns a JS bool as int → "Invalid operands 'int' and 'bool'" | Comparison fixed | **Fixed** [TEST] |
| 11-15 16:17 | "Failed to create PositionWorklet" / worklet load AbortError | Likely server restarts and port contention during tests | AudioContext wrapper rewritten as a Proxy | **Likely fixed / intermittent** [TEST] |
| 11-15 16:35 | Own account locked out of the cert files (PermissionError) | `icacls /inheritance:r` with an `(OI)(CI)` grant left the files with empty ACLs | Lock the folder; files inherit | **Fixed** [TEST] |
| 11-15 18:14 | A development session ended abruptly and left servers and a hidden Brave process running | Session interrupted | Processes stopped by ID; Brave is now tied to the server by a job object | **Resolved** |
| 11-15 19:29 | Port 8443 busy | phone_host still running | Tests on 8453; close phone_host first | Workaround |
| 11-15 19:29 | No Python had every package | Split installs | `.venv` + system Python for yt-dlp | Workaround |
| 11-15 19:29 | `UnicodeEncodeError` when printing JSON | Non-UTF-8 legacy console code page | `PYTHONIOENCODING=utf-8` | Workaround |

### Open questions

- Does setting ffmpeg to Power saving make `ddagrab` work?
- Which GPU runs Godot?
- Laptop temperatures and CPU load in a long session.

---

## 4. Samsung Galaxy A08 (active HMD phone)

### Specs

| Item | Value | Tag |
|---|---|---|
| Model | SM-A085F (budget model, 2026 release) | [TEST adb] |
| OS | Android 17 (adb) | [TEST] |
| SoC | MediaTek MT6789 (the plan calls it Helio G99) | [TEST adb] |
| Screen | 1600×720 (adb 720x1600 portrait), 6.7", **90 Hz** | [TEST] |
| Browser | Chrome, version not recorded (Samsung Internet avoided; it "may block sensors" [ASSUMED]) | [TEST] |
| Viewport in Chrome | 1601×720 landscape, devicePixelRatio 1.875 | [TEST] |
| Pixels per eye | ~800×720, ≈8 px/deg | [ASSUMED calc] |

### Sensors [TEST, Android sensor list + `check_phone.bat` in Chrome, 2026-11-14]

| Sensor / API | Result |
|---|---|
| Accelerometer | **Yes**, 60 Hz, sample [0.2, 9.1, 4.3] (no readings on the first run, OK on the second) |
| Gyroscope | **No** (NotReadableError "Could not connect to a sensor") |
| Magnetometer | **No** (API not supported) |
| Game / rotation / geomag rotation vector | **No** |
| Relative/AbsoluteOrientationSensor | NotReadableError |
| `deviceorientation` | 0.7 Hz, alpha/beta/gamma null (later runs: fires with alpha fixed at 0, beta/gamma from the accelerometer) |
| `devicemotion` | 59.3 Hz, 16 ms interval, `rotationRate` null |
| WebCodecs H.264 decode | Hardware |
| Wake lock / fullscreen / orientation lock | All available |
| Secure context | Yes (`localhost` via `adb reverse`) |

**Verdict:** the A08 senses pitch and roll (tilt) only. It **cannot sense yaw** (turning left/right). Turning comes from the mouse, the controller right stick or a screen drag.

### Measured performance

| What | Number | Context | Tag |
|---|---|---|---|
| Refresh rate | ~54 Hz on the first run, ~90 Hz on the second (recorded as 90 Hz) | Chrome check page. **CONFLICT:** the first reading was low; 90 Hz is the accepted value. | [TEST] |
| Pose stream (tilt) | ~58–60 Hz (57.9, 59.9, 60.1, 60.0, 60.4) | USB, `/ws/pose` | [USER log] |
| Video playback | 30 fps, 0 drops, "no delays, it was in sync" | Play-test 2, desktop capture over USB | [USER] |
| Video in the VR browser | Design note: if video stutters (e.g. on the A08), the v2 page drops to a lower quality by itself (README). Not observed or reported yet. | VR browser | [DOC] |
| Test capability set (fixture, not measured) | MSE, MAX_TEXTURE_SIZE 8192, avc1 1080, VP9 2160@30 / 1080@60, AV1 720/480 | Used in tests only | [DOC] |

Thermals and battery: **not measured**. Plan [ASSUMED]: the phone only decodes (its cheapest job); lower the brightness; USB charging adds heat.

### Restrictions

- **Accelerometer only:** no yaw tracking, and timewarp cannot correct yaw.
- Chrome allows fullscreen and landscape lock **only after a real touch**.
- adb can wake the screen and dismiss a swipe lock, but **a PIN, pattern or fingerprint lock needs a hand** before the phone goes into the headset.
- Setup needed:
  - Developer options (tap Build number 7×).
  - USB debugging, accepting the Allow prompt.
  - **Stay awake** ON while charging.
- Over Wi-Fi, Chrome refuses motion sensors and WebCodecs on plain `http://` LAN addresses, so HTTPS is required. Over USB, `localhost` counts as secure.
- 5 GHz Wi-Fi support on the A08 was **unconfirmed** at the time.
- The screen is wider than the lens spacing: the eye centres sit ~75 mm apart [ASSUMED calc], so it needs a lens offset of 0.08.

### Issues faced

| Date | Symptom | Cause | Fix / workaround | Status |
|---|---|---|---|---|
| 11-14 14:47–15:20 | No head yaw | No gyroscope or magnetometer | Tilt mode (pitch/roll from gravity, `TILT_SMOOTH 0.25`) + mouse/stick/drag yaw. Planned: BNO085 IMU on an ESP32-C3 or Pro Micro (~$10–20, 100–400 Hz), or a second phone as tracker | Workaround **working** [USER "tilt worked"]; full yaw **open**, IMU not built |
| 11-14 15:20 | `--video desktop` showed nothing | Chrome refocused the old tab without reloading | Unique `?t=` query + video auto-reconnect | **Fixed** |
| 11-14 15:39 | View upside down / pitch inverted | Chrome on the A08 reports landscape tilt rotated 180° | Flip correction (`?flip=0` disables it); Godot `invert_pitch`/`invert_roll` exports | **Fixed** [TEST: camera-up y −0.96 → +0.96; USER confirmed 16:08] |
| 11-14 16:17 | Had to take the headset off to tap "Tap to start" | Fullscreen needs a user gesture | Bridge sends `KEYCODE_WAKEUP` + `adb shell input tap`; console keys Enter = tap, `r` = reload, `c` = recenter. CSS `[hidden]{display:none!important}` so the overlay hides. | **Fixed** for wired mode (bridge side tested: 2 taps sent, 2 expected); PIN lock still needs a hand |
| 11-14 17:53 | Walking direction didn't match the view | Recenter used the first pose, taken while the phone lay flat (56.3° error in the test, could be up to 180°) | Recenter waits until the phone is within ~60° of level | **Fixed** in simulation (0.0°) [TEST]; real playthrough pending |
| 11-15 13:55 | Whole view rolled 180° ("images flipped upside down") | The fixed 180° flip suited only one holding orientation | Auto-flip: if gravity reads upside down for ~0.5 s the flip inverts; `?flip=0/1` forces it; HUD shows `flip=N(auto)` | **Fixed in code**, untested on the phone at the time |
| 11-15 15:35 | Double image ("two north blocks") | 6.7" screen: eye centres (~75 mm) wider than the lenses | Lens offset 0.08 (Android default, remembered per phone), live tuning via laptop `[`/`]` (step 0.005), Lens sliders, d-pad | **Fixed** [TEST 64 px inward shift; USER 15:49 "works well for both now"] |
| 11-15 17:41 | Jagged, shimmering image; text jagged until close (smooth on the laptop) | No MSAA in the eye SubViewports; overscan spreads fewer pixels over the view; lens warp resamples | Proposed: MSAA, supersampling, mipmaps/anisotropic filtering, larger fonts | **Open**, deferred by the owner |

Planned safeguard (not an observed issue): v2 auto-lowers video quality on stutter [DOC].

### Open questions

- 5 GHz Wi-Fi support.
- Temperature and battery in long sessions.
- Real-phone check of the auto-flip and the level-recenter.
- External IMU for yaw.

---

## 5. iPhone 12

### Specs and role

- Model: iPhone 12, ~6.1" screen, **gyroscope present** (full look-around including yaw), devicePixelRatio 3 [USER/DOC].
- Original HMD. **Native iOS has been on hold since 2026-11-14.** It came back for the phone-hosted (Wi-Fi) and VR browser tests on 2026-11-15.
- Owner's view [USER, 11-15 15:49]: the iPhone is **lighter**, and gyro turning feels **more intuitive**. The small screen is "a physical limitation".
- Eye centres are ~65 mm apart [ASSUMED calc], close to the lens spacing, so the images fuse with **lens offset 0** ("works fine no tuning needed" [USER]).

### Measured / reported performance

| What | Result | Context | Tag |
|---|---|---|---|
| Phone-hosted Wi-Fi scene | "Works well, no tuning needed" | 2026-11-15 15:35 | [USER] |
| VR browser, normal browsing | Looks good | 2026-11-15 20:14 | [USER] |
| VR browser video | Blurry, even normal 1080p; 360° poor | 2026-11-15 20:14–20:16 | [USER] |
| Thermals | "Easily overheats" (no measurement) | Reason for the hold | [USER] |
| Test capability set (fixture) | MSE "mms", MAX_TEXTURE_SIZE 16384, avc1 2160 | Tests only | [DOC] |
| Browser-path latency | ~80–150 ms vs ~30–50 ms native | Estimate, never measured | [ASSUMED] |

### Restrictions (Safari / iOS)

- **No Mac:** no Xcode, so no signed native app. A free sideload expires after 7 days. The Windows tooling (iTunes/Apple Mobile Device drivers, pymobiledevice3) is fragile. The USBMuxd echo test (TCP 12345) was parked and never confirmed working.
- **Motion sensors** need a secure context, **and** `DeviceOrientationEvent.requestPermission()` must be called **inside a tap** ("Motion & Orientation" → Allow). No orientation events fire while the prompt is open.
- No `Accelerometer` (Generic Sensor) API in Safari.
- **No Fullscreen API for pages.** Workaround: Share > Add to Home Screen with web-app meta tags and a manifest (display fullscreen, landscape). The home-screen app may refuse an untrusted or self-signed certificate. With the Safari bars visible, the eye views don't line up with the lenses.
- Screen stays awake in Safari on iOS 16.4+; older versions need Auto-Lock = Never.
- **Audio:** an AudioContext created after the gesture starts "suspended" and needs `resume()` inside a gesture.
- Landscape notch: without `viewport-fit=cover`, iOS insets the layout ~47 pt per side.
- Avoid `100vh` (taller than the visible area when the bars show) and `inset` (Safari < 14.1).
- Safari may refuse a WebSocket on a self-signed cert; the status link falls back to HTTPS POST.
- Video plays only if Range requests get 206 answers (the proxy handles this). WebCodecs needs iOS 17+. Old Safari without MSE uses `split` mode.
- At DPR 3 a full-resolution canvas would render ~2500 px wide, so pages cap the DPR. Note: the caps differ per page (values as of 2026-11-15): the web game (`hmd_web.js`) caps at **1.5** (raising it to 2 was proposed); the phone-hosted WebGL scene (`phone_host/web/index.html`) caps at **2**; the VR browser page was capped at **2** (2/3 resolution, a cause of blur) and v2 raises it to **3** in stereo.
- Certificate warning bypass: Show Details → visit this website → Visit Website.

### Issues faced

| Date | Symptom | Cause | Fix / workaround | Status |
|---|---|---|---|---|
| 11-14 14:47 | Native build impractical; phone overheats | No Mac/Xcode; overheating | Pivot to Android A08 + Windows | **On hold** |
| 11-15 16:03 | Gyro not working in Home Space over Wi-Fi (bridge :8444) | `hmd.html` never requested iOS motion permission in a tap | `requestPermission` on the start tap | **Fixed in code**, untested on the phone |
| 11-15 16:03 | Add to Home Screen not fullscreen | No web-app tags/manifest; certificate untrusted | Manifest + meta tags + local CA install | **Fixed in code**, **untested** on the phone |
| 11-15 16:03 | A08's 0.08 separation would double the image on the iPhone | One value sent to every phone | Per-phone offset in localStorage `pvr_lens` (iPhone 0) | **Fixed** |
| 11-15 23:45 | VR browser: "can't connect to the server" on the iPhone while the A08 worked | The iPhone's Wi-Fi link was stuck: its requests never reached the laptop (watched on the laptop; firewall and server ruled out) | Turn airplane mode on and off on the iPhone | **Fixed** (workaround) |
| 11-15 (review) | Picked tilt instead of gyro if the prompt took >1.2 s | A 1200 ms timer decided the mode before the permission answer | Start sensors after the promise; a later gyro event switches back | **Fixed in code**, untested |
| 11-15 (review) | No game sound until the first touch | Godot creates its AudioContext after the 39.5 MB wasm loads | Resume on the next touch | **Fixed in code**, untested |
| 11-15 (review) | Picture narrowed in landscape | Notch insets (~47 pt/side) | `viewport-fit=cover` | **Fixed** [TEST grep] |
| 11-15 | Screen slept after interruptions | Wake lock lost | Re-acquire on `visibilitychange` | **Fixed in code**, untested |
| 11-15 | Accidental recenter | A two-finger lift counted as a double-tap | Single-finger taps only | **Fixed** |
| 11-15 20:14 | VR-browser videos blurry (also on the A08) | Not the source: Big Buck Bunny really was 1920×1080 [TEST]. Causes: few px/eye, trilinear mipmapping sampling a lower-detail level (~1/3 of a mip step softer), DPR cap 2, and the virtual screen spanning 100° while each eye sees only ~86° | v2: 4-tap sharp sampling, no per-frame mips for the sphere, DPR cap 3 in stereo, cinema span from the lens | **In progress** |

### Open questions

- Home-screen fullscreen with the local CA installed.
- Real-phone check of the motion-permission, audio and wake-lock fixes.
- Overheating under the web modes (not measured).

---

## 6. Headset / viewer and lenses

### Specs

- **VR Shinecon** cardboard-style viewer ("not a Google Cardboard") [USER]. The exact model and lens specs are **unknown**, so there are no per-headset presets.
- Manual knobs for focus and lens position.
- Lens spacing roughly matches the iPhone 12's half-screen centres (~65 mm) [ASSUMED calc].

### Default stereo and lens values [DOC]

| Setting | Godot (`hmd_stereo.gd`) | Phone-hosted / VR browser |
|---|---|---|
| IPD | 0.063 m (`ipd_m`) | 0.063 m (`?ipd=`) |
| Eye FOV | 90° (`eye_fov`); originally Camera3D fov 80 | 80° vertical FOV in stereo, 70° in mono (clamped 50–110°, ±2° per d-pad step) |
| Distortion k1 / k2 | 0.22 / 0.10 (barrel distortion, applied on the phone) | Off on the web build (plain shift) |
| Lens offset (separation) | 0.08 (`hmd.html` clamp −0.15..0.25, key `hmd_lens_offset`) | 0.08 Android / 0 iPhone (localStorage `pvr_lens`) |
| Overscan | 1.4 → render FOV **108.9°** for a 90° view | 1.0 |
| Timewarp | On, latency 90 ms (pose at now − 90 ms) | n/a |
| Vignette | 0.06 | n/a |
| Chromatic aberration | 0 (off) | n/a |

The eye images are 800×720 each in a 1600×720 window.

Tuning keys (Godot):
- `[ ]` separation, `- =` distortion, `, .` zoom, `9 0` FOV, `; '` IPD
- `O/P` overscan, `K/L` latency, `T` timewarp
- `F2` stereo, `F3` lens correction, `F4` framed view, Backspace reset
- Settings are saved to `user://hmd_settings.cfg`.

Distortion grid: `http://localhost:8080/?grid=1`.

### Verified

- [TEST] The lens shader compiles and fills the screen in headless Edge.
- [TEST] Stereo frames are 800×720. The main 3D render is disabled in stereo, so GPU cost stays about flat.
- [TEST] Timewarp direction was checked numerically:
  - Pitch up 5° gave a centre sample at +5.00° (correct).
  - The roll-right 5° check returned y=+0.087 where a negative value was expected. **CONFLICT:** the log shows no follow-up, yet the summary called both directions correct. Treat roll timewarp as **unverified**.

### Issues faced

| Date | Symptom | Cause | Fix / workaround | Status |
|---|---|---|---|---|
| 11-14 16:08 | No split view | Stereo not built yet | Side-by-side stereo + lens shader | **Fixed** |
| 11-14 16:36 | Looked like a framed screen seen from a distance, not immersive [USER] | Phone treated as a flat screen | Overscan 1.4, lens correction on the phone, timewarp inside the margin, clamped and vignetted edges | **Fixed** [USER 17:04 "good enough"] |
| 11-14 16:40 | Tuning labels doubled (one per eye, never fused) [USER] | Labels at fixed screen offsets | Labels centred per eye and re-placed on every change | **Fixed** (laptop-checked; not confirmed in the headset) |
| 11-15 15:35 | A08 double image | See A08 | Lens offset 0.08 | **Fixed** [USER] |
| 11-15 17:41 | Jagged, shimmering image | No MSAA; overscan; resampling; DPR cap | Proposed MSAA 4× laptop / 2× web, supersampling, mipmaps | **Open** |
| 11-15 20:14 | Side-by-side 3D videos shown split | Projection detection bugs ("threed" vs "3d"; mesh not equirect) | v2: reads `sv3d`/`st3d`, format note, title + image test; Y cycles the projection (saved per video). Avatar test file was 1920 wide = **960 px per eye**, which can't be made sharper. | **In progress** |
| 11-15 20:14 | 360° quality poor | Used 1080p avc1; YouTube offers 360° up to 3840 wide in VP9/AV1 | Phone capability probe (MediaCapabilities) + format pick | **In progress** |
| 11-15 | 360° detection | The downloaded MP4 had no spherical metadata | Use YouTube's format note ("1080s, equi") and the title | Works on the laptop; wrap-around untested in the headset |

### Open questions

- Exact headset model, lens FOV and lens spacing.
- Measured FOV and IPD fit.
- Roll timewarp direction.
- Lens distortion correction in the phone-hosted scene (currently plain side-by-side).

---

## 7. Xbox controller

### Connection paths

| Path | How | Status |
|---|---|---|
| **A1 – laptop, Godot reads it directly** (wired/Wi-Fi bridge modes) | XInput. Godot saw `joypads: 1, "device 0: XInput Controller"` [TEST 11-14 17:54] | **Working** [USER + TEST] |
| **A2 – laptop relay to the phone** (phone-hosted, VR browser) | `pad_relay.py` reads XInput (`xinput1_4` → `1_3` → `9_1_0`) at ~120 Hz and streams `/ws/pad` as `pad,<buttons>,<lx>,<ly>,<rx>,<ry>,<lt>,<rt>`: ~60 Hz while anything changes, otherwise a 0.5 s heartbeat; `pad,none` when nothing is connected. Works with USB, an Xbox Wireless adapter or Bluetooth paired to the laptop. | **Working end to end** [USER 11-15 20:24 "A worked for clicking"] |
| **B – Bluetooth paired to the phone** | Gamepad API in the page or the Godot web build (secure context needed) | **Built, untested** |

- The VR browser shows a badge: "Pad: laptop" / "Pad: phone" / "No controller". Orange means the laptop sees no pad; red means the phone lost the link.
- The relay log said `XInput slot 0` (16:14); a reviewer later found the pad on slot 1. All four slots are scanned, so either works.
- Without a controller: drag to turn, tap to use, double-tap to recenter.

### Mappings

- **Games:**
  - Left stick move; right stick turn (140°/s, deadzone 0.15).
  - A interact, Y flashlight.
  - Select recenter, hold Start 1.5 s restart.
  - D-pad for lens tuning on the web.
- **VR browser:**
  - A click / play-pause; B back; X play the page's video in VR.
  - Y reload / switch screen or 360.
  - Sticks scroll/cursor; d-pad seek ±10 s in VR.
  - LB/RB back/forward; Start recenter; View opens the lens panel.

### Issues faced

| Date | Symptom | Cause | Fix | Status |
|---|---|---|---|---|
| 11-14 17:53 | Movement didn't match the view | Bad recenter pose (phone flat) | Recenter only near level | **Fixed** in simulation |
| 11-15 16:13 | Owner wanted controller movement in the phone modes | No path existed | `/ws/pad` relay; movement follows gaze | **Fixed** [USER 20:24] |
| 11-15 (review) | Buttons stuck after a restart or Wi-Fi drop; holding Start restarted every 1.5 s | Only changes were sent | Full-state resend every 0.5 s; release everything on silence; phone reconnects after 5 s of silence | **Fixed**, laptop-tested |
| 11-15 (review) | Walking direction jumped up to 90° when looking almost straight down | Heading undefined near vertical | Keep the last facing direction | **Fixed** |
| 11-15 20:14 | No way to click in VR mode | No pointer UI in VR | v2 player bar + pointer + A, controller-toggled virtual keyboard | **In progress** |

---

## 8. Wi-Fi / network / security

### Ports

| Port | Use |
|---|---|
| 8080 TCP | Wired bridge (via `adb reverse`); also phone_host's HTTP redirect page and `/ca.crt` download |
| 8443 TCP (HTTPS) | phone_host server **or** VR browser (only one at a time) |
| 8444 TCP (HTTPS) | Bridge Wi-Fi mode |
| 8453 TCP | VR browser test instance when 8443 was busy |
| 8090 TCP (localhost) | Home Space media service (WS max 16 MB) |
| 9100 UDP (localhost) | Pose to Godot: 24-byte `<4fd` (quaternion float32 ×4 + timestamp ms float64) or `RECENTER` |
| 9101 UDP (localhost) | Godot lens settings → bridge (JSON) |
| 12345 TCP | Old iOS echo test (parked) |
| dynamic, localhost | Brave DevTools (CDP) |

### Restrictions and facts

- **HTTPS is required** over Wi-Fi: motion sensors, fullscreen, wake lock, the Gamepad API and WebCodecs all need a secure context. Over USB, `localhost` is already secure.
- Self-signed certificates warn again whenever the cert changes (for example when the laptop's LAN address changes), and they rule out a service worker / offline mode. Since 11-15 16:05 a **local CA** signs the server cert:
  - Name-constrained to private ranges and `.local`.
  - CA served at `/ca.crt`; the private key stays on the laptop and is git-ignored.
  - [TEST] A leaf for example.com or a public IP is rejected; the chain verifies with 2 certs.
- **Access key** `?k=` (16-byte `token_urlsafe`, kept between runs in the git-ignored `phone_host/cert/` folder, checked with `hmac.compare_digest`; delete the key file to generate a new one) on bridge `--wifi`, Home Wi-Fi and the VR browser (every `/media` URL too). WebSocket Origin is checked; one video viewer at a time. [TEST 16:36]:
  - Manifest without the key 403, with the key 200.
  - Video WS: no key, wrong key and a foreign Origin are rejected.
  - Pose WS without the key is rejected.
- The phone and the laptop must be on the same LAN (not a guest network). The phone never fetches third-party URLs directly; the laptop proxies them.
- The laptop moved from the router's 2.4 GHz network to its 5 GHz network on 11-15. The A08's 5 GHz support was unconfirmed.
- Wi-Fi latency was **not measured**. The plan estimated +5–20 ms with spikes [ASSUMED]. Bridge video is 8 Mbit/s by default; `--bitrate 5M` is suggested if Wi-Fi stutters (untested).
- YouTube media URLs expire after 6 h and are bound to the laptop's IP. The proxy chunks at 2 MiB with 3 in parallel.

### Issues faced

| Date | Symptom | Cause | Fix | Status |
|---|---|---|---|---|
| 11-14 | Bridge "keepalive ping timeout" | Slow link | Ping 20 s / timeout 60 s | **Fixed** |
| 11-15 14:05 | Phones blocked on the LAN | Windows Wi-Fi profile was **Public** | Owner switched to **Private** at 14:13 | **Fixed** [TEST Get-NetConnectionProfile] |
| 11-15 20:14 | VR browser downloaded the whole video before playing | v1 used a yt-dlp download (file mode) | v2 streams through the `/media` proxy with closed 2 MiB chunks, 3 in parallel | **In progress** |
| 11-15 (review) | Bridge `--wifi` endpoints had no authentication | Design gap | Access key, Origin check, single viewer | **Fixed** [TEST] |
| 11-15 (review) | Cert folder readable by other local accounts; altered or unconstrained CA accepted | Design gap | icacls lock-down, CA validation | **Fixed** |
| 11-15 | Every phone warns once per address until the CA is installed | New certificate | Install the CA (iPhone: open `/ca.crt` → VPN & Device Management → Install → Certificate Trust Settings → enable). Chrome: Advanced → Proceed. | Phone-side install **untested** |

**CONFLICT, firewall state:** an earlier note (11-15) said the Wi-Fi profile was Public and the firewall blocks phones unless it is set to Private. A later README check the same day found the profile **Private**, with Allow-inbound rules for `python.exe`. Allow rules should be limited to the Private profile and to the specific ports (8443/8444/8080). The owner made the switch at 14:13, so the later state (Private) is current. Nothing is changed automatically.

---

## 9. Software versions (checked 2026-11-15)

| Software | Version | Notes |
|---|---|---|
| Godot | **4.7.stable.official.5b4e0cb0f** | Mobile renderer, `config_version=5`, window 1600×720 not resizable. 4.7 templates for all platforms installed 11-15 (earlier only 4.3 templates). Older versions 3.6–4.5.1 also installed. |
| ffmpeg | **9.0.2** full_build (gyan.dev, winget) | Has `gfxcapture`, `ddagrab`, `h264_qsv`, `libx264`, `gdigrab`; ffprobe for durations |
| adb | **1.0.41** / platform-tools 37.0.3 (winget) | Not installed at the start of the project |
| Python | **3.14.0** (system + project `.venv`) | pip 26.1.1 in one interpreter and 25.2 in another (26.3 available) |
| Python packages | fastapi 0.136.1, starlette 1.0.0, uvicorn 0.47.0, websockets 17.4, cryptography 48.0.0, psutil 7.2.2, numpy 2.4.5, sounddevice 0.5.5 | `qrcode` not installed (no QR code printed) |
| yt-dlp | **2026.10.26** (system Python, run as a subprocess) | Installed 11-15 17:47. JS runtime node v24.11.0; deno absent |
| Microsoft Edge | **157.0.4341.60** | Headless Home Space browser and screenshot tests (`--headless=new --use-angle=swiftshader`) |
| Brave | **157.1.99.57** | VR browser (headless, persistent profile) |
| Chrome (phone) | not recorded | A08 |
| Chrome (laptop) | installed, version not recorded | |
| GitHub CLI | not installed (11-14) | Repo pushed by the owner |

---

## 10. Chronological issue timeline

| When | Device | Issue | Status |
|---|---|---|---|
| Before 11-14 | Laptop + phone | SteamVR phone bridge lagged and heated both devices [USER] | Reason for this project |
| Planning | Laptop | NVENC impossible on the MX130 → QSV | Fixed |
| 11-14 14:47 | iPhone 12 | No Mac; overheats → pivot to the A08 | On hold |
| 11-14 14:53 | Bridge | FLV parser / end-of-stream bugs | Fixed |
| 11-14 15:20 | A08 | Accelerometer only → tilt mode | Workaround; yaw open |
| 11-14 15:20 | Laptop | ffmpeg not on PATH | Fixed |
| 11-14 15:20 | A08 Chrome | Stale tab, no video | Fixed |
| 11-14 15:36 | Bridge | Partial pipe reads ended the stream | Fixed |
| 11-14 15:38 | Laptop | `ddagrab` fails on Optimus | Open (superseded) |
| 11-14 15:39 | A08 | Pitch inverted | Fixed |
| 11-14 16:08 | A08 | Play-test: 30 fps, 0 drops, in sync [USER] | — |
| 11-14 16:12 | Laptop | Captured VS Code instead of the game → gfxcapture | Fixed |
| 11-14 16:12 | Bridge | Keepalive timeout crash | Fixed |
| 11-14 16:17 | A08 | Fullscreen needs a touch → adb tap | Fixed (wired) |
| 11-14 16:36 | Headset | Not immersive → overscan + phone lens + timewarp | Fixed |
| 11-14 16:40 | Headset | Doubled tuning labels | Fixed |
| 11-14 17:33 | Game | Doors blocked the player; text never faded | Fixed |
| 11-14 17:53 | A08 + pad | Walk direction ≠ view | Fixed (simulation) |
| 11-15 13:55 | A08 | View rolled 180° → auto-flip | Fixed in code |
| 11-15 14:05 | Network | Public Wi-Fi profile blocked phones | Fixed |
| 11-15 15:35 | A08 + headset | Double image → offset 0.08 | Fixed |
| 11-15 15:52 | Laptop | Godot WebSocket frames 49 → 2.4 fps → TCP | Fixed |
| 11-15 15:52 | Laptop | Google captcha in headless Edge | Workaround |
| 11-15 15:58 | Laptop | Template download stall | Fixed |
| 11-15 16:03 | iPhone | No gyro (permission); home screen not fullscreen | Fixed in code, untested |
| 11-15 16:06 | Laptop | Web export configuration error | Fixed |
| 11-15 16:08–16:27 | Web game | Pointer lock, int/bool bridge bug, audio worklet errors | Fixed / intermittent |
| 11-15 16:35 | Laptop | icacls self-lockout | Fixed |
| 11-15 16:36 | Network | Wi-Fi endpoints had no auth → access key | Fixed |
| 11-15 17:41 | Phones | Jagged / shimmering image | Open (deferred) |
| 11-15 18:14 | Tooling | Interrupted session left processes running | Resolved |
| 11-15 19:29 | Laptop | Port 8443 clash; split Python; console encoding | Workarounds |
| 11-15 20:14 | Both phones | VR video blurry; 360 poor; SBS split; download instead of stream; no click in VR | In progress (v2) |
| 11-15 20:24 | Controller | Laptop relay confirmed on the phone [USER] | Working |

---

## 11. Lessons / rules of thumb

1. **Check the encoder before planning.** The MX130 has no NVENC. On this laptop use Quick Sync (`h264_qsv`, 142 fps at 1600×720) with `libx264 ultrafast` as the fallback.
2. **On Optimus laptops, capture by window, not by screen.** `gfxcapture` (~52 fps, ignores covering windows and scaling) beats `gdigrab` (~30–42 fps), and `ddagrab` fails on the wrong GPU.
3. **Check the phone's sensors before choosing a phone.** The Galaxy A08 has **no gyroscope or magnetometer**, so it gives tilt only and no yaw. A budget phone may be accelerometer-only; run `check_phone.bat` first.
4. **Mobile browsers need a secure context** for sensors, WebCodecs, fullscreen, wake lock and the Gamepad API. Use `localhost` via `adb reverse` over USB, or HTTPS with an installed local CA over Wi-Fi.
5. **iOS motion permission must be requested inside a tap**, and no events fire until it is answered. Never decide gyro vs tilt on a timer.
6. **Safari has no page fullscreen.** Use Add to Home Screen with a manifest and a trusted certificate.
7. **Chrome fullscreen needs a real touch.** Over USB, `adb shell input tap` can provide it, but a PIN lock still needs a hand.
8. **Lens offset depends on screen size.** A 6.7" screen (A08) needs ~0.08 separation; a 6.1" screen (iPhone 12) needs 0. Store it per phone.
9. **Recenter only when the phone is roughly level.** A pose taken with the phone lying flat gives a meaningless heading.
10. **Don't push big frames through Godot's WebSocketPeer.** It cost ~50 ms per 2.7 MB frame; use a plain TCP socket and keep only the newest frame.
11. **Blur is usually the render path, not the source.** A 1080p source looked blurry because of DPR caps, mipmaps and too few pixels per eye. Side-by-side 3D at 1920 wide is only 960 px per eye.
12. **Set the Windows Wi-Fi profile to Private** (or allow the server through the firewall) before testing phones on the LAN.
13. **Lock down Wi-Fi endpoints.** Use an access key, an Origin check and a name-constrained local CA, and keep keys git-ignored.
14. **Measure what is still unmeasured:** glass-to-glass latency (first-pass target <100 ms), device temperatures, Wi-Fi latency and battery drain.

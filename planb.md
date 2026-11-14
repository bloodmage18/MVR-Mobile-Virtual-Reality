# Plan B — Web-Based HMD (Browser as Client)

> Fallback if the native iOS app route in plan.md hits a hard wall.  
> Same USB tunnel foundation (Phase 0) applies — nothing from plan.md is wasted.

## Architecture
```
Laptop: Godot renders → FFmpeg encodes → streams video (WebRTC or MJPEG)
iPhone Safari: receives video stream → displays full screen
iPhone: DeviceOrientationEvent → WebSocket → Laptop → Godot camera
Laptop: keyboard input → Godot directly (no relay needed)
```

---

## Why This Works
- No Xcode, no Apple Developer account, no sideloading
- Godot 4 doesn't change — same scene, same encoder
- Python bridge swaps native socket server for a small WebSocket + HTTP server
- Phone stays as a dumb screen — laptop still does all rendering (no heat problem)

## The One Catch — iOS HTTPS Requirement
Safari blocks `DeviceOrientationEvent` (gyroscope) unless served over HTTPS.

**Fix:** USB tunnel from Phase 0 solves this.  
`pymobiledevice3` tunnels the port so iPhone accesses it as `localhost`.  
Safari treats `localhost` as a secure context — DeviceMotion works, no cert needed.

**Fallback fix (Wi-Fi):** Use `mkcert` to generate a locally trusted cert.

---

## What Changes vs plan.md

| Component | plan.md (Native) | Plan B (Web) |
|---|---|---|
| iOS client | Swift app (Xcode) | Safari browser tab |
| Video decode | VideoToolbox (hardware) | Browser `<video>` tag |
| Gyro access | `CMDeviceMotion` (Swift) | `DeviceOrientationEvent` (JS) |
| Warp/ATW | Metal shader | Not available in browser |
| Python bridge | Raw TCP socket server | WebSocket + HTTP server |
| Expected latency | ~30–50ms | ~80–150ms |

---

## Likely Trigger Points to Switch to Plan B
- USBMuxd / `pymobiledevice3` tunnel won't stabilize on Windows
- Xcode sideloading blocked or expiring every 7 days becomes too disruptive
- VideoToolbox integration too complex before having a working pipeline to test against

---

## Stack for Plan B
- **Python:** `websockets` + `http.server` or `FastAPI` for the bridge server
- **Browser client:** Plain HTML + JS (no framework needed)
  - `<video>` or `<canvas>` for frame display
  - `DeviceOrientationEvent` listener → WebSocket sender
- **Streaming format:** MJPEG over HTTP (simplest) or WebRTC (lower latency)

---

## Latency Expectation
Good enough for the horror game. Not good enough for ATW (Phase 3 of plan.md becomes irrelevant here — browser has no Metal access). If latency is acceptable without ATW, ship it. If not, go back to native.

---

# Implementation Phases

> Each phase has an **Android track** and an **iOS track**.  
> Complete Phase 0 first — it's the foundation for everything else.

---

## Phase 0 — USB Tunnel & Toolchain Verify (Shared Setup)

**Goal:** Prove the phone can talk to the laptop over USB before writing any app code.

### Android
- Install ADB (Android Debug Bridge) — part of Android Platform Tools
- Enable Developer Mode + USB Debugging on the phone
- Run: `adb reverse tcp:8080 tcp:8080` and `adb reverse tcp:8765 tcp:8765`
- Open Chrome on phone → navigate to `http://localhost:8080` → verify it hits a Python test server on the laptop
- **No cert needed** — Chrome on Android allows DeviceOrientationEvent over localhost HTTP

### iOS
- Install `pymobiledevice3` on the laptop (`pip install pymobiledevice3`)
- Enable Developer Mode on iPhone (Settings → Privacy → Developer Mode)
- Run: `pymobiledevice3 remote start-tunnel` to open the USB tunnel
- Open Safari on iPhone → navigate to `http://localhost:8080` → verify it hits the same Python test server
- Safari treats `localhost` as a secure context — DeviceMotion will work without HTTPS

**Done when:** Phone browser loads a "hello world" HTML page served from your laptop over USB.

---

## Phase 1 — Python Bridge Server

**Goal:** One Python process that serves the browser client, streams MJPEG frames, and receives gyro data over WebSocket.

### Shared (same code for both platforms)
- Stack: `FastAPI` + `websockets` (or just `http.server` to keep it minimal)
- Endpoints:
  - `GET /` → serve `client.html`
  - `GET /stream` → MJPEG stream (multipart/x-mixed-replace)
  - `WS /gyro` → receive `{alpha, beta, gamma}` JSON from the phone, forward to Godot
- Godot side: replace the existing TCP socket listener with a UDP or local pipe that reads from this bridge

### Android difference
- Port forwarding stays active only while ADB is connected — add a reconnect loop or shell script to re-run `adb reverse` if the cable is unplugged

### iOS difference
- The `pymobiledevice3` tunnel must be running before the bridge starts — add a startup check or a batch script that launches the tunnel then the server

**Done when:** Laptop Python server starts without errors and the `/stream` endpoint returns a static MJPEG test image.

---

## Phase 2 — Browser Client (client.html)

**Goal:** A single HTML file that displays the video stream full-screen and sends gyro data back.

### Shared structure
```html
<video id="v" autoplay playsinline style="width:100%;height:100%"></video>
<!-- OR -->
<img id="frame" style="width:100%;height:100%">
```
- For MJPEG: set `<img src="/stream">` — browser handles the multipart decode automatically
- For WebRTC (later phase): use `RTCPeerConnection`
- DeviceOrientation listener → WebSocket sender:
```js
const ws = new WebSocket("ws://localhost:8765/gyro");
window.addEventListener("deviceorientation", e => {
  ws.send(JSON.stringify({a: e.alpha, b: e.beta, g: e.gamma}));
});
```

### Android (Chrome)
- DeviceOrientationEvent fires immediately — no permission prompt needed on Android
- Add `<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">`
- Fullscreen: use `document.documentElement.requestFullscreen()` on a tap event
- Test in Chrome DevTools device emulator first before deploying to phone

### iOS (Safari)
- Safari requires a user gesture before granting DeviceMotion — add a "Tap to Start" overlay button that calls `DeviceMotionEvent.requestPermission()` then hides itself
```js
DeviceMotionEvent.requestPermission().then(state => {
  if (state === "granted") startGyroStream();
});
```
- Safari does not support `requestFullscreen` — use CSS `position:fixed; top:0; left:0; width:100vw; height:100vh` instead
- Disable scroll bounce: `document.body.style.overflow = "hidden"` and `touch-action: none`

**Done when:** Phone browser shows a live MJPEG feed from the laptop and gyro values appear in the laptop terminal.

---

## Phase 3 — Godot Integration

**Goal:** Godot camera follows the phone's orientation in real time.

### Shared
- Godot GDScript reads gyro data from the Python bridge (via local socket, pipe, or shared variable)
- Map `beta` → camera pitch, `gamma` → camera roll, `alpha` → camera yaw
- Apply smoothing (lerp) to avoid jitter from gyro noise
- Godot renders the scene → FFmpeg captures the Godot window → pipes frames to the Python bridge → phone receives them

### Android difference
- Android gyro axes may differ from iOS — test and remap if the camera feels inverted
- Android Chrome may throttle WebSocket when the tab is backgrounded — keep the screen on (`WakeLock API`) and keep the tab in foreground

### iOS difference
- Safari's DeviceOrientation uses a different coordinate frame — alpha is compass-relative on iOS; subtract the initial heading on first connect to normalize it
- Add a recalibrate button in the client that resets the heading offset

**Done when:** Moving the phone rotates the Godot camera with visible, low-latency response.

---

## Phase 4 — Latency & Quality Tuning

**Goal:** Get perceived latency as low as possible within the browser constraint (~80–150ms target).

### Shared
- Tune FFmpeg capture: lower resolution first (720p → 480p) and raise fps (30fps → 60fps) — lower res at higher fps often feels better for VR
- MJPEG quality: start at `q:v 5` (low compression = less latency), tune upward until bandwidth allows
- WebSocket gyro rate: cap at 60 messages/sec — higher doesn't help and wastes bandwidth

### Android upgrade path
- Chrome on Android supports WebRTC — switch from MJPEG to WebRTC for ~30–50ms lower latency
- Use `aiortc` (Python) as the WebRTC peer on the laptop side

### iOS upgrade path
- Safari supports WebRTC — same `aiortc` path works
- Test with Safari's built-in WebRTC internals (`Develop → WebRTC`) to inspect frame timing

**Done when:** Latency feels tolerable for the horror game (head turn → camera update feels connected, not slidey).

---

## Phase 5 — Polish & Stability

**Goal:** Make it reliable enough to actually play-test the game.

### Android
- Auto-reconnect WebSocket if connection drops
- Keep-screen-on via `navigator.wakeLock.request("screen")`
- Add a battery/connection status indicator in the corner of the client
- Package the ADB reverse command into a `.bat` file for quick setup

### iOS
- Auto-reconnect WebSocket
- Prevent Safari from dimming the screen: use a silent audio context trick (`AudioContext` with a silent buffer playing) or keep a video element playing to suppress sleep
- Package the pymobiledevice3 tunnel + Python server into a single `.bat` startup script
- Document the 7-step startup sequence (tunnel → server → Safari → tap to start → fullscreen)

**Done when:** Can sit down, run one script, open the browser, and be in the game within 60 seconds.

---

## Phase Summary Table

| Phase | Goal | Android blocker | iOS blocker |
|---|---|---|---|
| 0 | USB tunnel working | ADB not found / USB debugging off | pymobiledevice3 tunnel fails |
| 1 | Python bridge serving | ADB reverse drops on reconnect | Tunnel must pre-start |
| 2 | Browser client live | None — Chrome is permissive | DeviceMotion permission prompt |
| 3 | Godot camera tracks head | Axis remapping | Compass-relative alpha offset |
| 4 | Latency acceptable | WebRTC via aiortc | WebRTC via aiortc (same) |
| 5 | Stable play-test | ADB bat script | 7-step startup, screen-sleep fix |

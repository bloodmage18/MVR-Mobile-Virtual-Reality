# Cross Reality HMD: Android + Windows Plan (v3, 2026-11-14)

> iOS is **on hold** (no Mac, iPhone 12 overheats). Old material is in `on_hold_ios/`.
> This plan replaces `plan.md`. `planb.md`'s Android track is folded in here.

## Hardware (checked on this laptop)

| Device | Spec | What it means |
|---|---|---|
| Laptop CPU | Intel i7-8650U, 4C/8T, 15.9 GB RAM | x264 software encode is possible but runs hot |
| Laptop iGPU | Intel UHD 620 | **Intel Quick Sync** H.264 encoder (`h264_qsv`): the main encoder |
| Laptop dGPU | NVIDIA GeForce MX130 | **No NVENC on this chip.** The old plan's NVENC step could never have worked here |
| Phone | Samsung Galaxy A08 **SM-A085F**, MediaTek MT6789 (Helio G99), Android 17, 1600×720 @ 90 Hz | Hardware H.264 decode ✔. **Accelerometer only: no gyroscope, no magnetometer** (Phase 0 result) |
| Godot | 4.7 at `E:\Godot` | Use the **Mobile** or **Compatibility** renderer (weak GPUs, less heat) |
| Link | USB cable + `adb reverse` | Phone's `localhost:8080` reaches the laptop. No Wi-Fi, no HTTPS cert, no app install |

## Review: what changed and why

1. **Android removes the iOS bottleneck.** `adb reverse` (one command) replaces iTunes drivers, pymobiledevice3, Xcode and sideloading.
2. **NVENC → Quick Sync.** Encode on the Intel iGPU. This also keeps the MX130 free for rendering.
3. **The browser path is not a dead end.** `planb.md` said "no timewarp in a browser". On Android Chrome that's wrong: **WebCodecs** gives hardware H.264 decode, and **WebGL** can do rotational reprojection. A native app becomes an *optional* upgrade, not a requirement.
4. **Exact frame boundaries.** ffmpeg outputs FLV, so the bridge forwards each frame the moment it's encoded, with no one-frame parsing delay.
5. **Biggest risk is now the phone's gyroscope.** Several budget Samsung A0x models ship without one. Phase 0 answers this before anything else is built.
6. **STT scripts** (`stt.py`, `stt_live.py`) are a separate Whisper tool, already Windows-native. Fixed one bug: a `null` `transcript_suffix` produced files named `transcriptNone_…`.

## Architecture

```
 WINDOWS LAPTOP                                                ANDROID PHONE (Chrome, in headset)
 ┌───────────────────────────────┐                             ┌──────────────────────────────┐
 │ Godot 4.7 game (window)       │                             │ hmd.html                     │
 │  Head node ← hmd_head.gd ◀────┼── UDP :9100 ──┐             │  RelativeOrientationSensor   │
 │  stereo SBS + lens shader     │               │             │  (gyro+accel fusion, 60 Hz)  │
 └──────────────┬────────────────┘               │             │        │ WS /ws/pose         │
                │ screen capture                 │             │        ▼                     │
 ┌──────────────▼────────────────┐   ┌───────────┴───────┐ USB │  WebCodecs H.264 decode ◀─┐  │
 │ ffmpeg  gdigrab → h264_qsv    ├──▶│ hmd_bridge.py     │◀═══▶│  → canvas (→ WebGL warp)  │  │
 │ (FLV over pipe)               │   │ FastAPI :8080     │ adb │        WS /ws/video ──────┘  │
 └───────────────────────────────┘   └───────────────────┘     └──────────────────────────────┘
 Keyboard/mouse/controller → Godot directly (no relay)
```

## Built today (ready to run)

| File | Purpose |
|---|---|
| `check_phone.bat` | Phase 0 in one click: USB link, hardware sensor list, opens the check page on the phone |
| `start_hmd.bat` | Bridge + phone link + HMD view. Pass `--video test` / `--video desktop …` |
| `bridge/adb_setup.py` | Finds adb, checks authorization, `adb reverse`, `--sensors` (reads Android's sensor list), `--watch` (re-forwards on replug) |
| `bridge/hmd_bridge.py` | HTTP pages, pose WebSocket → UDP to Godot, video WebSocket (ffmpeg → H.264 frames, drops to next keyframe when the phone lags) |
| `bridge/web/check.html` | Tests gyro, sensors, rates, screen size and refresh, WebCodecs, and wake lock. Report prints on the laptop and saves to `bridge/sensor_report.json` |
| `bridge/web/hmd.html` | Fullscreen, landscape lock, wake lock, pose sender (gyro, falls back to compass), H.264 decoder. Tap toggles stats; double-tap recenters |
| `godot_project/scripts/hmd_head.gd` | Drop-in Godot node: applies the phone pose to a Head node, yaw recenter, optional smoothing |

Tested on the laptop with a simulated phone: pages, pose → UDP packet, recenter, sensor report, and the FLV frame parser. **Not yet tested:** a real phone, a real ffmpeg encode, and the axis mapping in Godot.

---

## Phase 0: Phone link + sensor check (do first)

One-time setup on the laptop:
```
winget install Google.PlatformTools     # adb
winget install Gyan.FFmpeg              # ffmpeg (needed from Phase 1)
```
On the phone:
1. Enable developer mode (Settings > About phone > Software information > tap Build number 7×).
2. Turn on USB debugging (Developer options).
3. Install or update Chrome.
4. Plug in the cable and tap **Allow**.

Run `check_phone.bat`.

**Done when:** the laptop prints a **SENSOR CHECK REPORT** and the verdict line. The page also reports the screen's real pixel size. Use it for `--size` (default guess is 1600×720).

### Phase 0 result (2026-11-14): accelerometer only
The A08 reports **only an accelerometer** (no gyroscope, no magnetometer, no fusion sensors). The accelerometer measures gravity, so it can sense **tilt (pitch and roll)**. It **cannot sense turning (yaw)** at all. Video decoding is fine (hardware H.264), and the screen is 1600×720 at 90 Hz.

| Option | Quality | Cost/effort |
|---|---|---|
| **A. Tilt mode, now the default on this phone**: phone gives pitch + roll; mouse/controller turns the body. `hmd.html` switches to it automatically (HUD shows `tilt`) | Look up/down and lean work. Turning is by hand. Sudden head moves cause small wobble (smoothed by `TILT_SMOOTH` in `hmd.html`) | Free, works today |
| **B. External IMU on the headset (recommended for real head tracking)**: ESP32-C3 or Pro Micro + **BNO085** (or MPU-6050), USB serial to the laptop, bridge forwards to the same UDP port | Full 3-DOF, 100–400 Hz, on-chip fusion. Better than most phone gyros | ~$10–20, small Python addition (`pyserial`) |
| C. A second-hand phone with a gyro as a dedicated "tracker" clipped to the headset (sends pose only, A08 stays the display) | Full 3-DOF via the same `hmd.html` page | Any old Android phone with a gyroscope |

Decision: build and play-test with **A** now; order a BNO085 board for **B**. B needs no changes to Godot: it feeds the same UDP pose packet.

### Capture/encode measurements (this laptop)
- Quick Sync H.264 1600×720: **142 fps** encode. Fine.
- `gdigrab` desktop capture: **~42 fps max**. Through the full bridge pipeline: ~37 fps at `--fps 40`. This is the bottleneck.
- `--video dda` fails with "Selected output not supported": the dual-GPU (Optimus) problem. ffmpeg runs on the MX130, but the screen belongs to the UHD 620. Fix: Settings > System > Display > Graphics > add `ffmpeg.exe` > **Power saving**.
- **Now the default: `--video game`** (FFmpeg `gfxcapture`, Windows Graphics Capture). The bridge finds the game window by title (skipping the editor's floating Game window) and captures only its picture. It works when the window is covered or moved, and ignores the 125% display scaling (screen is 1920×1080 physical, 1536×864 scaled). Measured end to end: **~52 fps** to the phone, vs ~30 fps for desktop capture. Desktop-region capture showed VS Code on the phone whenever it covered the game.

### Hands-free start (headset stays on)
- The phone page asks the bridge for a tap whenever it isn't fullscreen. The bridge sends a real touch over adb (`input tap`), which Chrome accepts for fullscreen and landscape lock. It also wakes the screen first and dismisses a swipe lock (a PIN still needs a hand).
- Bridge window keys: **Enter** = tap again, **r + Enter** = reload phone page, **c + Enter** = recenter.
- Stats overlay shows for 4 s after start, then hides (tap once to toggle).
- Tip: Developer options > **Stay awake** (while charging) stops the phone sleeping in the headset.

### Play-test 1 results (2026-11-14)
- Phone tilt + mouse turning work in Godot (pose 60 Hz over USB).
- Pitch felt inverted. Logged poses showed Chrome's landscape "screen" frame rotated 180° (view upside-down). `hmd.html` now flips landscape readings; `?flip=0` disables it. Godot also has `invert_pitch` / `invert_roll` on the Head node.
- Video failed: bridge bug (partial pipe reads ended the stream after the decoder config). Fixed and verified with real ffmpeg: test, desktop, QSV and x264 all stream frames.

## Phase 1: Video to the phone
1. `start_hmd.bat --video test`: ffmpeg test pattern. Check the phone HUD for ~60 fps and drop count.
2. If Quick Sync errors: `start_hmd.bat --video test --encoder x264`. Update the Intel graphics driver and retry QSV later.
3. `start_hmd.bat --video desktop --offset 0,0 --size 1600x720` mirrors the top-left desktop region.

**Done when:** a stable 60 fps on the phone with no growing delay. Measure glass-to-glass latency: film both screens showing a stopwatch with another camera. Target < 100 ms in this first pass.

## Phase 2: Godot project + head tracking
**Built:** `godot_project/` (Godot 4.7, Mobile renderer, 1600×720 window titled `Cross Reality HMD`). Test room: dark floor, flashlight, and four colored pillars as landmarks (red = ahead, green = right, blue = behind, yellow = left).
- Scene: `Player (CharacterBody3D, hmd_player.gd)` → `Head (Node3D, hmd_head.gd)` → `Camera3D` + flashlight.
- **Controls:** mouse X / right stick turn left-right (body yaw). WASD / arrows / left stick move. **Phone tilt** drives look up/down + roll. If no phone pose arrives for 0.5 s, mouse Y / right stick Y take over up/down. R / controller Select = recenter. Esc releases the mouse; click recaptures it.
- Tune in the Inspector: `mouse_sensitivity`, `invert_mouse_y`, `stick_turn_speed`, `move_speed` (Player); `smoothing` (Head).
- Checked headless: imports and runs with no script errors; switches to phone tracking when poses arrive.
- **Verify axes on the real phone:** tilt your head back → the view must go up; lean to the right ear → horizon tilts the matching way. If one is inverted, adjust `_world_fix` in `hmd_head.gd` (the only place to change).

**Done when:** head tilt + mouse turning move the camera, and the image reaches the phone.

## Phase 3: Stereo + lens (headset mode)
**Built (2026-11-14):** `scripts/hmd_stereo.gd` (`HMDStereo` node in `main.tscn`) + `shaders/hmd_lens.gdshader`. Verified by rendering real frames: two 800×720 eye views, barrel distortion, lens centers shifted toward the nose. The main window's own 3D render is switched off in stereo, so the GPU cost is about the same as flat.

Play-test 2 (before stereo): tilt correct, video 30 fps with 0 drops, no noticeable delay.

Live tuning keys (saved to `user://hmd_settings.cfg`):

| Key | Setting | Fixes |
|---|---|---|
| F2 | Stereo on/off | Back to the flat Windows view |
| F3 | Lens distortion on/off | Compare with/without |
| `[` `]` | Lens separation | Double vision, eye strain |
| `-` `=` | Distortion strength | Straight walls curving at the edges |
| `,` `.` | Zoom | Black edges visible, or image cut off |
| `9` `0` | Field of view | World feels too big/small when turning |
| `;` `'` | Eye distance (3D depth) | Depth too flat or too strong |
| Backspace | Reset | |

**Immersive mode (2026-11-14, default):** the user's idea. The eye image should run past the screen edges, like a big screen close to the eyes, not a framed screen seen from a distance.
- Godot renders each eye **wider than the lens shows** (`overscan`, default 1.4, which makes render FOV 108.9° for a 90° view) and sends it undistorted.
- The phone (`hmd.html`, WebGL) traces every pixel through the lens model, applies **timewarp** (rotates by head movement since the frame was rendered, from its own sensor history and `latency_ms`), and looks it up in the wide image. The screen is always filled, edges fade softly, and head movement shows at the phone's refresh rate.
- Godot sends settings to the bridge (UDP 9101), which forwards them to the phone. New keys: **F4** lens on phone/laptop, **T** timewarp, **O/P** overscan, **K/L** latency.
- Tuning labels now fuse into one (same position relative to each eye's lens center).
- `hmd.html?grid=1` shows a test grid instead of video, for tuning distortion (lines should look straight).
- Verified: shader compiles and renders in headless Edge (screen filled, overscan edge never visible); timewarp direction checked numerically (pitch and roll).

Still to do: chromatic aberration (`chroma` uniform exists, default off), and per-headset presets once the headset model is known.

## Phase 4: Latency and heat tuning
- **Capture:** move from `gdigrab` to `ddagrab` (Desktop Duplication) + `hwmap` to QSV. Frames stay on the Intel GPU, so CPU load drops a lot.
- **Prediction:** send the pose timestamp back with each frame; predict 30–50 ms ahead from angular velocity (see ALVR `motion_prediction`).
- **Rotational timewarp in the phone browser:** switch the client canvas from 2D to WebGL. Draw each frame on a slightly oversized quad and rotate it by (current pose − pose the frame was rendered with). This is the big comfort win.
- **Rates:** try 1280×576 or 45 fps if the phone or laptop throttles; lower bitrate before resolution.

## Phase 5: The horror game
- One Godot project, three run modes chosen at startup:
  1. **Desktop**: normal Windows game, KB/M or controller, flat screen.
  2. **Phone HMD**: stereo + lens + `hmd_head.gd` (this plan).
  3. **PCVR (later, optional)**: Godot's built-in OpenXR. Any SteamVR/OpenXR headset works without changing game logic. (Windows Mixed Reality is discontinued, so skip it.)
- Export with the standard Windows export template. Keep game logic independent of the mode (Head node + input actions), so modes are just different rigs.

### Level 1 built (2026-11-14): the abandoned school, presence only
Based on the EZ Weekly Dev Challenge project (`E:\Projects\GitHub\ez-weekly-dev-challaendge`): its school timetable (data.csv, TimeOfDay, Time_Table with the 16:00-06:00 **DIMENSIONAL** period), its room set (classrooms, staff room, corridor, assembly ground) and its classroom concept sketch. Its fixed per-room cameras and the chasing students/teacher are kept for the later "visible threat" phase.
- `school.tscn` is now the main scene; `main.tscn` is still the test room.
- **Level = text map** in `scripts/school/school_builder.gd` (edit the characters to change the building). 6 rooms: 5 classrooms + staff room, long corridor, locked gate to the assembly ground. The key spawns in one of 3 random spots.
- **Presence director** (`presence_director.gd`): distant footsteps into a classroom, the door that shuts behind you, a locker bang, chair scrapes and whispers, the hourly bell announcing daytime lessons at night, a blackout when you take the key, and footsteps that follow you and stop when you turn to look.
- **Sounds** are all generated by `tools/make_sounds.py` (no downloads, no licences).
- **Controls:** WASD/left stick move, mouse/right stick turn, phone tilt looks up/down, E/click/A interact, F/Y flashlight, R/Select recenter, F10/hold Start restart.
- Verified: headless run has no script errors; the logic test (gate locked, take key, unlock, open, escape) passes; screenshot tour checked (`tools/screenshot_tour.gd`).

## Phase 6: Media viewer
Video and book library in a dark virtual room using the same pipeline. Possibly a phone-side mode later (local video playback with the head-tracked quad in WebGL), so the laptop doesn't render at all.

## Upgrade path (only if Phase 4 isn't good enough)
**Native Android client.** A Godot 4.7 Android export (or Kotlin) with MediaCodec decode, raw sensors at 200+ Hz, and Vulkan or GLES timewarp. Fully buildable on Windows (Android SDK + JDK 17), no Mac. The bridge protocol stays the same, so nothing on the laptop changes.

## Heat
- **Phone:** the browser client only decodes video, the cheapest job. Lower the brightness. Plain USB charging during play adds heat; if it gets hot, unplug charging power via a data-only setup or run on battery for short sessions.
- **Laptop:** Quick Sync encode + Mobile renderer + 60 fps cap. Avoid x264 at 60 fps for long sessions on a 15 W U-series CPU.

## Open questions
- ~~Does the A08 have a gyroscope?~~ No: accelerometer only (tilt mode + external IMU plan).
- ~~Exact A08 screen size and refresh rate~~ 1600×720, 90 Hz.
- Does `--video dda` work once ffmpeg is set to "Power saving" GPU? If yes, it replaces gdigrab.
- Which headset/lens: needed for distortion constants (Phase 3).
- Does Godot render on the MX130 or the UHD 620? Test both; the iGPU may be fine for a dark low-poly scene and simplifies capture.

# Cross Reality HMD — Project Plan

## Core Architecture
```
Godot → NVENC (GPU) → Python Bridge → USBMuxd → iPhone (VideoToolbox decode) → Display
                                          ↑
                       iPhone gyro (CMDeviceMotion) ←────────────────────────────┘
```

**Hardware:** Laptop (NVIDIA GPU) + iPhone 12 + Lightning cable + Mobile VR headset mount  
**Stack:** Godot 4, Python, Swift (Xcode), VS Code

---

## Phase 0 — USB Tunnel
**Goal:** Bytes flowing reliably from laptop to iPhone over Lightning cable.  
**Nothing else starts until this works.**

| Task | Tool | Notes |
|---|---|---|
| Install iTunes (Apple Mobile Device Support) | Windows | Required for USBMuxd drivers |
| Set up free Apple Developer account | developer.apple.com | Needed to sideload iOS app |
| Install `pymobiledevice3` | Python | The bridge library |
| Write minimal iOS app — TCP echo server | Swift (Xcode) | Listens on a port, echoes data back |
| Write Python script that connects and sends "hello" | Python | Proves tunnel works over USB |

**Exit criteria:** Python sends a string, iPhone receives and logs it. No Wi-Fi involved.

---

## Phase 1 — One-Way Video Stream
**Goal:** A Godot scene appears on the iPhone screen. No tracking yet, just a live picture.

| Task | Tool | Notes |
|---|---|---|
| Godot test scene | Godot 4 | Spinning cube or room — something with visible motion |
| Screen capture via `RenderingServer` | Godot 4 GDScript | Start here before GDExtension |
| Encode with FFmpeg + NVENC | Python subprocess | `h264_nvenc`, `zerolatency` tune, Baseline Profile |
| Stream over USB tunnel | Python | Raw TCP packets |
| iOS: decode with `VTDecompressionSession` | Swift | VideoToolbox hardware decode |
| iOS: display decoded frames | Swift + Metal/UIKit | Full-screen display |

**Exit criteria:** Godot renders at 60fps, iPhone displays it. Measure latency — aim for <100ms first pass.

---

## Phase 2 — Tracking Loop
**Goal:** Moving the iPhone moves the Godot camera.

| Task | Tool | Notes |
|---|---|---|
| Read `CMDeviceMotion` on iPhone | Swift | Quaternion orientation at 100Hz |
| Send gyro data back over USB tunnel | Swift | Separate data channel or multiplexed |
| Receive and parse in Python | Python | Forward to Godot via local socket |
| Apply quaternion to Godot camera | Godot 4 GDScript | Camera rotation |
| Kalman filter / smoothing | Python or GDScript | Reduce jitter |

**Exit criteria:** Head rotation drives camera with no perceptible lag.

---

## Phase 3 — Async Timewarp (ATW)
**Goal:** Sub-20ms motion-to-photon. iPhone warps the last received frame to match current head position while the next frame is in transit.

| Task | Tool | Notes |
|---|---|---|
| Render decoded frame to a quad in Metal | Swift + Metal | Frame becomes a texture on a plane |
| Apply warp matrix from latest gyro | Swift + Metal shader | Rotate quad to compensate for head movement |
| Prediction: extrapolate head position 15ms forward | Swift | Use angular velocity from gyro |
| Tune and measure latency | Timestamp logging | Aim for <20ms motion-to-photon |

**Exit criteria:** Wearing the HMD feels stable — no swimmy/lag sensation on head movement.

---

## Phase 4 — Horror Game
**Goal:** Playable game experience through the HMD.

| Task | Tool | Notes |
|---|---|---|
| 3D horror environment | Godot 4 | Corridors, lighting, atmosphere |
| Barrel distortion shader | Godot 4 | Compensate for VR lens in headset mount |
| Stereo rendering (side-by-side) | Godot 4 | Left/right eye views for lens mount |
| Keyboard + mouse / controller input | Godot 4 | Standard input handling |
| Basic game loop | Godot 4 | Simple horror mechanic to test immersion |

**Exit criteria:** Phone in headset mount, game is playable and immersive.

---

## Phase 5 — Media Viewer (Post-Game)
Video library and book library viewed through HMD. Separate "app mode" built after game pipeline is stable. Shares all Phase 0–3 infrastructure.

---

## Repo Structure
```
Cross Reality Project Test/
├── godot_project/              ← Godot 4 project
│   ├── scenes/
│   └── shaders/
├── bridge/                     ← Python: USB tunnel + encoding
│   ├── usb_tunnel.py
│   ├── encoder.py
│   └── tracking_receiver.py
├── ios_hmd/                    ← Xcode project (Swift)
│   ├── VideoDecoder.swift
│   ├── USBListener.swift
│   ├── GyroSender.swift
│   └── MetalRenderer.swift
├── dream.txt
├── research_report.md
└── plan.md
```

---

## Immediate Next Steps
1. Install iTunes + `pymobiledevice3`
2. Create free Apple Developer account
3. Xcode: build iOS app with TCP listener + label display
4. Python: connect over USB and change the label — Phase 0 done

---

## Open Questions / Decisions Pending
- Multiplexing strategy: single USB channel for both video and gyro data, or two separate tunnels?
- Stereo rendering: render twice in Godot (two cameras) or post-process a single frame into side-by-side?
- ATW shader: write from scratch in Metal or adapt from ALVR's open-source implementation?
- Controller input: gamepad only, or also design for one-handed play (phone in headset, one hand on keyboard)?

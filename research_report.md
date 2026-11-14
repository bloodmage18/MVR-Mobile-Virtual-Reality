# Research & Information: Custom Cross Reality HMD (PC to iOS)

This research summarizes the technical paths for building a custom, low-latency Cross Reality (VR/AR) HMD using an iPhone 12 connected to a PC via a USB cable, bypassing heavy middleware like SteamVR.

## 1. Technical Architecture Overview

To achieve "no lag" and low heat, you need to bypass traditional "Remote Desktop" logic and use a **Split Rendering** pipeline.

### The Problem with iOS (The "80lvl Difficulty")
Unlike Android (which uses `adb reverse` for easy USB port forwarding), iOS requires **USBMuxd** (USB Multiplexing Daemon) to establish a TCP/IP-like connection over the Lightning/USB-C cable.

### Proposed Pipeline
1.  **PC (Host):** Renders the 3D scene in Godot.
2.  **Encoder (PC):** Grabs the Godot Viewport and encodes it to H.264/HEVC using the GPU (NVENC/AMF).
3.  **Bridge (Python/C++):** Tunnels the encoded data over the USB cable using a library like `usbmuxd`.
4.  **HMD (iPhone):** Decodes the stream using hardware acceleration (`VideoToolbox`) and renders it to the screen.
5.  **Tracking:** iPhone sends Gyroscope data back over the same USB tunnel to update the Godot camera.

---

## 2. Low-Latency Research Papers & Concepts

### A. Motion-to-Photon Latency
*   **Goal:** < 20ms for a comfortable experience.
*   **Mechanism:** To hit this, you cannot wait for a frame to go "Godot -> Encoder -> Cable -> iOS -> Display".
*   **Solution:** **Asynchronous Timewarp (ATW)**. The iPhone should render the received frame on a 3D quad and use its *own* latest gyro data to "warp" the image to match the current head rotation, even if the new frame hasn't arrived from the PC yet.

### B. Suggested Papers for Study
1.  **"Latency analysis of smartphone-based virtual reality systems"**: Discusses the impact of sensor pooling and frame buffers on VR immersion.
2.  **"Split Rendering for Mobile VR" (Microsoft/Meta Research)**: Focuses on how to split the load between a powerful PC and a mobile device.
3.  **"Optimization of real-time video streaming for head-mounted displays"**: Covers foveated encoding and transport protocol choices (UDP vs. TCP).

---

## 3. Communication over USB (The iOS Bridge)

To talk to the iPhone over the cable on Windows:
*   **USBMuxd (iTunes Drivers):** You need the Apple Mobile Device Support installed (comes with iTunes).
*   **Libraries:** 
    *   **Python:** Look into `pymobiledevice3` or `libimobiledevice` wrappers.
    *   **Swift/Objective-C (iOS side):** Use **PeerTalk** (or native BSD sockets listening on the port mapped by USBMuxd).
*   **Method:** You map a local PC port (e.g., 12345) to a port on the iOS device. The iPhone listens for a connection, and your Godot app (or a Python helper) sends data to `localhost:12345`.

---

## 4. Godot Implementation Strategy

### Screen Capture in Godot
Don't use `Viewport.get_texture().get_image()`. It is slow (runs on CPU).
*   **Godot GDExtension:** Use a C++ extension to access the GPU texture directly (Vulkan/DirectX) and pass it to an encoder (FFmpeg/NVENC).
*   **NVIDIA Video SDK:** Use this for ultra-fast encoding if you have an NVIDIA GPU.

### Gyroscope Integration
iPhone's `CMDeviceMotion` provides high-frequency orientation data.
*   **Smoothing:** Use a **Kalman Filter** or simple **Lerp** to smooth the tracking data before it reaches Godot.
*   **Prediction:** Use the angular velocity from the gyro to predict where the head will be in 10-20ms to compensate for cable latency.

---

## 5. Summary Checklist for Planning

- [ ] **Establish USB Tunnel:** Can you successfully ping a TCP server on the iPhone from the PC over the cable? (Requires `libimobiledevice` or `usbmuxd`).
- [ ] **Hardware Decoding:** Build a simple iOS app that can decode a 60fps H.264 stream without jitter.
- [ ] **Godot Frame Grab:** Research "GDExtension texture sharing" to send Godot frames to an encoder without copying to System RAM.
- [ ] **Tracking Loop:** Send iPhone gyro data to Godot and see the camera move with minimal delay.

---
**References for deeper dive:**
*   **ALVR Source Code:** Check their `alvr_client_core` for how they handle motion prediction.
*   **Moonlight/Sunshine:** Look at how they handle low-latency game streaming over networks.
*   **CloudXR:** NVIDIA's SDK for split rendering.

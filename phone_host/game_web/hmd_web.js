// Phone sensors -> Godot web build (the phone runs the game itself).
// Loaded in the <head> of the exported page (export preset html/head_include).
//  - caps the render resolution (iPhone devicePixelRatio 3 would render ~2500 px wide)
//  - shows TAP TO START: iOS motion permission, fullscreen + landscape (Android), wake lock,
//    and unlocks game audio (browsers only allow sound after a tap)
//  - head tracking, same maths as the phone test page (phone_host/web/index.html):
//      gyro (deviceorientation, iPhone) or tilt from gravity (accelerometer only, Galaxy A08)
//  - publishes window.hmdHeadStr = "seq,recenterSeq,r00,r01,r02,r10,r11,r12,r20,r21,r22"
//    (camera -> world rotation rows, Godot axes: Y up, -Z forward); hmd_head.gd polls it.
//  - double-tap anywhere = recenter
"use strict";
(function () {
  const PARAMS = new URLSearchParams(location.search);
  const deg = Math.PI / 180;

  // ── Resolution cap ────────────────────────────────────────────────────────
  const MAX_DPR = parseFloat(PARAMS.get("dpr") || "1.5");
  const realDpr = window.devicePixelRatio || 1;
  try { Object.defineProperty(window, "devicePixelRatio", {get: () => Math.min(realDpr, MAX_DPR), configurable: true}); } catch (_) {}

  // ── Remember audio contexts so the tap can unlock them ────────────────────
  const audioCtxs = [];
  let begun = false;
  function resumeOnNextTouch(c) {
    const go = () => { try { c.resume(); } catch (_) {} removeEventListener("touchend", go, true); removeEventListener("click", go, true); };
    addEventListener("touchend", go, true); addEventListener("click", go, true);
  }
  for (const name of (PARAMS.has("nowrap") ? [] : ["AudioContext", "webkitAudioContext"])) {
    const Orig = window[name];
    if (!Orig) continue;
    // A Proxy keeps the real constructor intact (statics, prototype, instanceof); a plain wrapper
    // function broke Godot's AudioWorklet setup ("Failed to create PositionWorklet").
    window[name] = new Proxy(Orig, {
      construct(target, args, newTarget) {
        const c = Reflect.construct(target, args, newTarget === window[name] ? target : newTarget);
        audioCtxs.push(c);
        if (begun) { try { c.resume(); } catch (_) {} resumeOnNextTouch(c); }   // iOS needs a gesture
        return c;
      },
    });
  }

  // ── Head pose output ──────────────────────────────────────────────────────
  let seq = 0, recenterSeq = 0;
  window.hmdSource = "none";
  function publish(H) {
    seq++;
    const f = (v) => v.toFixed(5);
    window.hmdHeadStr = `${seq},${recenterSeq},` + [H[0], H[1], H[2]].map(r => r.map(f).join(",")).join(",");
  }

  // Quaternion helpers [x, y, z, w]
  function qMul(a, b) {
    return [a[3]*b[0] + a[0]*b[3] + a[1]*b[2] - a[2]*b[1],
            a[3]*b[1] - a[0]*b[2] + a[1]*b[3] + a[2]*b[0],
            a[3]*b[2] + a[0]*b[1] - a[1]*b[0] + a[2]*b[3],
            a[3]*b[3] - a[0]*b[0] - a[1]*b[1] - a[2]*b[2]];
  }
  function qToRows(q) {
    const [x, y, z, w] = q;
    return [[1 - 2*(y*y + z*z), 2*(x*y - z*w), 2*(x*z + y*w)],
            [2*(x*y + z*w), 1 - 2*(x*x + z*z), 2*(y*z - x*w)],
            [2*(x*z - y*w), 2*(y*z + x*w), 1 - 2*(x*x + y*y)]];
  }
  // Sensor world is Z-up (east, north, up); Godot is Y-up with -Z north.
  const enuToGl = (M) => [M[0], M[2], M[1].map(v => -v)];
  function screenAngle() {
    const a = (screen.orientation && typeof screen.orientation.angle === "number") ? screen.orientation.angle : window.orientation;
    return a || 0;
  }
  function eulerToScreenQuat(alpha, beta, gamma) {
    const x = (beta || 0) * deg / 2, y = (gamma || 0) * deg / 2, z = (alpha || 0) * deg / 2;
    const cX = Math.cos(x), cY = Math.cos(y), cZ = Math.cos(z), sX = Math.sin(x), sY = Math.sin(y), sZ = Math.sin(z);
    const q = [sX*cY*cZ - cX*sY*sZ, cX*sY*cZ + sX*cY*sZ, cX*cY*sZ + sX*sY*cZ, cX*cY*cZ - sX*sY*sZ];
    const a = -screenAngle() * deg / 2;
    return qMul(q, [0, 0, Math.sin(a), Math.cos(a)]);
  }

  // (a) gyro: deviceorientation. Chrome on a phone WITHOUT a gyro (A08) still fires it with a
  // constant alpha, so only trust it on iOS, when alpha moves, or when a rotationRate shows up.
  const IS_IOS = /iPhone|iPad|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  let lastOrient = 0, alphaFirst = null, alphaMoved = false, rotRateSeen = false;
  function onOrientation(e) {
    if (e.alpha == null || e.beta == null) return;
    lastOrient = performance.now();
    if (alphaFirst === null) alphaFirst = e.alpha; else if (Math.abs(e.alpha - alphaFirst) > 1e-3) alphaMoved = true;
    // iOS always has a gyro: if tilt was chosen too early (slow permission answer), switch now
    if (window.hmdSource !== "gyro" && IS_IOS && window.hmdSource !== "waiting") { window.hmdSource = "gyro"; recenterSeq++; }
    if (window.hmdSource !== "gyro") return;
    publish(enuToGl(qToRows(eulerToScreenQuat(e.alpha, e.beta, e.gamma))));
  }
  function onMotionProbe(e) {
    const r = e.rotationRate;
    if (r && r.alpha != null && r.beta != null) { rotRateSeen = true; removeEventListener("devicemotion", onMotionProbe); }
  }

  // (b) tilt from gravity: pitch + roll only (yaw from the controller / drag)
  const FLIP_PARAM = PARAMS.get("flip");
  let flipXY = false, upsideDownN = 0, grav = null;
  function gravityReading(x, y, z) {
    if (window.hmdSource === "gyro") return;
    if (flipXY) { x = -x; y = -y; }
    if (FLIP_PARAM === null) {
      const n = Math.hypot(x, y, z);
      upsideDownN = (n > 5 && y / n < -0.6) ? upsideDownN + 1 : 0;
      if (upsideDownN > 30) { flipXY = !flipXY; upsideDownN = 0; grav = null; return; }   // heads are never upside down
    }
    if (!grav) grav = [x, y, z];
    else for (let i = 0; i < 3; i++) grav[i] += 0.25 * ([x, y, z][i] - grav[i]);
    const n = Math.hypot(grav[0], grav[1], grav[2]); if (n < 1) return;
    const u = grav.map(v => v / n), fu = -u[2];
    let h = [-fu * u[0], -fu * u[1], -1 - fu * u[2]];
    if (Math.hypot(...h) < 0.15) { const s = fu > 0 ? -1 : 1, yu = u[1]; h = [s * -yu * u[0], s * (1 - yu * u[1]), s * -yu * u[2]]; }
    const hn = Math.hypot(...h), Y = h.map(v => v / hn), Z = u;
    const X = [Y[1]*Z[2] - Y[2]*Z[1], Y[2]*Z[0] - Y[0]*Z[2], Y[0]*Z[1] - Y[1]*Z[0]];
    publish(enuToGl([X, Y, Z]));
  }
  let motionStarted = false;
  function startDeviceMotion() {
    if (motionStarted) return; motionStarted = true;
    flipXY = FLIP_PARAM === "1";
    const iosSign = IS_IOS ? -1 : 1;
    window.hmdSource = "tilt (devicemotion)";
    addEventListener("devicemotion", (e) => {
      const g = e.accelerationIncludingGravity;
      if (!g || g.x == null) return;
      const a = screenAngle() * deg, c = Math.cos(a), s = Math.sin(a);
      const dx = g.x * iosSign, dy = g.y * iosSign, dz = g.z * iosSign;
      gravityReading(dx * c - dy * s, dx * s + dy * c, dz);
    });
  }
  function startTilt() {
    if ("Accelerometer" in window) {
      try {
        const s = new Accelerometer({frequency: 60, referenceFrame: "screen"});
        let got = false;
        flipXY = FLIP_PARAM !== null ? FLIP_PARAM === "1" : true;   // the A08 fit proven earlier
        s.onreading = () => { got = true; gravityReading(s.x, s.y, s.z); };
        s.onerror = () => { if (!got) startDeviceMotion(); };
        s.start();
        window.hmdSource = "tilt (Accelerometer)";
        setTimeout(() => { if (!got) startDeviceMotion(); }, 1200);
        return;
      } catch (_) {}
    }
    startDeviceMotion();
  }
  function startSensors() {
    addEventListener("deviceorientation", onOrientation);
    addEventListener("devicemotion", onMotionProbe);
    const want = PARAMS.get("sensor");
    if (want === "tilt") { startTilt(); return; }
    if (want === "gyro") { window.hmdSource = "gyro"; return; }
    window.hmdSource = "waiting";
    setTimeout(() => {
      if (lastOrient > 0 && (IS_IOS || alphaMoved || rotRateSeen)) window.hmdSource = "gyro";
      else startTilt();
      recenterSeq++;
    }, 1200);
  }

  // ── Tap to start ─────────────────────────────────────────────────────────
  function begin() {
    // iOS: permission prompts must be requested inside the tap, before any await. The sensor
    // choice waits for the answer (no motion events arrive while the prompt is open).
    let permission = null;
    if (window.DeviceOrientationEvent && typeof DeviceOrientationEvent.requestPermission === "function")
      permission = DeviceOrientationEvent.requestPermission().catch(e => "error: " + e.message);
    if (window.DeviceMotionEvent && typeof DeviceMotionEvent.requestPermission === "function")
      DeviceMotionEvent.requestPermission().catch(() => {});
    const el = document.documentElement;
    const fs = el.requestFullscreen || el.webkitRequestFullscreen;
    if (fs) {
      try {
        const p = fs.call(el, {navigationUI: "hide"});
        if (p && p.then) p.then(() => screen.orientation && screen.orientation.lock && screen.orientation.lock("landscape").catch(() => {})).catch(() => {});
      } catch (_) {}
    }
    for (const c of audioCtxs) { try { c.resume(); } catch (_) {} }
    try { navigator.wakeLock && navigator.wakeLock.request("screen").catch(() => {}); } catch (_) {}
    begun = true;
    overlay.remove();
    if (permission) permission.then(r => { window.hmdPermission = r; startSensors(); });
    else startSensors();
  }

  let overlay;
  function makeOverlay() {
    overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;background:#05070d;color:#dde;" +
      "display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;font:18px system-ui,sans-serif;text-align:center;padding:16px";
    overlay.innerHTML = '<div style="font-size:28px;font-weight:700">The School (phone)</div>' +
      '<button id="hmdGo" style="font:800 38px system-ui,sans-serif;padding:24px 50px;border:0;border-radius:18px;background:#b3261e;color:#fff">TAP TO START</button>' +
      '<div style="max-width:640px;color:#99a;line-height:1.4">Landscape, then into the headset. Allow Motion &amp; Orientation if asked.<br>' +
      'Controller paired to the phone: left stick walks, right stick turns, A uses, Y flashlight.<br>' +
      'No controller: drag the screen to turn, tap to use. Double-tap = recenter.<br>' +
      'Lens fit is taken from the test page (its Lens button).</div>';
    document.body.appendChild(overlay);
    overlay.querySelector("#hmdGo").addEventListener("click", begin);
  }
  if (PARAMS.has("autostart")) {        // test hook: no tap screen, no sensors
    window.hmdSource = "autostart test";
  } else if (document.body) makeOverlay(); else addEventListener("DOMContentLoaded", makeOverlay);

  // ── Laptop controller relay: the controller stays paired to the laptop; the server
  //    reads it and streams it here (phone_host/pad_relay.py). Godot polls window.hmdPadStr.
  window.hmdPadStr = "";
  let padFails = 0;
  function connectPad() {
    let ws;
    try { ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/pad`); }
    catch (_) { setTimeout(connectPad, 3000); return; }
    let lastMsg = performance.now();
    const watchdog = setInterval(() => {      // half-open socket (Wi-Fi lost): release the pad
      if (performance.now() - lastMsg > 1500) { window.hmdPadStr = "pad,none"; try { ws.close(); } catch (_) {} }
    }, 500);
    ws.onopen = () => { padFails = 0; lastMsg = performance.now(); };
    ws.onmessage = (ev) => { lastMsg = performance.now(); window.hmdPadStr = ev.data; };
    ws.onclose = () => { clearInterval(watchdog); window.hmdPadStr = "pad,none"; padFails++; setTimeout(connectPad, Math.min(10000, 1000 * padFails)); };
  }
  connectPad();

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && begun) { try { navigator.wakeLock && navigator.wakeLock.request("screen").catch(() => {}); } catch (_) {} }
  });

  // Double-tap anywhere = recenter
  let lastTap = 0;
  addEventListener("touchend", (e) => {
    if (e.touches.length !== 0 || e.changedTouches.length !== 1) return;   // ignore multi-finger lifts
    const now = performance.now();
    if (now - lastTap > 40 && now - lastTap < 300) { recenterSeq++; lastTap = 0; } else lastTap = now;
  }, {passive: true});
})();

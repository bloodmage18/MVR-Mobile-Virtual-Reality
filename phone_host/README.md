# Phone-hosted VR test

The **phone runs the scene itself** (WebGL in the browser). The laptop does not render or
stream anything. It only hands the page to the phone over Wi-Fi, once.

The scene is the old test room: a floor grid, 4 tall pillars about 6 m away with big letters
(**N** red, **E** green, **S** blue, **W** yellow), and a few small cubes for depth. You stand at 1.6 m eye height.

## 1. Start the server (laptop)

Double-click **`phone_host_test.bat`** in the repo root. It prints the address to open, for example:

```
##    https://192.168.x.y:8443/    ##
```

Leave the window open. It prints a line every few seconds for each connected phone: which
sensor it uses, fps, mono or stereo, controller and screen orientation.

## 2. Open it on the phone (same Wi-Fi as the laptop)

Type **`https://192.168.x.y:8443/`** into the phone's browser. Use the address the window
prints if it is different.

- **iPhone 12:** use Safari.
- **Galaxy A08:** use Chrome.

If you type `http://...:8080` by mistake, a page tells you the right https address.

### The one-time certificate warning

The laptop makes its own (self-signed) certificate, so the phone warns the first time:

- **iPhone Safari:** "This Connection Is Not Private" > **Show Details** > **visit this website** > **Visit Website**.
- **Android Chrome:** "Your connection is not private" > **Advanced** > **Proceed to 192.168.x.y (unsafe)**.

This is safe here, because it is your own laptop on your own Wi-Fi.

### Then

1. Turn the phone sideways (landscape).
2. Tap **TAP TO START**.
3. On the iPhone, tap **Allow** for "Motion & Orientation Access".
4. Put the phone in the headset.

## 3. Windows Firewall (if the phone can't load the page at all)

Checked on 2026-11-15: the laptop was on **HUAWEI-5G-qxB5**, profile **Private**, and Windows Firewall
already had *Allow inbound* rules for `C:\Python314\python.exe` (the interpreter the `.venv` runs) on
both Private and Public networks. So the firewall should **not** block the phones. If that changes:

- Make sure the phone is on the **same** Wi-Fi as the laptop. A 2.4 GHz and a 5 GHz network on the same
  router are usually the same LAN, but "guest" networks are not.
- Settings > Network & internet > Wi-Fi > (your network) > Network profile type: **Private**.
- Or allow Python in Windows Security > Firewall > "Allow an app through firewall" (tick Private and Public
  for `C:\Python314\python.exe`; the `.venv` python just starts that one).

Nothing here changes your firewall settings. You choose.

## Controls

| Action | How |
|---|---|
| Look around | Move your head (sensors) |
| Turn | Drag a finger left/right on the screen, or the controller's **right stick** |
| Walk | Controller **left stick** (pair the Bluetooth controller to the *phone*) |
| Recenter (face North) | **Double-tap** the screen, the **Recenter** button, or controller **Y / Triangle** |
| Mono / Stereo | Small button bottom-right, or controller **Start / Options**. Stereo is the default. |
| Show stats | Single tap (fps, sensor source, laptop link; fades out by itself) |

## What each phone should do

- **iPhone 12 (gyro):** full look-around. The sensor reads `gyro (deviceorientation)`. Yaw starts
  facing North (the red **N**) when you start. It can drift slowly over minutes, so double-tap to recenter.
- **Galaxy A08 (accelerometer only, no gyro):** **tilt only**. Looking up/down and tilting your head
  side to side work (`tilt (Accelerometer)`). Turning left/right with your head **cannot** work without a
  gyro, so turn by dragging the screen or with the controller's right stick. If the view starts
  upside down, hold still for half a second and it flips itself (or add `?flip=0` / `?flip=1` to the URL).
  Chrome on a phone without a gyro still sends orientation events, with a fixed alpha of 0. The page
  spots that and uses the accelerometer instead (the laptop log then shows
  `[orientation events without a gyro (alpha fixed)]`). If the A08 ever shows `gyro (deviceorientation)`,
  add `?sensor=tilt` to the URL.

## Why HTTPS?

Browsers only give motion sensors (and fullscreen, wake lock and the controller API in some
browsers) to **secure** pages. A plain `http://192.168...` page gets no head tracking at all.

## Known limits

- **No real fullscreen in iPhone Safari.** Apple doesn't allow it for web pages. You can try
  **Share > Add to Home Screen** and opening the new "Phone VR" icon, which runs without the Safari bars.
  **Untested:** the home-screen app may refuse the self-signed certificate even after Safari accepted it,
  and then it won't load. If so, use it in Safari: the bars take some of the screen, so the two eye
  views won't line up exactly with the lenses.
  (Android Chrome goes fullscreen and locks landscape when you tap Start.)
- **Screen turning off on iPhone:** Safari on iOS 16.4 or later keeps the screen on. On older iOS, set
  Settings > Display & Brightness > Auto-Lock to **Never** while testing (the page says so if needed).
- **Certificate warning** every time the certificate changes (for example if the laptop's IP changes; the
  server then makes a new certificate by itself).
- **No offline mode.** Offline use needs a service worker, and browsers refuse service workers on a
  self-signed certificate. Doing that later needs a properly trusted certificate (for example a local CA
  installed on the phone, or a real domain).
- iPhone Safari may refuse the status WebSocket on a self-signed certificate. The page then reports over
  plain HTTPS instead (the server window shows `link=https-post`). The scene itself never needs the laptop.
- No lens-distortion correction yet. This is a plain side-by-side test.

## Test switches (add to the URL)

- `?mode=mono`: start in mono.
- `?fov=90`: change the per-eye vertical field of view (default 80 in stereo, 70 in mono).
- `?ipd=0.065`: change the eye distance in metres.
- `?sensor=tilt`: force tilt mode.
- `?sensor=compass`: force compass mode.
- `?sensor=gyro`: force gyro (deviceorientation) mode.
- `?flip=0` / `?flip=1`: tilt-mode flip off or on.
- `?autostart=1`: skip the start screen and use no sensors. This is for automated screenshots.

Server options: `phone_host_test.bat --ip 192.168.x.y` (override IP detection), `--regen-cert`, `--port 8443`.

## Lens fit: when you see two images

If you see two of each pillar, the two eye images are not sitting under the headset lenses. This happens on screens wider than the lens spacing, such as the Galaxy A08. The iPhone 12 fits as it is.

The phone remembers the setting. Tune it in either of these ways:

- **Headset on, keys on the laptop:** in the server window, press `[` and `]` to bring the images closer together or move them apart, `-` and `=` to change the field of view, and `0` to reset. Yellow crosses appear for a few seconds. Make the two crosses merge into one.
- **Before putting the headset on:** tap the **Lens** button in the bottom-right corner of the page and use the sliders.
- **Controller paired to the phone:** the d-pad's left and right change the separation; up and down change the field of view.

Starting values: separation 0.08 on Android (the value tuned for the A08 in the laptop pipeline) and 0 on iPhone. Use the headset's own lens knobs for fine focus.

## The school horror game on the phone (`/game/`)

The phone runs the game itself (a Godot web build). The laptop only serves the files and relays the controller.

1. Build it once, and again after changing the game: run `phone_host\build_game.bat`.
2. Run `phone_host_test.bat`, then open **`https://<laptop-ip>:8443/game/`** on the phone. The test page also has a "Play the school" button.
3. Tap **TAP TO START**. On iPhone, allow Motion & Orientation.

Controls:
- **Head:**
  - The iPhone gyroscope gives full look-around, and walking goes where you look.
  - The A08 gives tilt only; turn with the controller or by dragging.
- **Controller:**
  - The Xbox controller connected to the **laptop** works as it is: the server reads it and streams it to the game on the phone. The server window prints `[pad] laptop controller found`.
  - A controller paired to the phone over Bluetooth also works.
- **No controller:** drag the screen to turn, tap to use.
- **Recenter:** double-tap the screen, or press the controller's Back button.
- **Eye separation:** comes from the test page's Lens settings on the same phone.

## Trust this laptop (no more warnings, full screen from the iPhone home screen)

The server makes a small **local certificate authority** that can only vouch for home-network addresses (192.168.x.x, 10.x, 172.16-31.x and `.local` names). It cannot vouch for real websites. Install it on a phone once:

- **iPhone:**
  1. In Safari, open `http://<laptop-ip>:8080/ca.crt`, then tap Allow. "Profile Downloaded" appears.
  2. Go to Settings → General → VPN & Device Management → *Cross Reality home CA* → Install.
  3. Go to Settings → General → About → Certificate Trust Settings and switch on *Cross Reality home CA*.
- **Android:** open `http://<laptop-ip>:8080/ca.crt`, then go to Settings → Security → More → Install from device storage → CA certificate.

After that, `https://<laptop-ip>:8443/` (test and game) and the home space over Wi-Fi open with no warnings. For the home space, use the `https://<laptop-ip>:8444/?k=...` address that `start_home_wifi.bat` prints. The `?k=` key is required, so nobody else on the network can watch the stream or send fake head movements. The key stays the same between runs, so a home-screen icon keeps working. "Share → Add to Home Screen" then opens full screen.

The private key, `phone_host/cert/ca_key.pem`, stays on this laptop. It is git-ignored; never share it. To withdraw trust, remove the profile on the phone.

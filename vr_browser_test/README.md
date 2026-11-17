# VR Browser test

Put the phone in the headset and you float in dark space with a browser window in front of you.
Browse with the Xbox controller. When a video starts, the browser fades away and the video plays on
the phone: normal videos on a big curved cinema screen, 360 and VR180 videos all around you. B takes you back.

The browser is Brave, running hidden on the laptop with its own profile, so you sign in once and stay signed in.
It is a real Brave window parked off the edge of the screen (not on the taskbar): you may see it flash for a
moment when the server starts. Don't minimise or close it from the Task Manager while you browse.

## 1. Sign in once

1. Double-click `vr_browser_test\login.bat`.
2. A normal Brave window opens on YouTube. Sign in (and to any other site you want).
3. Close that Brave window. You stay signed in from now on.

Do this again only if a site logs you out. The server and the sign-in window can't run at the same time.

## 2. Start

1. Close `phone_host_test.bat` if it is running (both use port 8443).
2. Double-click `vr_browser_test.bat` in the project folder.
3. It prints an address like `https://192.168.x.y:8443/?k=...`. Open exactly that address on the phone
   (same Wi-Fi). The `?k=...` part is the access key: without it the page is refused. Don't share it.
4. Tap the start screen, put the phone in the headset. Plug the Xbox controller into the laptop
   (or connect one to the phone).

Everything works with the controller alone. A small window "VR Browser keyboard" also opens on the
laptop if you prefer typing there: a box for the field you clicked, a box for an address or search,
and buttons Back, Home (YouTube), New window, Play in VR. They act on the window you are looking at.

## Controls

| Button | Browsing | Watching a video (VR) | Keyboard open |
|---|---|---|---|
| A | click (page, tabs, address bar) | button under the ring / highlighted, else play/pause | press key (ring on the page or tabs: click there) |
| B | back (cancels a video that is getting ready) | back to the browser | close keyboard |
| X | play this page's video in VR | show / hide the player bar | backspace |
| Y | reload | next projection (fixes split / bent videos) | space |
| LB / RB | back (hold LB: new Tor window) / forward | move the player-bar highlight | shift / symbols |
| LT / RT | previous / next window | | caret left / right (close the keyboard to switch windows) |
| L3 (press left stick) | keyboard on / off | keyboard on / off | close keyboard |
| R3 (press right stick) | new window (hold: close window) | | nothing (close the keyboard first) |
| Left stick | scroll | screen size (flat videos) | move key highlight |
| D-pad up/down | scroll | | move key highlight |
| D-pad left/right | arrow keys | −10 s / +10 s | move key highlight |
| Right stick | nudge the pointer | turn the view | nudge the pointer |
| Start | re-centre the view | re-centre the view | enter / go |
| View (Back) | lens settings (d-pad tunes, A/B/View close) | lens settings | lens settings |

You point by looking: a small ring shows where you point. Touch: tap = click / play-pause, double tap = re-centre, drag = turn.
The top of the browser shows which controller is active ("Pad: laptop", "Pad: phone ..." or "No controller").

## Keyboard and windows

- Click a text field (search box, login form) and the keyboard opens by itself; L3 opens or closes it anywhere.
- With no text field selected, the keyboard types an address or search words; Start (or the Go key) goes there.
- R3 opens a new window with the address bar and keyboard ready. LT / RT switch windows, hold R3 to close one.
  Links that open in a new tab show up as a new window too. Up to 6 windows.
- Above the page: the windows (A on a tab switches, × closes, + opens one) and the address bar
  (◀ ▶ ⟳, the address, ⌨). Pop-ups that open by themselves are closed.

## Private window with Tor

Brave's own **"New private window with Tor"** (the same as Alt+Shift+N in Brave) works from the headset:

- Open one with the purple **Tor** button next to **+** in the window strip, or **hold LB** (about 1 s; a short
  LB press is still "back"). The server presses Alt+Shift+N in the hidden Brave, so the laptop's focus jumps
  to Brave for a split second.
- Tor windows have a purple **Tor** label in the strip and the address bar. The first one says
  **"Connecting to Tor…"** until Tor is up (10-30 s, sometimes longer); then type an address as usual.
- They count towards the 6 windows. Close them like any window (× or hold R3). Brave forgets everything
  from them when the last Tor window closes.
- **Limits:** it is slower than normal browsing (all traffic goes through Tor), Brave's Tor protections
  apply (some sites block Tor or show captchas), you are not signed in to anything there.
- **Videos in a Tor window are not streamed to the phone** (that would fetch them from the laptop's own
  connection and show your real IP). X shows the browser picture on the cinema screen instead (sound on the
  phone, see Sound); it can be choppy.
- No cookies of a Tor window are ever used by the server, and Tor-window addresses are not written to the log.
  The server's page helpers (video and text-field detection) run hidden from the sites in a Tor window, so a
  site cannot tell this setup apart from any other Brave Tor window. If Brave restarts by itself, the page
  of a Tor window is not reopened (the restarted window is the normal, signed-in one).
- On the cinema screen a video fills the page instead of going truly full screen (true full screen would put
  the hidden Brave window on the laptop's monitor). If a site goes full screen by itself (YouTube's F key),
  the window is pushed straight back off the monitor; you may see a very short flash on the laptop.
- Tor windows need the normal mode: they are not available when the server runs with `--headless`
  (the old hidden mode without a real window, kept as a fallback: `python vr_browser_test\server.py --headless`).

## Videos

- Videos are **streamed**: they start after a moment (1-2 s on a YouTube page that has been open for a few
  seconds, about 8 s otherwise) and you can jump anywhere. Nothing is downloaded.
- The phone gets the best quality it can play smoothly (up to 4K/8K for 360 and VR180 if the phone can decode it).
- 3D videos (side by side, top/bottom, VR180, 3D 360) are detected; if one still looks split, press Y.
  Your choice is remembered for that video.
- Sites the phone can't stream: the laptop downloads the video first (with a progress bar), or, for
  protected video, the phone shows the live browser picture on the cinema screen, with the site's own player controls.
- When you go back with B, the browser video jumps to where you stopped watching.

## Sound

- **All browser sound plays on the phone** (earbuds): pages, sites that play on the cinema screen, Tor windows.
  Only Brave's own sound is captured on the laptop (Windows 10 2004+ "process loopback"), never other apps or
  the microphone, and it never touches the network, so it is safe for Tor windows.
- While a phone is connected the **laptop speakers are muted**; they are unmuted when the phone disconnects or
  the server stops (also after a crash: the next start unmutes them).
- The helper `audio_capture.exe` is built automatically on first use from `audio_capture.cs` with the C#
  compiler that comes with Windows (into `cache\`).
- `--laptop-sound` keeps the sound on the laptop (the old way); `?audio=0` in the phone address turns it off
  for that phone.
- Delay is about 0.1-0.2 s, close to the browser picture's own delay.

## Performance (low-end phones)

- **Video quality memory:** when a phone can't keep up with a video quality (e.g. the A08 with 4K), it
  remembers that for 14 days and starts the next videos one step lower (1440p) instead of stuttering first.

- **Auto resolution:** when the phone can't keep up (it gets warm, or a heavy video plays), it draws fewer
  pixels (scale 1 -> 0.85 -> 0.72 -> 0.6) instead of stuttering, and goes back up after a long smooth spell.
  If fewer pixels don't help (battery saver's 60 Hz cap, iPhone Low Power Mode's 30 fps), it goes back to full
  sharpness and stays there. `?scale=0.8` in the address
  fixes the scale.
- **Background windows pause:** 15 s after you leave a window it is paused on the laptop (its scripts and
  animations stop); it wakes up when you switch back. `--no-freeze` keeps them running.
- **Measurements:** the phone sends a sample every 5 s to `vr_browser_test\perf.log` (fps, the screen's refresh
  rate, render scale, frame times, video frames dropped, battery, laptop CPU, Brave memory, windows open/paused),
  and the server window prints a `[perf]` line every minute. The debug HUD shows "fps of Hz" and the scale.

## Troubleshooting

- **"Port 8443 is already in use"**: `phone_host_test.bat` (or another copy of this server) is still open. Close it.
- **"the sign-in Brave window is still open"**: close the Brave window that `login.bat` opened.
- **Phone says the page is not safe / certificate warning**: install the certificate once from
  `https://<laptop-ip>:8443/ca.crt` (same as for the phone_host test).
- **403 / "access key"**: open the full address including `?k=...`.
- **Not signed in on YouTube**: stop the server, run `login.bat`, sign in, close Brave, start again.
- **Tor window says "Brave did not open a Tor window"**: Tor may be switched off in Brave (Settings → Privacy →
  Private window with Tor); turn it on with `login.bat`. If it keeps "Connecting to Tor…" for minutes, close it
  and open a new one (some networks block Tor).
- **A Brave window appears on the laptop screen**: the server moves it back by itself; if one stays, restart the server.
- **"No controller"**: orange = the laptop sees no controller (plug it in, or pair it with the phone);
  red = the phone lost the connection to the laptop. "Pad: phone" means the phone's own Bluetooth pad is used.
- **Video stutters (e.g. on the Galaxy A08)**: when the phone drops frames for about 10 s it switches to a lower
  quality by itself. Phones never get more than 4K-wide video (8K would run out of memory).
- **360 / 3D video looks bent or split**: press Y until it looks right (it is remembered).
- **The picture is doubled or blurry**: press View and adjust the lens settings with the d-pad.
- **Video has no sound on the phone**: tap the phone screen once (iPhones need a tap before sound).
- Old downloaded videos in `vr_browser_test\cache\*.mp4` are no longer needed: delete them to free space
  (the small `.json` files remember your 3D/360 choices).
- Your logins live in `vr_browser_test\profile\` (only your Windows account can open it). Delete the folder to sign out everywhere.

# VR Browser test

Put the phone in the headset and you float in dark space with a browser window in front of you.
Browse with the Xbox controller. When a video starts, the browser fades away and the video plays on
the phone: normal videos on a big curved cinema screen, 360 and VR180 videos all around you. B takes you back.

The browser is Brave, running hidden on the laptop with its own profile, so you sign in once and stay signed in.

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
| LB / RB | back / forward | move the player-bar highlight | shift / symbols |
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

## Videos

- Videos are **streamed**: they start after a moment (1-2 s on a YouTube page that has been open for a few
  seconds, about 8 s otherwise) and you can jump anywhere. Nothing is downloaded.
- The phone gets the best quality it can play smoothly (up to 4K/8K for 360 and VR180 if the phone can decode it).
- 3D videos (side by side, top/bottom, VR180, 3D 360) are detected; if one still looks split, press Y.
  Your choice is remembered for that video.
- Sites the phone can't stream: the laptop downloads the video first (with a progress bar), or, for
  protected video, the phone shows the live browser picture on the cinema screen and the **sound plays on the laptop**.
- When you go back with B, the browser video jumps to where you stopped watching.

## Troubleshooting

- **"Port 8443 is already in use"**: `phone_host_test.bat` (or another copy of this server) is still open. Close it.
- **"the sign-in Brave window is still open"**: close the Brave window that `login.bat` opened.
- **Phone says the page is not safe / certificate warning**: install the certificate once from
  `https://<laptop-ip>:8443/ca.crt` (same as for the phone_host test).
- **403 / "access key"**: open the full address including `?k=...`.
- **Not signed in on YouTube**: stop the server, run `login.bat`, sign in, close Brave, start again.
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

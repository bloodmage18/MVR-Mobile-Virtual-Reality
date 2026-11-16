# Home Space: plan

A 3D home or library you walk around in with the phone HMD. You pick things up and use them:

- look at images,
- read comics,
- watch videos,
- use a web browser.

It is separate from the school game. It reuses the same phone link: tilt head tracking, mouse or controller to turn, video streamed to the phone.

Status: **Phase 1 built** (project shell, HMD rig, test bench, `start_home.bat`). The design of the space itself (layout, look, mood) is yours. This document covers the plumbing.

**Decisions (2026-11-14):**
- Headless means both: media sources are windowless from the start, and the home app goes windowless in Phase 5.
- You describe the space before `home.tscn` is built. Until then, stations are tested in `test_bench.tscn`.
- Order: images/comics, then video, then browser.
- Comic formats: image folders and PDF. Web comics come with the browser phase. CBZ/CBR are not needed for now.

**Update (2026-11-15):** You will design your own home scene later. For now the hub is a **3-floor library**, `library.tscn` (the main scene):
- Floor 1: browsers and other stuff (entrance, browser terminals, lounge, misc kiosk).
- Floor 2: books/comics reading room and gallery.
- Floor 3: cinema.

The shell and stairs are in `library.tscn`. Each floor is its own editable scene in `floors/`. Media stations are `Station_<Type>_<N>` nodes with a `Surface` child, so the media phases attach to them. Run `tools/stair_walk_test.gd` after editing a floor; it checks that the stairs still work. The wireless headset plan is in `../PLAN_WIRELESS.md`.

---

## 1. What "headless" means here

There are two separate things that can run "headless".

| Part | Today | Headless version |
|---|---|---|
| **Media sources** (browser, video decoder) | n/a | They run without any window on the laptop. Their frames go straight onto surfaces inside the 3D room. |
| **The home app itself** (Godot) | A visible window that `gfxcapture` grabs (this works even when the window is covered) | No visible window. Godot hands its rendered frames straight to the encoder. |

Godot's own `--headless` flag switches rendering **off** completely, so it can't be used for this. A headless home app has to keep rendering into a hidden or off-screen window and push the frames out itself.

The plan:

- Media sources are headless from day one.
- The home app starts with a visible window, because that capture path already works.
- The hidden-window version is its own phase (Phase 5).

## 2. Architecture

```
            ┌──────────── laptop ─────────────────────────────────────────┐
 phone ◄─USB─┤ hmd_bridge.py   (existing: pose in, video out, settings)   │
            │      ▲ frames (gfxcapture now, direct push in Phase 5)      │
            │      │                                                      │
            │  Godot "home" app ◄─── media_service.py (new, headless) ────┤
            │   • room scene        • library index + thumbnails          │
            │   • panels/screens    • video decode (ffmpeg → raw frames)  │
            │   • pointer/input     • headless Edge via CDP (screencast)  │
            │                       • comics: CBZ/folders/PDF → pages     │
            └─────────────────────────────────────────────────────────────┘
 earbuds ◄── laptop audio (video audio and browser audio play on the laptop)
```

`media_service.py` is a separate Python process (default port 8090), started by `start_home.bat` together with the bridge. Godot talks to it in two ways:

- **HTTP** for lists, thumbnails, comic pages and commands (play, pause, seek, navigate).
- **One local TCP socket per live surface** for frames: one for the video, one for the browser.

### Per media type

| Media | How it reaches the room | Notes |
|---|---|---|
| **Images** (png/jpg/webp) | Godot loads them directly (`Image.load_from_file`) | No service needed except for the folder listing |
| **Comics** | CBZ: Godot's built-in `ZIPReader`. Folder of images: direct. PDF: the service renders pages with PyMuPDF. CBR: the service unpacks it. | Page-by-page; optional panel zoom later |
| **Video** (mp4/mkv/…) | The service decodes with ffmpeg (QSV hardware decode where possible) and sends raw frames to a texture on a screen. Audio plays on the laptop through ffmpeg. | Godot only plays Ogg Theora natively. A fallback is the FFmpeg GDExtension (EIRTeam.FFmpeg), if a 4.7 build exists. |
| **Browser** | Edge (installed, v157) in `--headless=new` mode. The CDP `Page.startScreencast` command delivers JPEG frames to a texture on a panel. The pointer and keyboard are forwarded with `Input.dispatchMouseEvent` / `dispatchKeyEvent`. | No window on the laptop. About 15–30 fps, which is good for pages and reading and weak for watching video. Use the video player for video. |

## 3. Interacting in the headset

The phone gives tilt only, so yaw comes from the mouse or controller. That shapes these choices:

- **Pointer = centre-of-view reticle.** It uses the same raycast as the school doors. Look at a thing and press A, E or click.
- **"Pick up" = theatre mode.** The selected item flies in front of you and follows your body. Walking or pressing B puts it back.
- **Two mouse modes.** In *look* mode the mouse turns you, as it does now. In *pointer* mode (toggle with Tab or the right mouse button) the mouse moves a cursor on the focused panel, for browsing.
- **Controller:** bumpers turn comic pages, the stick scrolls, A selects, B goes back, Start opens a home menu.
- **Typing in the browser** comes from the laptop keyboard (blind typing). A virtual keyboard can come later.

## 4. Things to keep in mind

1. **Text readability.** The A08 gives each eye about 800x720 pixels across a wide field of view, which is roughly 8 pixels per degree. A comic page or web page has to fill a large part of your view to be readable. Small browser text will be hard to read. Theatre mode places panels big and close for this reason; zoom and browser page zoom help too.
2. **Laptop load.** The i7-8650U already runs Godot plus a QSV encode. Adding a video decode and a headless browser on top is the real limit. Hardware decode (QSV) and pausing hidden surfaces keep it manageable. Only one live surface (video or browser) runs at a time at first.
3. **DRM video** (Netflix and similar) renders black in headless or screencast browsers. YouTube and ordinary sites work. Local files are the reliable path.
4. **Your media stays where it is.** A `library.json` lists your folders (comics, videos, pictures). Nothing is copied into the repo, and the media folders stay out of git.

## 5. Folder layout

```
home_space/
  PLAN.md                 ← this file
  library.example.json    ← which folders to show (your copy: library.json, git-ignored)
  service/
    media_service.py      ← headless media: index, thumbnails, video decode, headless Edge, comics
    requirements.txt
  godot/                  ← separate Godot 4.7 project for the home
    project.godot
    home.tscn             ← the space, built in the editor (you design it)
    stations/             ← reusable pieces you drag into the room:
      screen.tscn         ←   video screen
      shelf.tscn          ←   comic/book shelf (spines from the library)
      gallery_frame.tscn  ←   picture frame
      terminal.tscn       ←   browser panel
    scripts/              ← HMD rig + player copied from the school project for now
start_home.bat            ← (repo root) bridge + media service + home app
```

The room is a **scene you build in the Godot editor**, not something generated from a text map. The stations are ready-made scenes with the wiring inside. You place them, size them and point them at a folder or URL in the Inspector.

## 6. Phases

| # | Goal | Done when |
|---|---|---|
| 0 | **Your decisions** (section 7) | The questions below are answered |
| 1 | **Shell:** new Godot project with the HMD rig and player, a placeholder room, `start_home.bat` | You can walk around the empty room in the headset |
| 2 | **Images and comics:** `library.json`, gallery frame, shelf, comic reader, theatre mode | You can take a comic off the shelf and read it page by page in the headset |
| 3 | **Video:** headless ffmpeg decode to a screen, audio in the earbuds, play/pause/seek | A local mp4 plays on a screen in sync with its audio |
| 4 | **Browser:** headless Edge screencast panel with pointer mode and keyboard | You can open a site, scroll and click in the headset, with no Edge window on the laptop |
| 5 | **Headless home app:** hidden window; Godot pushes frames straight to the encoder | Nothing about the home app is visible on the laptop desktop |
| 6 | **Tidy-up:** move the shared HMD code into one `addons/hmd/` used by both the school game and the home | One copy of the rig |

Each phase ends with a headset test before the next one starts.

## 7. Questions for you

1. **The space.** What is it? A cosy room, a big library hall, a cinema plus reading nook, or a different place? Do you want to block it out in the editor yourself, or should I start with a plain box room you then reshape?
2. **Headless meaning.** Does section 1 match what you meant? Specifically: media without windows first, and the whole home app windowless later.
3. **Order.** Is it images/comics → video → browser, or do you want the browser earlier?
4. **Comic formats** you actually have: CBZ, CBR, PDF, or folders of images?
5. **Library browsing.** Should items be physical objects only (shelves and frames you walk to), or should there also be a menu or panel that lists everything?

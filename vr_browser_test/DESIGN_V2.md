# VR Browser test: v2 design and protocol

Status: design for the builders. Implement exactly this. When something here conflicts with the
v1 code, this document wins. When this document says nothing about a detail, keep the v1 behaviour.

Addresses the user feedback after the real-phone test (iPhone + Galaxy A08 in a headset, laptop Xbox
controller relayed):

| # | Feedback | Section |
|---|---|---|
| 1 | VR and even NORMAL (flat) videos are blurry (top priority) | B |
| 2 | Videos are downloaded instead of streamed | A |
| 3 | Split (stereo) videos show as split | C |
| 4 | Everything must work with the controller in the headset: click in VR video mode, virtual keyboard, A-to-click everywhere, show which controller is active | D |
| 5 | Toggle the keyboard with the controller; open new windows; popups become switchable windows; tab strip; in-VR address bar | D, F |

Ownership (section E): the **SERVER builder** owns `server.py`, `video_resolve.py`, `typing_window.py`,
`README.md` and the new `media_probe.py`. The **PAGE builder** owns `web/index.html`. The
contract between them is section G (every message, field and default).

Hard rules carried over from v1 (both builders):
- The access key (`?k=`) is required on `/`, on `/ws/vr` and on **every** `/media/...` URL, including the
  new index and mesh endpoints. The WebSocket Origin check (`origin == https://<host>`) stays. Keep the
  `_SameHostCookies` redirect handler on every upstream fetch. Keep `lock_dir(PROFILE)` and `lock_dir(CACHE)`.
- The phone never fetches googlevideo (or any third-party URL) directly. Everything goes through `/media`.
- Never write yt-dlp `-J` output (it can hold cookies and signed URLs) to disk, except in the last-resort
  download path, which already deletes it in `finally`.
- Do not install anything (in particular not the yt-dlp EJS solver; that is the user's decision).

---------------------------------------------------------------------------------------------------

## A. Streaming (no full download)

### A1. Measured facts the design is based on
- YouTube serves **no muxed (progressive) format** to yt-dlp's current client: video-only and audio-only
  DASH files over https. `avc1` stops at 1080p; 1440p and higher are VP9 (webm) or AV1 (mp4) only.
- The DASH mp4 files have `ftyp, moov(mvex), sidx, moof/mdat...`; the webm files have Cues. Both are
  ready for MSE (Media Source Extensions) without remuxing.
- URLs expire after 6 h (`expire=` query parameter) and are bound to the laptop's IP.
- One open-ended upstream read can be slow (0.75 MB/s seen); closed 2 MB chunks, 3 in parallel, gave
  21.7 MB/s. 4K VP9 needs about 2.1 MB/s.
- `yt-dlp -J` takes about 7 s. That is the real start-up cost, so it is prefetched (A4).
- The flat videos the user tested really were 1080p (Big Buck Bunny = itag 299, 1920x1080). The flat
  blur is a render problem (section B), not a format problem.

### A2. Playback modes (the `mode` field of the "ready" message)
| mode | When | Phone side |
|---|---|---|
| `mse` | separate video + audio streams, the phone has `ManagedMediaSource` or `MediaSource`, and the index endpoint works | one `<video>`, two SourceBuffers, segment fetches (A6) |
| `split` | separate streams but no MSE on the phone (old iPhone Safari), or MSE failed on the phone | muted `<video>` + `<audio>`, audio is the clock (A7) |
| `file` | one file with picture and sound: a cached mp4, a progressive format, a downloaded+merged file, or the page's own video file | `video.src = v.src` (v1 behaviour) |

If MSE fails on the phone for any reason (index 404, `addSourceBuffer` throws, decode error within the
first 5 s), the phone switches itself to `split` with the same `v.src` / `a.src` and tells the server
(`{"t":"vfail"}`, G2). If `split` fails too, it exits to the browser with an error card (v1 behaviour).

### A3. Resolve order (server, `VRBrowser._resolve`)
Stop at the first step that gives a playable result.
1. **Non-YouTube page whose `<video>` plays a plain http(s) file of type `video/*`** → `_direct` (v1 step 0),
   mode `file`.
2. **Cached mp4 instant path**: `cache/youtube_<id>.mp4` + `.json` exist AND the json's `projection`
   is `flat` → mode `file` (1080p is enough for flat, see B). Cached spherical files are ignored (they were
   1080p and some were wrongly projected); streaming gives better quality.
3. **yt-dlp lookup** (`Resolver.lookup`, A4; memory cache or prefetched). Then, in order:
   1. separate streams (A5) → mode `mse` if the phone caps say `mse != "none"`, else `split`;
   2. a progressive muxed https format playable per the phone's `file` caps → mode `file` through the proxy;
   3. **last resort**: the v1 download+merge (`_download`, avc1 ≤1080, with the progress bar) → mode `file`.
      Used only when yt-dlp has no usable https format (e.g. HLS/DASH-manifest-only sites).
4. **The page video's own file** (`_direct`, any type that is not a playlist) → mode `file`.
5. **Screen mode** (live page on the cinema screen, sound on the laptop) → `state:"screen"` (v1).

Progress messages: while the lookup runs, send `{"t":"video","state":"progress","text":"Looking up the video..."}`
**without** `pct` (the phone shows no bar when `pct` is absent). Only the download path sends `pct`.

### A4. Lookup cache and prefetch (`video_resolve.py`)
- `Resolver.lookup(page_url, cookies, force=False) -> dict info` runs `yt-dlp -J` (same args as v1:
  `--no-playlist --no-warnings --cookies <tmp> <js_runtime_args>`), keeps the parsed info **in memory only**.
- Memory cache: key `"yt:<id>"` for YouTube, else the page URL without `#`. TTL **5 h** (URLs live 6 h).
  At most **30** entries (drop the oldest). `force=True` skips the cache and replaces the entry.
- Concurrent lookups of the same key share one `asyncio.Future` (no double yt-dlp runs).
- **Prefetch**: on `Page.frameNavigated` (main frame) or `Page.navigatedWithinDocument` of **any** tab whose
  URL is a YouTube `/watch`, `/shorts/<id>` or `youtu.be/<id>` URL, start `lookup()` in the background with
  the YouTube cookies (same cookie URLs as v1). At most **2** prefetches run at once; others are dropped
  (not queued). A prefetch is never cancelled by navigation (it is cheap and populates the cache). Errors
  are logged at one line and ignored.
- The temporary `cookies_<tag>.txt` file is still deleted in `finally` (v1).

### A5. Format choice for the phone (`video_resolve.pick_streams(info, caps, kind) -> dict | None`)
Inputs: yt-dlp `info`, the phone caps (G2 `"caps"`), and `kind` from A5.1.

**A5.1 Content kind before the header read**
- `sphere` if any https video format's `format_note` matches `\b\d{3,4}s(\d\d)?\b` (YouTube writes
  "2160s", "1080s60") or contains `equi` or `mesh`.
- else `flat3d` if the title/tags match the C3 side-by-side or top/bottom keywords;
- else `flat`.

**A5.2 Candidates** (video): `protocol` in (`http`,`https`), has `url`, `vcodec != "none"`, `acodec == "none"`,
not `has_drm`. Codec family from `vcodec`:
`avc1*`/`h264` → `avc1` (ext mp4); `vp09*`/`vp9` → `vp9` (ext webm) or `vp9mp4` (ext mp4);
`av01*` → `av1`. Anything else is skipped. Use the **format's own** `width`/`height`/`fps`, never the
top-level info's (old YouTube 3D: the top-level size comes from the HLS version).

**A5.3 Playable test.** With `t = caps.mse != "none" ? "mse" : "file"`, `fps60 = (fps or 30) > 31`,
`row = caps.v[family][t]` = `[s30, s60, pe30, pe60]` (max smooth height at 30 fps, at 60 fps, max
power-efficient height at 30 / 60 fps; 0 = not supported):
- playable if `height <= (fps60 ? row[1] : row[0])` and `width <= min(caps.mtex, caps.vtex)`
  (`vtex` = per-frame video texture budget: 4096 on phones/tablets, 8192 on desktops; v2 review fix);
- power-efficient if `height <= (fps60 ? row[3] : row[2])`.
Missing family in caps = not playable.

**A5.4 Height cap per kind**: `flat` 1080, `flat3d` 2160, `sphere` 4320 (in practice limited by the phone caps
and `caps.mtex`).

**A5.5 Pick**: take the highest height `H <= cap` that has at least one playable candidate. At `H`:
first a power-efficient candidate in the order `av1, vp9, vp9mp4, avc1`; if none is power-efficient,
a playable one in the order `avc1, vp9, vp9mp4, av1` (software AV1 is the heaviest). Ties: higher
`tbr`. Never drop below the highest playable height to get a power-efficient codec.

**A5.6 Audio**: `vcodec == "none"`, `acodec != "none"`, https, not DRC (`"drc"` not in `format_id` and
not in `format_note` lower-cased). Language: prefer `"(default)"` in `format_note`, else
`language == info.language`, else any. Container: if the video is webm and `caps.a.opus` → opus webm
(251 class), else m4a AAC (140 class) if `caps.a.aac`, else whichever the phone supports. Among those
prefer `abr` closest to 128 kbps (never above 160).
- MSE note: video webm + audio mp4 is fine (separate SourceBuffers).

**A5.7 Result** `{"v": fmt, "a": fmt, "kind": kind}`, or `None` (no playable pair) → continue with A3.3.2.

**Default caps** (no phone has sent `caps` yet, or caps failed): `mse:"none"`,
`v:{avc1:{file:[1080,1080,1080,1080],mse:[0,0,0,0]}}`, `a:{aac:true,opus:false}`, `mtex:4096`.
With several phones connected, use the caps of the phone that most recently sent `caps` **and** is still
connected (one-viewer test; no intersection logic).

### A6. MSE on the phone (PAGE)
- `MS = window.ManagedMediaSource || window.MediaSource`. With ManagedMediaSource set
  `video.disableRemotePlayback = true` before attaching, and only fetch while `ms.streaming` is true
  (listen to `startstreaming` / `endstreaming`; MediaSource has no such flag: treat it as always true).
- Attach: `video.src = URL.createObjectURL(ms)` (revoke on stop). On `sourceopen`:
  `ms.duration = ready.duration` (if a positive number), `addSourceBuffer(v.mime)` and `addSourceBuffer(a.mime)`,
  mode `"segments"`.
- Fetch `GET v.index` and `GET a.index` (JSON, G3). Append each `init` range, then set
  `video.currentTime = start` (if `start > 1` and `< duration - 2`), then the segments.
- Segment fetch: `fetch(src, {headers: {Range: "bytes=a-b"}, signal})` with `a,b` from the index; 206 expected.
  One fetch in flight per track.
- Buffer targets: ahead **30 s** (video height ≤ 1080) or **15 s** (higher); keep **10 s** behind; every
  2 s `remove(0, currentTime - 10)` when more than that is buffered behind. On `QuotaExceededError`:
  `remove(0, currentTime - 2)`, halve the ahead target (minimum 6 s), retry the append once.
- Seek (`seeking` event): abort the in-flight fetches (`AbortController`); if `currentTime` is already
  inside a buffered range, continue from the end of that range; else `sb.abort()` (only when not
  `updating`, otherwise after `updateend`) and fetch the segment containing the target. Do not wait for
  `seeked` to start the fetch.
- End: when both tracks have appended their last segment → `ms.endOfStream()` (only when neither SourceBuffer
  is updating).
- Fetch errors: retry the same segment after 1 s, then 3 s; the third failure is a fatal stream error →
  exit to the browser with the error card ("Lost the video stream").
- The WebGL texture path (`updateVideoTexture`) is unchanged: it reads the same `<video>`.

### A7. Split mode (two elements, PAGE)
- A second element `<audio id="aud" preload="auto">` next to `#vid`. Unlock it in the start tap exactly like
  the video (`unlockVideo` plays+pauses both).
- `video.muted = true`, `video.src = v.src`, `aud.src = a.src`. Both get `currentTime = start` on
  `loadedmetadata`. Play both. **Audio is the master clock.**
- Every 250 ms while playing: `drift = video.currentTime - aud.currentTime`.
  - `|drift| > 0.08 s`: `video.playbackRate = drift > 0 ? 0.97 : 1.03`; back to `1.0` when `|drift| < 0.03`.
  - `|drift| > 0.5 s`: hard seek `video.currentTime = aud.currentTime`, **at most once every 3 s**.
  - Three hard seeks within 20 s = the phone cannot keep up: send `{"t":"vfail","mode":"split","reason":"lag"}`;
    the server answers with a new "ready" one rung lower (next lower height in the same pick rules, A5.5,
    excluding heights ≥ the failing one). If there is no lower rung, keep playing.
- Pause/play/seek act on both elements; seeking sets `aud.currentTime` first, then the video.
- iOS: a WebSocket-triggered `play()` is not a user gesture; the start-tap unlock covers it. If `aud.play()`
  is rejected with `NotAllowedError`, use the v1 "Tap the screen to start the video" path for both.

### A8. Media proxy (SERVER, `server.py` + `media_probe.py`)
Media table entries (`vb.media[mid]`, `mid = secrets.token_hex(8)`):
```
{"kind":"file", "path"}                                         # cached / downloaded mp4
{"kind":"proxy","url","headers","ctype"?}                       # v1 generic proxy (direct file, progressive)
{"kind":"yt", "url","headers","fid","vid_key","mime","size"?,"expire","index"?,"chunked":true}
```
For mode `mse`/`split` there is one `mid` per playback with two entries stored as `vb.media[mid] = {"v": ent, "a": ent}`;
for mode `file` `vb.media[mid] = {"f": ent}`. Entries older than 6 h are dropped; at most 40 `mid`s (drop oldest).

Routes (all: key check first → 403; unknown mid/track → 404):
| Route | Answer |
|---|---|
| `GET /media/{mid}/{track}` (`track` ∈ `v`,`a`,`f`) | the bytes (Range supported) |
| `GET /media/{mid}.mp4` | alias of `/media/{mid}/f` (v1 compatibility) |
| `GET /media/{mid}/{track}/index` | JSON segment index (G3), `Cache-Control: no-store` |
| `GET /media/{mid}/mesh` | JSON projection mesh (G4) or 404 |

Byte serving for `kind:"yt"` (chunked upstream):
- Requested `bytes=a-b`: if `b - a + 1 > 16 MiB` clamp `b = a + 16 MiB - 1`. Open-ended `bytes=a-` (and no
  Range at all): `b = min(size - 1, a + 8 MiB - 1)`. Answer **206** with the honest
  `Content-Range: bytes a-b/size` (browsers request the rest themselves). No Range header (not sent by
  media elements or the MSE fetches) → 200 with `Content-Length: size`, streamed through the same chunker.
- `size` comes from the index probe or the first upstream response's `Content-Range` total; store it in
  the entry.
- Upstream: split `a..b` into **2 MiB** closed chunks, fetch up to **3 in parallel** (threads via
  `asyncio.to_thread`, `_HTTP.open` with the entry headers + `Range`), yield in order. A per-`mid` semaphore
  limits upstream connections to **4** across concurrent phone requests. If the phone disconnects, cancel the
  pending chunk tasks.
- `Content-Type`: the entry's `mime` without codecs (`video/mp4`, `video/webm`, `audio/mp4`, `audio/webm`).
- **Expired URL**: before fetching, if `time.time() > expire - 120`, refresh. On an upstream **403 or 410**,
  refresh and retry that chunk **once**. Refresh = `resolver.lookup(watch_url, cookies, force=True)`, find the
  format with the same `format_id`, replace `url`/`headers`/`expire`. One refresh per `mid` at a time
  (shared future). Refresh failure → 502 and a log line.
- Other `kind`s use the v1 `file_response` / `proxy_response` unchanged.

### A9. Segment index and container probe (`media_probe.py`, SERVER)
Pure functions on bytes plus one async helper that fetches through the entry (reuse the chunk fetcher).
- **mp4**: read `bytes=0-65535`; walk top-level boxes. `init = [0, end of moov - 1]`. Find `sidx`
  (if not in the first 64 KB, read up to 1 MiB). Parse version 0/1, timescale, earliest PTS,
  `first_offset`, references (`reference_type` 1 = nested sidx → not supported → 404).
  Segment `k`: `start = sidx_end + first_offset + Σ sizes before k`, `end = start + size - 1`,
  `t = (earliest + Σ durations before k) / timescale`, `d = duration / timescale`.
- **webm**: read `bytes=0-65535`; parse EBML header, `Segment` (data offset = Segment payload start),
  `SeekHead` → `Cues` position, `Info` (`TimecodeScale`, default 1 000 000; `Duration`). Fetch the Cues
  element (its position is relative to the Segment payload start). Each `CuePoint`: `CueTime`,
  `CueTrackPositions/CueClusterPosition`. `init = [0, first cluster position - 1]`. Segment `k` runs from its
  cluster to the next cluster − 1 (the last to `size - 1`); `t = CueTime * TimecodeScale / 1e9`, `d` = next `t` − `t`
  (last: `duration - t`).
- **Projection header read** (C2): parse `moov/trak/mdia/minf/stbl/stsd/<sample entry>` children `st3d`
  and `sv3d` (`proj` → `prhd`, then `equi` | `cbmp` | `ytmp`/`mshp`). The visual sample entry's children start
  78 bytes after its header (8-byte box header + 78 bytes of fixed fields). Read this from **an mp4 format of
  the same video** (the chosen one if it is mp4; otherwise the lowest-height `avc1` or `av01` https format;
  header boxes are the same across codecs of one video). Read `bytes=0-262143`; if `moov` is not complete,
  read up to 2 MiB.
- **Mesh** (`ytmp`/`mshp`): port `scratchpad/stereo/mesh.py` (the investigation's decoder, ExoPlayer's
  ProjectionDecoder logic): `mshp` header (version/flags, crc32, 4-byte encoding: `raw ` or `dfl8`; `dfl8`
  → `zlib.decompress(d, -15)`), then one or two `mesh` boxes: `coordinate_count` (u32) floats,
  `vertex_count` (u32), vertices of 5 indices (x,y,z,u,v) each `ceil(log2(coordinate_count*2))` bits as
  zigzag deltas, byte align, `vertex_list_count` (u32), lists of (`texture_id` u8, `index_type` u8: 0 triangles,
  1 strip, 2 fan, `index_count` u32, indices of `ceil(log2(vertex_count*2))` bits zigzag deltas).
  Result kept in the media entry and served by `/media/{mid}/mesh` (G4).
- The mesh and box-walker logic came from throwaway prototype scripts (`mesh.py`, `box.py`); it now lives in `media_probe.py`.

### A10. What stays in `cache/`
- `cache/<key>.json`: per-video metadata only, **no URLs, no cookies**. v2 schema (G5). Written after
  detection and when the phone reports an image-test or user projection.
- Existing `cache/*.mp4` files: used only by A3.2. New mp4 files are only written by the last-resort download.
- README: "Videos are streamed. Old downloaded videos in `cache\` can be deleted."

### A11. Start-time target
Prefetched lookup + MSE: first frame within **1 to 2 s** of the play event (header read + index ≈ 0.3 s,
first segment ≈ 0.3 to 1 s). Not prefetched: about 7 s for the lookup + the same.
Seek: one segment, 0.2 to 1.5 s.

---------------------------------------------------------------------------------------------------

## B. Render quality (PAGE, `web/index.html`)

Root causes, in order of impact for flat video: (1) few pixels per eye on these phones, (2) trilinear
mipmapping throws away a further ~1/3 mip level, (3) on iPhone the canvas is drawn at 2/3 resolution
(DPR cap 2), (4) a 100° screen is wider than the ~86° eye field of view. The browser panel uses the same
texture path; text just survives minification better than film.

### B1. Sharper minification (flat video AND browser panel)
FS gets `uniform int uSharp;`. When `uMode == 0 && uSharp == 1` sample with a 4-tap box at LOD bias −1:
```glsl
vec2 dx = dFdx(uv) * 0.25, dy = dFdy(uv) * 0.25;
c = (texture(uTex, uv + dx + dy, -1.0) + texture(uTex, uv + dx - dy, -1.0) +
     texture(uTex, uv - dx + dy, -1.0) + texture(uTex, uv - dx - dy, -1.0)) * 0.25;
```
`uSharp = 1` for: the browser panel, the flat cinema screen (`flat`, `flat_sbs`, `flat_tb`), the tab strip /
address bar, the keyboard, the player bar. `uSharp = 0` for sphere and mesh projections and the HUD card.
Derivatives must be taken in uniform control flow: compute `dFdx/dFdy` before any branch on per-fragment data.
URL test switch `?sharp=0` forces `uSharp = 0` everywhere (for the before/after screenshots).

### B2. No per-frame mipmaps for big/sphere video
- `uploadTex(T, src, w, h, mips)`: `generateMipmap` only when `mips` is true.
- `videoTex` filter follows the projection: flat family → `LINEAR_MIPMAP_LINEAR` + mips on every upload;
  sphere family and `mesh` → `TEXTURE_MIN_FILTER = LINEAR`, no `generateMipmap`. Set the min filter
  whenever `state.proj` changes (also when Y cycles).
- `panelTex`, `hudTex` and the new canvas textures keep mipmaps.

### B3. Canvas resolution
- `resize()`: `dpr = Math.min(devicePixelRatio || 1, state.stereo ? 3 : 2)`; test override `?dpr=N`.
- Keep `antialias: true`.
- Send the real numbers in `hello` (G2): `w`, `h`, `dpr`, per-eye size and the px/deg at the centre:
  `ppd = (eyeH / 2) / Math.tan(fov / 2) * Math.PI / 180`. The server logs them (B7).

### B4. Cinema screen size
- Default `cinemaSpan` from the lens: `eyeAspect = (canvas.width / 2) / canvas.height` (stereo; mono uses the
  full width), `span = 2 * atan(tan(lens.fov/2) * eyeAspect) * 0.9`, clamped to 60°..100°.
  (A08 at fov 80 → about 77°.) Recomputed on resize and lens change **unless** the user changed the size.
- The user's size (left stick in VR, or the player bar −/+ buttons, ±10° per click, range 45°..170°) is
  saved as `localStorage["vrb_span"]` (degrees, try/catch) and wins over the default from then on.

### B5. Per-fragment sphere sampling
VS: add `out vec3 vDir; vDir = aPos;` (object-space position; for the screen surface it is unused).
FS: `uniform int uProj;` (0 = use the (transformed) mesh UV, 1 = equirect 360, 2 = equirect 180, 3 = EAC),
`uniform vec4 uWin;` (eye window `s0, t0, ds, dt`, t = 0 at the top), `uniform vec2 uTexel;` (1/texture size).
```glsl
vec3 d = normalize(vDir);
float lon = atan(d.x, -d.z);                 // 0 straight ahead (-Z), + to the right (+X)
float lat = acos(clamp(d.y, -1.0, 1.0));     // 0 up
vec2 uv;
if (uProj == 1) uv = vec2(lon / 6.2831853 + 0.5, lat / 3.1415927);
if (uProj == 2) uv = vec2(lon / 3.1415927 + 0.5, lat / 3.1415927);   // outside 0..1 -> black (after sampling)
// seam: use the u derivative of fract(u + 0.5) when it is smaller
vec2 g1x = dFdx(uv), g1y = dFdy(uv);
vec2 u2 = vec2(fract(uv.x + 0.5), uv.y);
vec2 g2x = dFdx(u2), g2y = dFdy(u2);
if (abs(g2x.x) + abs(g2y.x) < abs(g1x.x) + abs(g1y.x)) { g1x.x = g2x.x; g1y.x = g2y.x; }
vec2 st = uWin.xy + uv * uWin.zw;
st = clamp(st, uWin.xy + uTexel * 0.5, uWin.xy + uWin.zw - uTexel * 0.5);   // no bleed from the other eye
c = textureGrad(uTex, st, g1x * uWin.zw, g1y * uWin.zw);
if (uProj == 2 && (uv.x < 0.0 || uv.x > 1.0)) c = vec4(0.0, 0.0, 0.0, 1.0);
```
- Remove the old `uHalf` early `return` (undefined derivatives).
- The SPHERE mesh stays (96x48) as geometry; its UVs are no longer used for `uProj` 1..3.
- **EAC** (`uProj == 3`): pick the major axis of `d`, face-local `(qx, qy)` in −1..1, remap each with
  `q' = (2/π)·atan(q)` (−0.5..0.5), +0.5 → 0..1, then into the face's tile of the 3x2 layout
  (each tile `1/3` wide, `1/2` high). Layout per the stereo investigation: top row left, front, right;
  bottom row bottom, back, top, bottom-row tiles rotated 90°. Tile orientation MUST be verified on
  `scratchpad/stereo/frame_ZvZ7da8JBUk.jpg` (load it as `?demo=eac&img=<path>`-style local test or by
  temporarily pasting it into the demo canvas) and fixed until straight lines look straight; document the
  final face table in a comment. EAC is a fallback: YouTube EAC videos use the server mesh (`mesh`), which
  is exact.

### B6. Mesh projection (`mesh`)
- On "ready" with `mesh` non-null, the phone fetches `GET mesh` (G4) and builds one GL mesh per entry
  (`makeMesh` must support `Uint32Array` indices and `TRIANGLE_STRIP` / `TRIANGLE_FAN`). Until it has loaded
  draw nothing (stars stay).
- Draw with `VProt`, model scale 30 (like the sphere), `uProj = 0`, filter as in B2 (no mips).
- Per-eye `uUVT` (applied as `s = u * x + y`, `t = v * z + w`; the mesh's v counts from the **bottom**):
  | stereo | left eye | right eye |
  |---|---|---|
  | mono | `(1, 0, -1, 1)` | same |
  | sbs | `(0.5, 0, -1, 1)` | `(0.5, 0.5, -1, 1)` |
  | tb | `(1, 0, -0.5, 0.5)` | `(1, 0, -0.5, 1)` |
  With two meshes use mesh 0 for the left eye and mesh 1 for the right; with one mesh, both eyes.
  If the picture comes out upside down on the first real VR180/EAC test, flip `z,w` (that is the one
  unverified assumption).

### B7. Diagnostics
- 2D debug HUD line 4: `eye 852x786  8.2 px/deg  screen 77°  video 1920x1080 avc1 (mse)`.
- Server logs `hello` as `[phone] <ip> canvas WxH dpr D eye EWxEH fov F ppd P`.

### B8. Proof (PAGE builder, required)
- New demo `?demo=flat1080`: a 1920x1080 test canvas (resolution wedge, 1-px line pairs, 12/16/24 px text,
  a photo-like gradient) shown on the flat cinema screen. `?demo=flat` stays.
- Screenshots with headless Edge at `--window-size=1600,720` (A08 shape) and `1704,786` (iPhone capped
  shape): `?demo=flat1080&sharp=0&dpr=2` (old path) vs `?demo=flat1080` (new path, `dpr` default), same crop
  at the screen centre. Save to the scratchpad; report the paths.

---------------------------------------------------------------------------------------------------

## C. Projections and stereo detection

### C1. Projection ids (page `PROJS`, server `PROJECTIONS`)
| id | Label (HUD) | Geometry | Eye windows `[s0,t0,ds,dt]` L / R | Screen aspect |
|---|---|---|---|---|
| `flat` | Flat screen | cinema | `[0,0,1,1]` both | `w/h` |
| `flat_sbs` | 3D side by side | cinema | `[0,0,.5,1]` / `[.5,0,.5,1]` | `a=(w/2)/h`; if `a < 1.2` use `2a` |
| `flat_tb` | 3D top/bottom | cinema | `[0,0,1,.5]` / `[0,.5,1,.5]` | `a=w/(h/2)`; if `a > 2.6` use `a/2` |
| `equirect` | 360 | sphere `uProj 1` | full both | — |
| `equirect_tb` | 360 3D top/bottom | sphere `uProj 1` | top / bottom halves | — |
| `equirect_sbs` | 360 3D side by side | sphere `uProj 1` | left / right halves | — |
| `half_sbs` | 180 3D (VR180) | sphere `uProj 2` | left / right halves | — |
| `half_mono` | 180 | sphere `uProj 2` | full both | — |
| `half_tb` | 180 3D top/bottom | sphere `uProj 2` | top / bottom halves | — |
| `eac` | 360 cube map | sphere `uProj 3` | from `stereo`: mono full both; tb top/bottom halves; sbs left/right halves | — |
| `mesh` | VR180 / 360 (EAC) from the video (label from `mesh_label`) | server mesh | B6 table, `stereo` field | — |

`state.force2D` (player bar 2D/3D): when true, both eyes use the **left** eye window (comfortable 2D view of a
3D video). Reset to false on every new video.

**Y cycle order**: `flat → flat_sbs → flat_tb → equirect → equirect_tb → equirect_sbs → half_sbs → half_mono → half_tb → eac → mesh (only if the video has a mesh) → flat`.
Each step shows its label in the HUD card; the projection the server detected is marked "(auto)".
Every Y press (and every player-bar projection change) sends `{"t":"proj",...,"source":"user"}` (G2);
the server saves it, so it wins next time.

### C2. Server detection order (`video_resolve.detect_projection`)
Stop at the first firm answer; record `projection_source`.
0. **User choice** saved in `cache/<key>.json` (`source:"user"`) → use it. A saved `image` result → use it too.
1. **Container** (A9 header read):
   - `sv3d/proj/equi`: bounds = 4 × u32 after version/flags, fractions of 2^32 (top, bottom, left, right).
     If left and right are each ≥ 0.2 (about 0.25 = 180°) → `half_mono`/`half_sbs`/`half_tb` by st3d; else
     `equirect`/`equirect_tb`/`equirect_sbs` (st3d 0/1/2 = mono/tb/sbs).
   - `cbmp` → `eac`, `stereo` from st3d (eye windows per C1).
   - `ytmp`/`mshp` → `mesh` with `stereo` from st3d; `mesh_label` = "VR180" if the vertex yaw span ≤ 200°,
     else "360 (EAC)".
   - `st3d` without `sv3d`: 1 → `flat_tb`, 2 → `flat_sbs` (old YouTube 3D).
   - `st3d` 0 / no boxes → no information, go on.
2. **format_note** (only when the header read failed): `equi_threed_top_bottom` → `equirect_tb`;
   `equi_threed_left_right` → `equirect_sbs`; `equi` → `equirect` (overrides "3D" in the title);
   `mesh` → `half_sbs` if the title says 180/VR180, else `eac`.
   Old-3D sign: chosen DASH format aspect 0.85..0.92 while an m3u8 format of the same height is ≈1.78 → `flat_tb`.
   (v1 bugs fixed: the note says `threed`, not `3d`; `mesh` is not equirect.)
3. **Title, tags, first 300 chars of the description** (only if 1 and 2 found nothing):
   - SBS: `\b(h-?sbs|half[- ]?sbs|f-?sbs|full[- ]?sbs|sbs|side[- ]by[- ]side|left[- ]right|lr 3d)\b`
   - TB: `\b(h-?ou|over[- ]under|top[- /]?bottom|tab|ou|3d tb)\b`
   - 180: `vr ?180|180 ?°|180vr`; 360: `360|equirect|spherical`; stereo-ish: `\b3d\b|stereo(scopic)?`
   - Combine with the chosen format's aspect: 360+TB or 360 at 1:1 → `equirect_tb`; 360+SBS at ≥3.5:1 →
     `equirect_sbs`; 360 otherwise → `equirect`; 180 + (3D or SBS) → `half_sbs`; SBS → `flat_sbs`;
     TB → `flat_tb`; else `flat`. Source `title`. A title result is only a candidate (the phone image test may
     confirm or reject it, C3).
4. Nothing → `flat`, source `guess`.

`image_test` in "ready" = `projection_source` ∈ {`title`, `guess`}.

### C3. Phone image test (PAGE)
Runs when `ready.image_test` is true, in modes `mse`/`split`/`file`, after playback starts.
- Samples at video time ≈ start+1.5, +3, +5, +8, +12 s (use `timeupdate`; skip while paused). Draw the video
  into a 128x72 2D canvas, `getImageData` (same origin, never tainted), grey = 0.299R+0.587G+0.114B.
- Per sample: edge map `|dx|+|dy|`. Skip the sample if the mean edge strength < 2.0 (0..255 scale) (black
  frames, fades). For the left half vs right half: max normalised cross-correlation over shifts
  x ∈ [−4,4], y ∈ [−1,1], both on raw grey (`LR`) and on edges (`LRedge`). Same for top vs bottom (`TB`, `TBedge`).
- After 5 samples (or 3 valid ones by +12 s), take the medians:
  - **SBS** if `LRedge ≥ T && LR ≥ 0.85 && LRedge − TBedge ≥ 0.3`;
  - **TB** with LR and TB swapped;
  - `T = 0.62`, or `0.45` when the server's candidate (source `title`) already says that layout.
  - `0.45 ≤ edge < 0.62` without a matching candidate: no switch; HUD hint "Looks 3D? press Y" (4 s).
  - Upgrade within the family only: `flat → flat_sbs/flat_tb`, `equirect → equirect_tb/equirect_sbs`,
    `half_mono → half_sbs/half_tb`. A title candidate that the test does **not** confirm (e.g. `flat_sbs`
    candidate, LRedge < 0.45) is downgraded to the family's mono id (`flat`, `equirect`, `half_mono`).
  - HUD: "Detected 3D side by side · Y to change" (or top/bottom) for 4 s.
  - Send `{"t":"proj","vid","projection","source":"image","scores":{...}}` (also when it confirms mono, so the
    test is not repeated next time).
- Abort the test when the user presses Y or uses the projection buttons, or when the video stops.

---------------------------------------------------------------------------------------------------

## D. Controller-only UI (PAGE, plus the server parts noted)

### D1. Input layer
- Bits as v1 (Godot order): 0 A, 1 B, 2 X, 3 Y, 4 View/Back, 6 Start, 7 L3, 8 R3, 9 LB, 10 RB, 11 up, 12 down,
  13 left, 14 right. **New virtual bits**: 15 = LT, 16 = RT, from the analog triggers (`lt`, `rt` from the
  laptop relay; `buttons[6].value` / `buttons[7].value` on the phone Gamepad API; `lt`/`rt` in
  `window.__vrbTestPad`). Pressed when > 0.6, released when < 0.3 (hysteresis). `pollPads` loops b = 0..16.
- **Hold** support: `onButtonUp(b, heldMs)` is added next to `onButton(b)`; R3 uses it (D5).
- Repeat: X (backspace) and the d-pad/left stick in the keyboard repeat after 400 ms every 80 ms.
- **Every input wakes the UI**: any button edge or stick beyond the dead zone calls `uiActivity()`, which
  shows the player bar in VR mode (D3) and the controller badge.

### D2. Controller source badge
`padSource()` returns one of:
| state | text | colour |
|---|---|---|
| laptop pad fresh (< 1.5 s) and not `pad,none` | `Pad: laptop` | green |
| phone Gamepad API connected | `Pad: phone (<id, 24 chars>)` | green |
| both | `Pad: laptop + phone` | green |
| WebSocket up, laptop says `pad,none`, no phone pad | `No controller (plug it into the laptop or pair it with the phone)` | orange |
| WebSocket down and no phone pad | `No controller (laptop not connected)` | red |
- Shown on the right end of the tab strip (browser mode), in the player bar's top row (VR mode), in the
  keyboard's preview row, and in the 2D debug HUD.
- When the state changes after the first 3 s, show it as a 3D HUD toast for 3 s ("Controller: laptop connected").

### D3. VR player bar (mode `vr`)
- A canvas texture **2048x320** on a quad **1.8 m x 0.28 m**, 2.0 m away, pitch −22°. Yaw: 0 for flat
  projections; for sphere/mesh the head yaw at the moment it appears (like `hud3.yaw`). Drawn after the video,
  before the HUD card, `uSharp = 1`, alpha fade 0.15 s.
- Top row (y 0..90): title (left, ellipsis), `12:34 / 45:00` (centre), pad badge (right).
- Button row (y 100..320), left to right, ids and labels:
  `back` "◀ Browser" · `rew` "−10" · `play` "▶"/"❚❚" · `fwd` "+10" · `seek` (a 600-px wide bar with buffered
  ranges in grey and the position in blue) · `proj` "<label> ▸" · `stereo` "2D"/"3D" · `smaller` "−" ·
  `bigger` "+" · `recenter` "⌖" · `kbd` "⌨".
  `smaller`/`bigger` are greyed out (no action) for sphere/mesh projections.
- Actions: `back` = exit to the browser (v1 `exitVr`); `rew`/`fwd` = seek ∓10 s; `play` = toggle;
  `seek` = jump to `fraction × duration` at the hit point; `proj` = next projection in the Y cycle;
  `stereo` = if the current projection is stereo → toggle `state.force2D`; if mono → switch to the family's
  stereo id (`flat→flat_sbs`, `equirect→equirect_tb`, `half_mono→half_sbs`, others: no-op) and send
  `proj` source `user`; `smaller`/`bigger` = cinema span ∓10°; `recenter` = `recenter()`; `kbd` = toggle the
  keyboard (D4).
- Visibility: appears on any input (D1), on a screen tap, on pause and for 4 s when a video starts; hides 4 s
  after the last input **while playing**; stays while paused, while the gaze ring is on it, or while it has a
  highlight that was set less than 4 s ago.
- Pointing: gaze ray (`rayDir` with the head rotation; in VR the right stick still turns the view) intersected
  with the bar quad; when it hits a button, that button is highlighted and the ring is drawn on the bar.
  **LB/RB** move the highlight left/right through the buttons (and show the bar); a highlight set by LB/RB
  stays until the gaze moves to another button or 4 s pass.
- **A in VR mode**: if the bar is visible and a button is highlighted (gaze or LB/RB) → that button; otherwise
  play/pause. (Touch tap: play/pause + show the bar.)

### D4. Virtual keyboard (all modes)
**Placement**: a canvas texture **2048x768** on a quad **1.3 m x 0.4875 m**, 1.4 m away, pitch −30°,
yaw 0 in browser/screen mode, the head yaw at opening in VR mode. Drawn last before the pointer ring
(on top of panel and video), `uSharp = 1`. While it is open, the gaze/stick pointer hits the keyboard first;
**no `ptr` messages go to the server** while the ring is on the keyboard.

**Rows** (unit = 1 key width; the row widths add up to 11 units):
- Preview row (always): left = the mode text (`Address:` / `Typing into: <input>`), then the text buffer
  with a caret (field mode: what was typed since opening, max last 60 chars, `•` for each char when
  `input == "password"`), right = pad badge and a `✕` close key (1 unit).
- Letters layer:
  1. `q w e r t y u i o p` `⌫`(1)
  2. `a s d f g h j k l` `'` `↵`(1) (label from `enter`: Go / Search / Send / Next / Done / ↵)
  3. `⇧`(1) `z x c v b n m` `,` `.` `⇧`(1)
  4. `?123`(1.5) `Esc`(1) `@`(1) `space`(4) `/`(1) `.com`(1.5) `←`(0.5) `→`(0.5)
- Symbols layer:
  1. `1 2 3 4 5 6 7 8 9 0` `⌫`
  2. `- _ = + : ; " ( ) &` `↵`
  3. `ABC`(1.5) `# $ % * ! ? \ ~ |`(9 keys) `Tab`(0.5)  
  4. `abc`(1.5) `Esc` `@` `space`(4) `/` `.com`(1.5) `←` `→`
- Shift: one-shot (next letter upper case, then off); two presses within 400 ms = caps lock (key drawn lit).
- `email` input: `@` stays where it is (already on row 4). `number`/`tel` inputs open on the symbols layer.

**Modes**
- `field`: keys go **live** to the focused element in the active tab:
  characters → `{"t":"text","s":"<char>"}`; `⌫` → `{"t":"key","key":"Backspace"}`; `↵` → `key Enter`;
  `←`/`→` → `key ArrowLeft/ArrowRight`; `Tab` → `key Tab`; `Esc` → `key Escape`; `.com` → `text ".com"`.
- `address`: keys edit a local buffer (max 2000 chars) with a caret; it opens pre-filled with the current
  URL fully selected (the first typed character replaces it; `←`/`→` deselect and move the caret).
  `↵` (label "Go") → `{"t":"nav","to":"url","url":"<buffer>"}` and close. Empty buffer + `↵` = close.

**Opening and closing**
- **Manual toggle: L3, in every mode** (browser, screen, vr, while resolving). Also the player bar `kbd`
  button and the address bar `⌨` button. Opening: if `state.info.focus` (G1 `info.focus`) is non-null →
  `field` mode with that input type; else `address` mode.
- **Auto-open**: on `{"t":"kbd","open":true,...}` from the server (D6), in browser or screen mode only, unless
  the keyboard is already open in `address` mode.
- **Auto-close**: on `{"t":"kbd","open":false}` only if the keyboard was auto-opened (a manually opened one
  stays). Also closes on: B, L3, `✕`, address `↵`, tab switch, entering VR video mode (unless opened in VR mode),
  leaving VR mode.
- New window (D5) opens it in `address` mode with an empty buffer.

**Controller while the keyboard is open** (overrides the mode's own mapping):
| Input | Action |
|---|---|
| A | press the highlighted key |
| B | close the keyboard |
| X | Backspace (repeats when held) |
| Y | Space |
| Start | Enter (field: `key Enter`; address: Go) |
| LB | Shift (twice = caps lock) |
| RB | switch letters / symbols layer |
| LT / RT | caret left / right |
| D-pad, left stick | move the key highlight (repeat 400 ms / 80 ms) |
| L3 | close the keyboard |
| Right stick | nudge the pointer (as in browser mode) |
| View | lens settings (unchanged) |
Highlight rule: the gaze ring over a key highlights it, except that after a d-pad/stick move the gaze is
ignored until the pointer direction has moved more than 1.5° from where it was at that moment.

### D5. Windows (tabs): page side
**Tab strip and address bar** ("chrome"), browser mode only (hidden when `cinemaT > 0.01`, in VR and screen mode):
a canvas texture **2048x256** on a flat quad **2.4 m x 0.30 m** (same width as the panel), at the panel's distance,
its bottom 0.04 m above the panel's top edge. `uSharp = 1`, mipmapped.
- Row 1 (y 0..120): one tab per window, width `min(330 px, (2048 − 120 − 360) / count)`, title with ellipsis,
  a small loading dot, an `×` (close) on the right of each tab, the active tab lit (#2f81f7 background); then a
  `+` button (120 px), greyed out when `list.length >= max`; the pad badge at the far right (360 px).
- Row 2 (y 128..256): `◀` (greyed when `!canBack`), `▶`, `⟳`, the URL field (the URL without `https://`,
  ellipsis), `⌨`.
- The pointer raycasts the chrome before the panel; while it is on the chrome, no `ptr` is sent; A clicks the
  chrome item: tab → `{"t":"tab","action":"switch","id"}`; `×` → `{"t":"tab","action":"close","id"}`;
  `+` → new window; `◀`/`▶`/`⟳` → `nav back/forward/reload`; URL field and `⌨` → keyboard in `address` mode.
- **New window** (from `+`, R3 tap): if `tabs.list.length >= tabs.max` → HUD "Up to 6 windows: close one first";
  else send `{"t":"tab","action":"new"}` and open the keyboard in `address` mode (empty).
- While the active tab's URL is `about:blank` (a new window), the phone ignores incoming screencast frames and
  shows the placeholder "New window" / "Type an address or search (L3 = keyboard)".

**Window controller mapping (browser mode)**
| Input | Action |
|---|---|
| LT / RT | previous / next window (wraps) |
| R3 tap (< 800 ms) | new window |
| R3 hold 800 ms | close the active window (fires once at 800 ms, HUD "Window closed"; the release is then ignored). HUD "Hold to close the window…" appears at 300 ms |
In screen mode LT/RT/R3 show the hint "Go back to the browser first (B)". In VR mode R3 does nothing; LT/RT do nothing.

### D6. Focus binding (SERVER)
`HOOK_JS` gets a focus reporter (still one injected script, still `__vrbHooked` guarded):
```js
const editable = (el) => !!el && (el.isContentEditable || el.tagName === 'TEXTAREA' ||
  (el.tagName === 'INPUT' && !/^(button|submit|reset|checkbox|radio|range|color|file|image|hidden)$/i.test(el.type || '')));
const kind = (el) => el.isContentEditable ? 'contenteditable' : el.tagName === 'TEXTAREA' ? 'textarea'
                     : String(el.type || 'text').toLowerCase();
addEventListener('focusin', (e) => {
  const el = (e.composedPath && e.composedPath()[0]) || e.target;
  if (!editable(el)) return;
  try { window.__vrbFocus && __vrbFocus(JSON.stringify({kind: 'focus', input: kind(el),
        mode: String(el.inputMode || ''), enter: String(el.enterKeyHint || ''),
        user: Date.now() - lastUser < 2000})); } catch (_) {}
}, true);
addEventListener('focusout', (e) => {
  const el = (e.composedPath && e.composedPath()[0]) || e.target;
  if (!editable(el)) return;
  try { window.__vrbFocus && __vrbFocus(JSON.stringify({kind: 'blur'})); } catch (_) {}
}, true);
```
Never send field values or names. `_hook()` adds the binding `__vrbFocus` next to `__vrbPlay` (also in
iframes). Server handling (payload ≤ 2000 chars, JSON dict, else ignored):
- Track per tab `tab.focus = {"input","enter"} | None`. Include the active tab's focus in `info` (G1).
- `focus` on the **active** tab, and (`user` is true or the last click/key from a phone was < 2 s ago):
  send `{"t":"kbd","open":true,"mode":"field","input","enter"}` to all phones.
- `blur`: wait **150 ms**; if no new `focus` came for that tab, set `tab.focus = None` and send
  `{"t":"kbd","open":false}`.
- Events from non-active tabs only update `tab.focus`.

### D7. Full controls table (for README; replaces the v1 table)

| Button | Browsing | Watching a video (VR) | Keyboard open |
|---|---|---|---|
| A | click (page, tabs, address bar) | button under the ring / highlighted, else play/pause | press key |
| B | back (cancels a video that is getting ready) | back to the browser | close keyboard |
| X | play this page's video in VR | show / hide the player bar | backspace |
| Y | reload | next projection (fixes split / bent videos) | space |
| LB / RB | back / forward | move the player-bar highlight | shift / symbols |
| LT / RT | previous / next window | | caret left / right |
| L3 (press left stick) | keyboard on / off | keyboard on / off | close keyboard |
| R3 (press right stick) | new window (hold: close window) | | |
| Left stick | scroll | screen size (flat videos) | move key highlight |
| D-pad up/down | scroll | | move key highlight |
| D-pad left/right | arrow keys | −10 s / +10 s | move key highlight |
| Right stick | nudge the pointer | turn the view | nudge the pointer |
| Start | re-centre the view | re-centre the view | enter / go |
| View (Back) | lens settings (d-pad tunes, A/B/View close) | lens settings | lens settings |

Lens settings open: d-pad tunes, **A, B or View closes** them (A-to-click everywhere).
While a video is getting ready: A shows "Getting the video ready… B = cancel"; L3 still opens the keyboard.
Touch: tap = click / play-pause (+ player bar), double tap = re-centre, drag = turn.

---------------------------------------------------------------------------------------------------

## F. Windows (tabs): server side (`server.py`)

### F1. Model
```python
class Tab:
    id: str            # short id "1", "2", ... (increasing, never reused in one server run)
    target: str        # CDP targetId
    session: str       # flat sessionId
    url, title: str
    loading, can_back: bool
    focus: dict | None
    children: dict[str, str]   # iframe sessions (per tab, replaces VRBrowser.children)
```
`VRBrowser.tabs: dict[str, Tab]` (insertion order = strip order), `VRBrowser.active: str`.
`self.target` / `self.session` / `self.url` / `self.title` / `self.loading` / `self.can_back` become
properties of the active tab (keeps the rest of the v1 code working). `MAX_TABS = 6`.

### F2. Opening a tab (`_open_tab(url, target_id=None, activate=True) -> Tab`)
- `target_id is None` → `Target.createTarget {"url":"about:blank","newWindow":true,"width":W,"height":H}`.
- `Target.attachToTarget {flatten:true}`, register the session handler, then exactly the v1 per-page setup:
  `Page.enable`, `Network.enable`, UA override, `Emulation.setFocusEmulationEnabled`,
  `Emulation.setDeviceMetricsOverride` (also fixes popup window sizes), `_hook` (both bindings),
  `Target.setAutoAttach` for iframes.
- Then `Page.navigate(url)` if `url` is not `about:blank`. If `activate`, `_activate(tab)`.
- `_start()` opens the first tab with HOME_URL (or the last URL) through this function.

### F3. Popups (replaces `_adopt_popup` / `_close_blank_popup`)
On `Target.targetCreated` with `type == "page"`, `openerId` ∈ our tabs' targets, not one we created:
- If no phone click/key in the last **2 s** (`time.monotonic() - self.last_click > 2`) → it is an unrequested
  popup: `Target.closeTarget`, log one line. (`last_click` is also set by phone `key`/`text` messages.)
- Else if `len(tabs) < MAX_TABS` → `_open_tab(url, target_id=...)`, activate it, toast "Opened in a new window
  (LT/RT to switch)".
- Else → close the target and `Page.navigate` the opener tab to its URL (v1 behaviour); toast "Window limit
  (6) reached: opened here". A popup still at `about:blank` is adopted the same way; it navigates by itself.
- `Target.targetDestroyed` / `Target.detachedFromTarget` for a tab's target (the page called `window.close()`
  or crashed): remove the tab as in F4 (without calling `closeTarget`).

### F4. Switch and close
- `_activate(tab)`: if mode is `vr`/`screen` → `leave_video(None)` first; if mode is `resolving` → cancel it
  (as `_maybe_cancel_resolve`). Run `PAUSE_JS` in the old tab (no hidden sound). Stop the screencast on the old
  session, set `active`, `Target.activateTarget`, `Page.bringToFront` on the new session, start the screencast
  there, clear `last_jpeg`, refresh history (`_history`), send `info` and `tabs`, and send
  `{"t":"kbd","open":false}`.
- `close(id)`: `Target.closeTarget`; remove the tab; if it was active, activate the tab to its right, else the
  left one. Closing the **last** tab opens a new one at HOME_URL. If the closed tab owned the VR video
  (`video_session` is its session or one of its children) → `leave_video(None)` first.
- Play events (`__vrbPlay`) from a non-active tab: run `PAUSE_JS` there and ignore (log "background window").
- Watchdog: every 1 s `Target.getTargets` once, update url/title of every tab; send `tabs` when anything in
  the list changed; `info` as v1 for the active tab.
- Brave restart (watchdog): all tabs are lost; reopen one tab at the active tab's last URL.

### F5. Tab messages are handled in order
`handle()` awaits `tab` actions inline (no `create_task`), so a `{"t":"nav","to":"url"}` sent right after
`{"t":"tab","action":"new"}` goes to the new tab.

### F6. typing_window.py
Unchanged behaviour, plus a **New window** button (calls `vb.new_tab()`), and the status line shows
`window i/n`. Text and Go act on the active tab.

---------------------------------------------------------------------------------------------------

## G. Protocol (complete; v1 messages not listed here are unchanged)

All WebSocket messages are JSON text frames ≤ 10000 bytes from the phone (the server drops larger ones),
plus binary JPEG screencast frames from the server (active tab only).

### G1. Server → phone
```
{"t":"info","url","title","w":1280,"h":720,"loading":bool,"canBack":bool,
 "tab":"<active id>","focus":null | {"input":"search","enter":"search"}}

{"t":"tabs","active":"<id>","max":6,
 "list":[{"id":"1","title":"YouTube","url":"https://www.youtube.com/","loading":false}, ...]}
     sent on connect, on every add/remove/switch, and when a title/url/loading changes

{"t":"kbd","open":true,"mode":"field","input":"text|search|email|url|password|tel|number|textarea|contenteditable",
 "enter":"search|go|send|next|done|enter|"}
{"t":"kbd","open":false}

{"t":"pad","s":"pad,<bits>,lx,ly,rx,ry,lt,rt" | "pad,none"}           (unchanged)
{"t":"toast","text"}                                                    (unchanged)

{"t":"video","state":"resolving","title"}
{"t":"video","state":"progress","text", "pct"?: number}                 (no pct = no bar)
{"t":"video","state":"ready",
   "mode":"mse"|"split"|"file",
   "vid":"youtube_<id>" | "<cache key>" | null,        // key for {"t":"proj"} reports; null = not saved
   "title", "start": seconds, "duration": seconds | null,
   "src": "<= v.src, kept for v1 compatibility>",
   "v": {"src":"/media/<mid>/v?k=<key>" (file mode: "/media/<mid>/f?k=<key>"),
         "index":"/media/<mid>/v/index?k=<key>" | null,     // null in file/split-only modes
         "mime":"video/webm; codecs=\"vp09.00.51.08\"", "codec":"vp9|avc1|av1|unknown",
         "w":3840, "h":2160, "fps":30, "itag":"313" | null},
   "a": null | {"src":"/media/<mid>/a?k=<key>","index":"/media/<mid>/a/index?k=<key>",
                "mime":"audio/webm; codecs=\"opus\"","codec":"opus|aac"},
   "w", "h",                                           // = v.w, v.h (full frame)
   "projection": one of C1 ids, "stereo":"mono|sbs|tb",
   "projection_source":"user|image|container|note|title|guess",
   "mesh": "/media/<mid>/mesh?k=<key>" | null, "mesh_label":"VR180" | "360 (EAC)" | null,
   "image_test": bool,
   "format":"313 3840x2160 vp9 + 251 opus (mse)"}       // for the HUD/debug only
{"t":"video","state":"screen","title"}  {"t":"video","state":"error","text"}  {"t":"video","state":"exit"}
```
`mime` is built from yt-dlp's `ext` and `vcodec`/`acodec` strings:
`video/<ext>; codecs="<vcodec>"`, `audio/<mp4|webm>; codecs="<acodec>"` (ext `m4a` → `audio/mp4`).

### G2. Phone → server
```
{"t":"hello","v":2,"w","h","dpr","eye":[ew,eh],"stereo":bool,"fov":deg,"ppd":number,
 "ua":"<navigator.userAgent, 200 chars>"}                               on open and after a resize
{"t":"caps","mse":"mms"|"mse"|"none","mtex":<MAX_TEXTURE_SIZE>,"vtex":<max video width, optional>,
 "v":{"avc1":{"mse":[s30,s60,pe30,pe60],"file":[...]},"vp9":{...},"vp9mp4":{...},"av1":{...}},
 "a":{"aac":bool,"opus":bool}}                                          once, after the probe (G6)
{"t":"ptr","x","y"} {"t":"click","x","y","button"} {"t":"scroll","x","y","dy"}   (unchanged)
{"t":"key","key":"Enter|Backspace|Tab|Escape|Delete|ArrowUp|ArrowDown|ArrowLeft|ArrowRight|Home|End|PageUp|PageDown|Space|f|k|m"}
{"t":"text","s":"<1..2000 chars>"}            -> Input.insertText on the active tab; sets last_click
{"t":"nav","to":"back"|"forward"|"reload"|"home"}                      (unchanged)
{"t":"nav","to":"url","url":"<typed text, ≤ 4000 chars>"}              -> normalize_url, active tab
{"t":"tab","action":"new"}                    -> new tab at about:blank, activated (refused at 6: toast)
{"t":"tab","action":"switch","id":"<id>"}
{"t":"tab","action":"close","id":"<id>"}      (id omitted = active tab)
{"t":"vr","action":"enter"|"exit","time"?}                             (unchanged)
{"t":"proj","vid":"<ready.vid>","projection":"<C1 id>","source":"user"|"image","scores"?:{...}}
                                              -> saved in cache/<vid>.json (G5); ignored when vid is null
                                                 or not a key the server issued in this run
{"t":"vfail","mode":"mse"|"split","reason":"<≤200 chars>","time"?:s}  -> log; reason "lag" (split: 3 hard seeks with
                                                the video behind; mse: >15 % dropped frames in two 5 s windows)
                                                -> a lower rung in the same mode; other mse reasons = switched to split
```
Server validation: unknown `t` ignored; strings are type-checked and length-limited; `text.s` has control
characters removed except `\n` and `\t`; `nav.url` goes through `normalize_url` (it already handles schemes).
Every `key`, `text`, `click` also updates `last_click` (used by F3 and D6).

### G3. Index JSON (`GET /media/{mid}/{v|a}/index?k=`)
```
{"init":[0, 1234], "segs":[[startByte, endByte, t, d], ...], "size": 123456789,
 "dur": 635.0, "mime":"video/mp4; codecs=\"avc1.64002A\""}
```
404 when the file has no usable index (the phone then uses `split`).

### G4. Mesh JSON (`GET /media/{mid}/mesh?k=`)
```
{"label":"VR180", "meshes":[{"pos":[x,y,z, ...], "uv":[u,v, ...], "idx":[...], "prim":"strip"|"triangles"|"fan"}, ...]}
```
Positions as decoded (forward −Z, up +Y); `uv` as decoded (v from the bottom); `idx` expanded to plain
vertex indices. Multiple vertex lists of one mesh become separate entries with the same eye.
Each mesh entry also has `"eye": 0 | 1 | null` (`null` = both eyes).

### G5. `cache/<key>.json` v2
```
{"v":2, "title", "w", "h", "projection", "stereo":"mono|sbs|tb", "projection_source",
 "mesh": bool, "user_projection": null | "<id>", "image_scores": null | {...}, "updated": <unix time>}
```
A v1 file (no `"v"`) is read only for A3.2 (`projection == "flat"` and an mp4 next to it); the next detection
rewrites it as v2 (keep `w`,`h`,`title`).

### G6. Phone capability probe (PAGE)
Runs once after load, before the first `caps` send (≈ 100 ms; do not block rendering).
- Heights `[720, 1080, 1440, 2160, 2880, 4320]`, width `round(h * 16 / 9)`, framerates 30 and 60,
  bitrates 720: 2.5e6, 1080: 5e6, 1440: 10e6, 2160: 20e6, 2880: 30e6, 4320: 50e6.
- Codec strings: `avc1`: `video/mp4; codecs="avc1.640028"` (≤1080) / `"avc1.640033"` (higher);
  `vp9`: `video/webm; codecs="vp09.00.51.08"`; `vp9mp4`: `video/mp4; codecs="vp09.00.51.08"`;
  `av1`: `video/mp4; codecs="av01.0.08M.08"` (≤1080) / `"av01.0.12M.08"` (≤2160) / `"av01.0.16M.08"` (higher).
- `navigator.mediaCapabilities.decodingInfo({type:"media-source"|"file", video:{contentType,width,height,bitrate,framerate}})`.
  `s30/s60` = max height with `supported && smooth`, `pe30/pe60` = max height also `powerEfficient`.
  A rejected/throwing query counts as unsupported. Do **not** use `canPlayType` as proof (Edge says
  "probably" for webm VP9 and then fails).
- No MediaCapabilities, or `media-source` queries throw on this browser: for each codec where
  `MS.isTypeSupported(type)` (or `video.canPlayType(type) === "probably"` for `file`) is true, use
  `[1080, 1080, 0, 0]`.
- `mse`: `"mms"` if `window.ManagedMediaSource`, else `"mse"` if `window.MediaSource` and
  `MediaSource.isTypeSupported('video/mp4; codecs="avc1.640028"')`, else `"none"`.
- `a.aac` = `audio/mp4; codecs="mp4a.40.2"`, `a.opus` = `audio/webm; codecs="opus"`, both via
  `MS.isTypeSupported` (mse) or `canPlayType` (none).
- `mtex = gl.getParameter(gl.MAX_TEXTURE_SIZE)`.
- Resend `caps` after every WebSocket reconnect (keep the result in memory).

---------------------------------------------------------------------------------------------------

## E. Builders: who owns what, and what to deliver

### SERVER builder: `server.py`, `video_resolve.py`, `media_probe.py` (new), `typing_window.py`, `README.md`
1. `video_resolve.py`: `Resolver.lookup` (A4, memory cache, prefetch support), `pick_streams` (A5),
   `pick_progressive` generalised to the phone's `file` caps (any codec it plays; still muxed https only),
   `detect_projection` (C2, fixed `note_projection` and `guess_projection`, flat_sbs/flat_tb),
   `PROJECTIONS` = the C1 ids, cache json v2 read/write (G5). Keep `_download` as the last resort.
   Remove the download from the normal path.
2. `media_probe.py`: sidx/Cues index, st3d/sv3d/equi bounds/cbmp, mesh decoder (A9). Unit-test it on the
   saved headers in `scratchpad/stereo/dl/*.head` (expected results are in the stereo table in the
   investigation: e.g. G6-hgdatqFc → equirect_tb, ZvZ7da8JBUk → mesh mono 360, 4aaWyFKpfDw → mesh sbs 2 meshes
   VR180, Z4Pru1uKjt0 → flat_tb).
3. `server.py`: tabs (F), focus binding + `kbd` (D6), new messages (G2), caps per client, `ready` v2 (G1),
   media routes + chunked proxy + expiry refresh (A8), prefetch hook on navigation (A4), hello logging (B7).
4. `typing_window.py`: New window button (F6).
5. `README.md`: new controls table (D7), "videos stream now", the windows/keyboard section, troubleshooting:
   "Pad: none" badge meaning, "video stutters on the A08 → it picks a lower quality by itself", Y for 3D,
   delete old `cache\*.mp4` to free space. Keep it short and in the v1 style.
Acceptance (headless; server on `--no-keyboard --port 8453`, phone = headless Edge as in the brief):
- BBB `aqz-KE-bpKQ`: "ready" arrives with `mode:"mse"`, no file appears in `cache/` (except the json), the
  index endpoint returns ≥ 100 segments, first video frame on the phone < 3 s after the play event once the
  lookup is cached; seeking to 500 s plays within 2 s.
- 360 `ZZyBG6UsvoQ` with Edge caps → a VP9/AV1 2160 format if decodingInfo says smooth, `projection:"equirect"`,
  `projection_source:"container"`.
- Popup test: a local page with `<a target=_blank>` clicked via the phone → a second tab in `tabs`, screencast
  switches; `window.open` without a click → closed. 7th window refused with the toast.
- Focus: clicking the YouTube search box → `kbd open field search`; clicking elsewhere → `kbd open:false`.
- Media URLs without `k` → 403; WebSocket from a foreign Origin → refused (v1 checks still pass).

### PAGE builder: `web/index.html`
1. Streaming playback: caps probe + `caps` message (G6), MSE manager (A6), split mode with the audio element
   (A7), file mode (v1), `vfail` fallbacks.
2. Render: B1–B7, `?sharp=0`, `?dpr=`, `?demo=flat1080`, and the B8 proof screenshots.
3. Projections: C1 table, Y cycle, `force2D`, mesh fetch/render (B6), EAC (B5), image test (C3), `proj` reports.
4. UI: input layer with LT/RT bits and holds (D1), pad badge (D2), player bar (D3), keyboard (D4), chrome tab
   strip + address bar + window controls (D5), A closes the lens panel, the start-screen help text updated to
   the D7 table (short).
5. Test hooks: `window.__vrbTestPad` accepts `lt`, `rt`; `?demo=` gets `flat_sbs`, `flat_tb`, `half_tb`,
   `eac`, `flat1080`, and `?demo=kbd` (keyboard open over the demo page) and `?demo=bar` (player bar visible).
Acceptance (headless Edge, `?demo=` without a server, then against the SERVER build):
- Screenshots: `demo=flat1080` old vs new (B8); `demo=flat_sbs` shows one un-split picture per eye;
  `demo=kbd`, `demo=bar`, the tab strip with 3 tabs.
- With `__vrbTestPad`: L3 opens/closes the keyboard in browser and VR mode; typing "abc" + Start into the
  YouTube search box via the controller only performs the search; R3 opens a new window with the keyboard up,
  typing `example.com` + Start loads it; RT/LT switch windows; R3 held 1 s closes it; A on a bar button works.

### Shared rules
- Do not change any message from section G without changing this document first.
- Stop every process you start, by PID; never kill the user's own server on port 8443.
- No firewall changes. No package installs.

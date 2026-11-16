extends Node3D
## Video screen station (cinema). Attach to a Station_Screen_* Node3D that has a "Surface" child
## (QuadMesh or thin box, local +Z faces the viewer).
##
## Frames come from the media service (home_space/service/video.py) over a WebSocket as raw RGB8;
## the sound plays on the laptop (earbuds). Nothing opens a window on the laptop.
##
## In the headset:  look at the screen, [E / A] -> menu of videos
##   menu:     Up/Down (keys, d-pad, left stick, wheel) choose · Enter / E / A / click play · Esc / B leave
##   playing:  Space / A / click pause-play · Left/Right -10/+10 s · Up/Down volume
##             M / Backspace / X stop and back to the menu · Esc / B leave (the video keeps playing)

@export var service_url := "ws://127.0.0.1:8090"   ## media service; http URL is derived from it
@export var max_w := 1280                           ## decode width cap (keep small screens small)
@export var menu_rows := 5                          ## visible rows in the menu
@export var status_seconds := 3.0                   ## how long the status line stays after an action
@export var use_tcp_frames := true                  ## frames over the service's TCP side channel (much
													## cheaper for Godot than big WebSocket messages)

const STICK := 0.55
const CONNECT_TIMEOUT := 3.0      ## s; the service is on localhost, so a slow connect means it is not running
const MENU_TEXT := Color(0.92, 0.94, 1.0)
const MENU_DIM := Color(0.62, 0.66, 0.78)
const ACCENT := Color(1.0, 0.78, 0.35)

var videos: Array = []            ## [{name, path, size, duration}] from /api/videos
var frames_received := 0          ## frames uploaded to the texture (tests read this)
var packets_received := 0         ## binary frames received (>= frames_received; extra ones were skipped)
var state := "stopped"            ## last state from the service: playing|paused|stopped|ended|error
var status: Dictionary = {}

var _surface: MeshInstance3D
var _size := Vector2(2, 1.125)
var _orig_scale := Vector3.ONE
var _video_mat: StandardMaterial3D
var _img: Image
var _tex: ImageTexture
var _ws: WebSocketPeer
var _ws_queue: Array[String] = []
var _ws_wait := 0.0               ## seconds the WebSocket has been connecting
var _list_pending := false        ## /api/videos request in flight
var _list_wait := 0.0             ## seconds it has been connecting
var _http: HTTPRequest
var _player: Node
var _menu_open := false
var _active := false              ## a video is loaded (playing, paused or ended-but-shown)
var _sel := 0
var _scroll := 0
var _list_msg := ""
var _status_timer := 0.0
var _hint_timer := 0.0
var _stick_timer := 0.0
var _stick_dir := Vector2i.ZERO
var _fps_frames := 0
var _fps_time := 0.0
var _fps := 0.0
var _volume := 1.0
var _pending_w := 0
var _pending_h := 0
# TCP frame channel: a worker thread reads frames; the main thread only uploads the newest one
var _tcp_thread: Thread
var _tcp_port := 0
var _tcp_started_ms := 0
var _tcp_quit := false
var _tcp_alive := false
var _tcp_mutex := Mutex.new()
var _tcp_frame := PackedByteArray()
var _tcp_w := 0
var _tcp_h := 0

# UI nodes
var _ui: Node3D
var _backdrop: MeshInstance3D
var _title: Label3D
var _rows: Array[Label3D] = []
var _row_times: Array[Label3D] = []
var _highlight: MeshInstance3D
var _hint: Label3D
var _status_bar: MeshInstance3D
var _status_label: Label3D
var _row_h := 0.3
var _fit_cache := {}


func _ready() -> void:
	_surface = get_node_or_null("Surface")
	if not _surface:
		push_warning("video_screen: %s has no Surface child" % name)
		return
	var m := _surface.mesh
	if m is QuadMesh:
		_size = (m as QuadMesh).size
	elif m is PlaneMesh:
		_size = (m as PlaneMesh).size
	elif m is BoxMesh:
		_size = Vector2((m as BoxMesh).size.x, (m as BoxMesh).size.y)
	_orig_scale = _surface.scale
	_video_mat = StandardMaterial3D.new()
	_video_mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	_video_mat.texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR
	_video_mat.albedo_color = Color.BLACK
	_ensure_collision()
	_build_ui()
	_http = HTTPRequest.new()
	_http.timeout = 30.0
	add_child(_http)
	_http.request_completed.connect(_on_list)
	set_process(true)


func _exit_tree() -> void:
	if _ws:
		if _ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
			_ws.send_text(JSON.stringify({"cmd": "stop"}))
		_ws.close()
		_ws = null
	_close_tcp()


# ------------------------------------------------------------ player interface

func prompt(_p: Node) -> String:
	return "Video controls" if _active else "Watch videos"


func interact(player: Node) -> String:
	_player = player
	player.focus(self)
	if _active:
		_show_status()
		_show_hint("Space/A pause   Left/Right 10 s   Up/Down volume   M/X menu   Esc/B leave", 5.0, true)
	else:
		open_menu()
	return ""


func on_unfocus() -> void:
	_player = null
	_hint_timer = 0.0
	_hint.visible = false
	if _menu_open:
		_close_menu()
		if not _active:
			_set_idle_look()


func focused_input(event: InputEvent) -> void:
	var a := _action_of(event)
	if a == "":
		return
	if _menu_open:
		match a:
			"up": _move_sel(-1)
			"down": _move_sel(1)
			"ok", "toggle":
				if _sel < videos.size():
					play_item(_sel)
			"back":
				if _active:
					_close_menu()
	else:
		match a:
			"ok", "toggle":
				_send({"cmd": "toggle"})
				_status_soon()
			"left":
				_send({"cmd": "seek_rel", "dt": -10.0})
				_status_soon()
			"right":
				_send({"cmd": "seek_rel", "dt": 10.0})
				_status_soon()
			"up", "down":
				_volume = clampf(_volume + (0.1 if a == "up" else -0.1), 0.0, 1.0)
				_volume = snappedf(_volume, 0.1)
				_send({"cmd": "volume", "v": _volume})
				_status_soon()
			"back":
				stop_video()
				open_menu()


func focused_process(delta: float) -> void:
	# left stick with key-like repeat (d-pad and keys arrive as events)
	var v := Vector2(Input.get_joy_axis(0, JOY_AXIS_LEFT_X), Input.get_joy_axis(0, JOY_AXIS_LEFT_Y))
	var d := Vector2i.ZERO
	if absf(v.y) > STICK and absf(v.y) >= absf(v.x):
		d.y = signi(int(signf(v.y)))
	elif absf(v.x) > STICK:
		d.x = signi(int(signf(v.x)))
	if d == Vector2i.ZERO:
		_stick_dir = d
		return
	if d != _stick_dir:
		_stick_dir = d
		_stick_timer = 0.45
	else:
		_stick_timer -= delta
		if _stick_timer > 0.0:
			return
		_stick_timer = 0.16 if _menu_open else 0.5
	var ev := InputEventAction.new()
	ev.action = "_video_stick_%s" % ("up" if d.y < 0 else "down" if d.y > 0 else "left" if d.x < 0 else "right")
	ev.pressed = true
	focused_input(ev)


# ------------------------------------------------------------ public (menu + tests)

func open_menu() -> void:
	_menu_open = true
	_set_label_visible(false)
	_ui.visible = true
	_backdrop.visible = true
	_title.visible = true
	_status_bar.visible = false        # it would sit on top of the menu's help line
	_status_label.visible = false
	_status_timer = 0.0
	_list_msg = "Loading..."
	_refresh_menu()
	fetch_list()


func fetch_list() -> void:
	if _http.get_http_client_status() != HTTPClient.STATUS_DISCONNECTED:
		_http.cancel_request()
	var err := _http.request(_http_base() + "/api/videos")
	_list_pending = err == OK
	_list_wait = 0.0
	if err != OK:
		_list_msg = "Media service not reachable"
		_refresh_menu()


func play_item(i: int) -> void:
	if i < 0 or i >= videos.size():
		return
	play_path(String(videos[i].get("path", "")), String(videos[i].get("name", "")))


func play_path(path: String, title := "") -> void:
	_close_menu()
	_active = true
	frames_received = 0
	_fps_frames = 0
	_fps_time = 0.0
	status = {"state": "playing", "name": title, "t": 0.0, "duration": 0.0}
	_set_label_visible(false)
	_video_mat.albedo_color = Color.BLACK      # black until the first frame arrives
	_surface.material_override = _video_mat
	_ensure_ws()
	_send({"cmd": "volume", "v": _volume})
	var open := {"cmd": "open", "path": path, "max_w": max_w}
	if use_tcp_frames:
		open["transport"] = "tcp"
	_send(open)
	_show_status()


func stop_video() -> void:
	_send({"cmd": "stop"})
	_active = false
	state = "stopped"
	_set_idle_look()


func measured_fps() -> float:
	return _fps


func is_active() -> bool:
	return _active


# ------------------------------------------------------------ frame loop

func _process(delta: float) -> void:
	if _status_timer > 0.0:
		_status_timer -= delta
		if _status_timer <= 0.0 and state != "paused":
			_status_bar.visible = false
			_status_label.visible = false
	if _hint_timer > 0.0:
		_hint_timer -= delta
		if _hint_timer <= 0.0:
			_hint.visible = false
	if _status_label.visible:
		_status_label.text = _status_text()
	# Godot 4.7 on Windows never reports a refused localhost connect: HTTPRequest, WebSocketPeer and
	# StreamPeerTCP all stay "connecting" forever when the service is not running. Time out ourselves.
	if _list_pending and _http.get_http_client_status() in [HTTPClient.STATUS_RESOLVING, HTTPClient.STATUS_CONNECTING]:
		_list_wait += delta
		if _list_wait > CONNECT_TIMEOUT:
			_http.cancel_request()
			_on_list(HTTPRequest.RESULT_CANT_CONNECT, 0, PackedStringArray(), PackedByteArray())
	if not _ws:
		return
	_ws.poll()
	var st := _ws.get_ready_state()
	if st == WebSocketPeer.STATE_CONNECTING:
		_ws_wait += delta
		if _ws_wait > CONNECT_TIMEOUT:
			_ws.close()
			st = WebSocketPeer.STATE_CLOSED
	if st == WebSocketPeer.STATE_OPEN:
		for s in _ws_queue:
			_ws.send_text(s)
		_ws_queue.clear()
		var frame := PackedByteArray()
		var fw := 0
		var fh := 0
		var n := _ws.get_available_packet_count()
		for i in n:
			var pkt := _ws.get_packet()
			if _ws.was_string_packet():
				_on_status(pkt.get_string_from_utf8())
			else:
				frame = pkt                       # keep only the newest frame
				packets_received += 1
				fw = _pending_w
				fh = _pending_h
		if _tcp_thread:
			_tcp_mutex.lock()
			if not _tcp_frame.is_empty():
				frame = _tcp_frame
				fw = _tcp_w
				fh = _tcp_h
				_tcp_frame = PackedByteArray()
			_tcp_mutex.unlock()
		if not frame.is_empty() and _active:
			_upload(frame, fw, fh)
		_fps_time += delta
		if _fps_time >= 1.0:
			_fps = _fps_frames / _fps_time
			_fps_frames = 0
			_fps_time = 0.0
	elif st == WebSocketPeer.STATE_CLOSED:
		_ws = null
		_ws_queue.clear()
		_close_tcp()
		if _active:
			_active = false
			state = "error"
			status["error"] = "Media service not running"
			_set_idle_look()
			_show_error()


## Worker thread: reads b"VFRM" + w, h, len (uint32 LE) + RGB8 frames; keeps only the newest.
func _tcp_loop(port: int) -> void:
	var tcp := StreamPeerTCP.new()
	if tcp.connect_to_host("127.0.0.1", port) != OK:
		_tcp_alive = false
		return
	var t0 := Time.get_ticks_msec()
	while not _tcp_quit:
		tcp.poll()
		var s := tcp.get_status()
		if s == StreamPeerTCP.STATUS_CONNECTED:
			break
		if s != StreamPeerTCP.STATUS_CONNECTING or Time.get_ticks_msec() - t0 > 3000:
			_tcp_alive = false
			return
		OS.delay_msec(2)
	var hdr := PackedByteArray()
	var buf := PackedByteArray()
	var need := 0
	var w := 0
	var h := 0
	while not _tcp_quit:
		tcp.poll()
		if tcp.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			break
		var avail := tcp.get_available_bytes()
		if avail <= 0:
			OS.delay_usec(1000)
			continue
		if need == 0:                                  # header
			var r: Array = tcp.get_partial_data(16 - hdr.size())
			if r[0] != OK:
				break
			hdr.append_array(r[1])
			if hdr.size() < 16:
				continue
			if hdr.slice(0, 4).get_string_from_ascii() != "VFRM":
				push_warning("video_screen: bad frame header")
				break
			w = hdr.decode_u32(4)
			h = hdr.decode_u32(8)
			need = hdr.decode_u32(12)
			hdr = PackedByteArray()
			buf = PackedByteArray()
			continue
		# ask only for what has arrived: get_partial_data(n) allocates n bytes up front, and asking
		# for the whole 2.7 MB frame on every 64 KB chunk was ~40 big allocations per frame
		var r2: Array = tcp.get_partial_data(mini(need, avail))
		if r2[0] != OK:
			break
		var chunk: PackedByteArray = r2[1]
		buf.append_array(chunk)
		need -= chunk.size()
		if need <= 0:
			need = 0
			_tcp_mutex.lock()
			_tcp_frame = buf                           # an older, unused frame is simply replaced
			_tcp_w = w
			_tcp_h = h
			packets_received += 1
			_tcp_mutex.unlock()
			buf = PackedByteArray()
	tcp.disconnect_from_host()
	_tcp_alive = false


func _start_tcp(port: int) -> void:
	_close_tcp()
	_tcp_port = port
	_tcp_started_ms = Time.get_ticks_msec()
	_tcp_quit = false
	_tcp_alive = true
	_tcp_thread = Thread.new()
	_tcp_thread.start(_tcp_loop.bind(port))


func _close_tcp() -> void:
	if _tcp_thread:
		_tcp_quit = true
		_tcp_thread.wait_to_finish()
		_tcp_thread = null
	_tcp_port = 0
	_tcp_frame = PackedByteArray()


func _upload(data: PackedByteArray, w: int, h: int) -> void:
	if w <= 0 or h <= 0 or data.size() != w * h * 3:
		return
	if _img == null or _img.get_width() != w or _img.get_height() != h:
		_img = Image.create_from_data(w, h, false, Image.FORMAT_RGB8, data)
		_tex = ImageTexture.create_from_image(_img)
		_video_mat.albedo_texture = _tex
		_video_mat.albedo_color = Color.WHITE
		_fit_aspect(float(w) / float(h))
	else:
		_img.set_data(w, h, false, Image.FORMAT_RGB8, data)
		_tex.update(_img)
	if _video_mat.albedo_color != Color.WHITE:
		_video_mat.albedo_color = Color.WHITE
	frames_received += 1
	_fps_frames += 1


func _on_status(text: String) -> void:
	var d = JSON.parse_string(text)
	if not d is Dictionary or d.get("type", "") != "status":
		return
	var prev := state
	status = d
	state = String(d.get("state", "stopped"))
	_pending_w = int(d.get("w", 0))
	_pending_h = int(d.get("h", 0))
	var port := int(d.get("frame_port", 0))
	# (re)connect on a new port at once; a dead link on the same port at most every 2 s
	# (status arrives 4x a second, and a failing connect must not respawn a thread each time)
	if use_tcp_frames and port > 0 and (_tcp_thread == null or port != _tcp_port
			or (not _tcp_alive and Time.get_ticks_msec() - _tcp_started_ms > 2000)):
		_start_tcp(port)       # if it fails, frames keep coming over the WebSocket
	_volume = float(d.get("volume", _volume))
	if state == prev:
		return
	if state == "paused" and _active:
		_show_status()
	elif state == "ended" and _active:
		_active = false
		_set_idle_look()
		if _player:
			open_menu()
	elif state == "error" and _active:
		_active = false
		_set_idle_look()
		_show_error()


## Error while a video was up: on the screen's status line, or, when the player is using the screen,
## back to the menu with the error as a HUD message (the menu covers the status line).
func _show_error() -> void:
	if _player:
		open_menu()
		if _player.has_method("show_message"):
			_player.show_message(_clip("Video: " + String(status.get("error", "error")), 60), 5.0)
	else:
		_show_status()


# ------------------------------------------------------------ helpers

func _http_base() -> String:
	var u := service_url.strip_edges()
	if u.begins_with("wss://"):
		u = "https://" + u.substr(6)
	elif u.begins_with("ws://"):
		u = "http://" + u.substr(5)
	var slash := u.find("/", u.find("://") + 3)
	return u if slash < 0 else u.substr(0, slash)


func _ensure_ws() -> void:
	if _ws and _ws.get_ready_state() in [WebSocketPeer.STATE_OPEN, WebSocketPeer.STATE_CONNECTING]:
		return
	_ws = WebSocketPeer.new()
	_ws.inbound_buffer_size = 8 * 1024 * 1024      # one 720p RGB frame is 2.7 MB
	_ws.outbound_buffer_size = 64 * 1024
	_ws.max_queued_packets = 64
	_ws_queue.clear()
	_ws_wait = 0.0
	var err := _ws.connect_to_url(service_url.trim_suffix("/") + "/ws/video")
	if err != OK:
		push_warning("video_screen: cannot connect to %s (%d)" % [service_url, err])
		_ws = null


func _send(d: Dictionary) -> void:
	if not _ws:
		if d.get("cmd", "") == "stop":
			return
		_ensure_ws()
		if not _ws:
			return
	var s := JSON.stringify(d)
	if _ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
		_ws.send_text(s)
	else:
		_ws_queue.append(s)


func _on_list(result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	_list_pending = false
	if result != HTTPRequest.RESULT_SUCCESS or code != 200:
		videos = []
		_list_msg = "Media service not running\n(start_home.bat starts it)"
	else:
		var d = JSON.parse_string(body.get_string_from_utf8())
		videos = d.get("videos", []) if d is Dictionary else []
		_list_msg = "" if not videos.is_empty() else "No videos found\nAdd folders to home_space/library.json"
	_sel = clampi(_sel, 0, maxi(0, videos.size() - 1))
	_refresh_menu()


func _action_of(e: InputEvent) -> String:
	if e is InputEventAction:
		return String(e.action).trim_prefix("_video_stick_") if e.pressed else ""
	if e is InputEventKey:
		if not e.pressed:
			return ""
		var k: int = e.physical_keycode if e.physical_keycode else e.keycode
		match k:
			KEY_UP, KEY_W: return "up"
			KEY_DOWN, KEY_S: return "down"
			KEY_LEFT, KEY_A: return "left"
			KEY_RIGHT, KEY_D: return "right"
			KEY_ENTER, KEY_KP_ENTER, KEY_E: return "ok" if not e.echo else ""
			KEY_SPACE: return "toggle" if not e.echo else ""
			KEY_M, KEY_BACKSPACE: return "back" if not e.echo else ""
		return ""
	if e is InputEventJoypadButton:
		if not e.pressed:
			return ""
		match e.button_index:
			JOY_BUTTON_DPAD_UP: return "up"
			JOY_BUTTON_DPAD_DOWN: return "down"
			JOY_BUTTON_DPAD_LEFT: return "left"
			JOY_BUTTON_DPAD_RIGHT: return "right"
			JOY_BUTTON_A: return "ok"
			JOY_BUTTON_X: return "back"
		return ""
	if e is InputEventMouseButton:
		if not e.pressed:
			return ""
		match e.button_index:
			MOUSE_BUTTON_LEFT: return "ok"
			MOUSE_BUTTON_WHEEL_UP: return "up"
			MOUSE_BUTTON_WHEEL_DOWN: return "down"
	return ""


func _move_sel(d: int) -> void:
	if videos.is_empty():
		return
	_sel = wrapi(_sel + d, 0, videos.size())
	_refresh_menu()


func _close_menu() -> void:
	_menu_open = false
	_backdrop.visible = false
	_title.visible = false
	_highlight.visible = false
	_hint.visible = false
	_hint_timer = 0.0
	for l in _rows:
		l.visible = false
	for l in _row_times:
		l.visible = false


func _set_idle_look() -> void:
	_surface.material_override = null
	_surface.scale = _orig_scale
	if not _menu_open:
		_set_label_visible(true)
	_status_bar.visible = false
	_status_label.visible = false


func _set_label_visible(v: bool) -> void:
	for n in ["Placeholder", "Label"]:
		var l := get_node_or_null(n)
		if l:
			l.visible = v


func _fit_aspect(aspect: float) -> void:
	var screen := _size.x / _size.y
	var s := _orig_scale
	if aspect > screen + 0.01:
		s.y *= screen / aspect     # wider than the screen: letterbox
	elif aspect < screen - 0.01:
		s.x *= aspect / screen     # narrower: pillarbox
	_surface.scale = s


func _status_soon() -> void:
	_show_status()


func _show_status() -> void:
	_status_timer = status_seconds
	_status_bar.visible = true
	_status_label.visible = true
	_status_label.text = _status_text()


func _show_hint(text: String, seconds: float, above_status := false) -> void:
	var h := _size.y
	_hint.position.y = -h * 0.5 + _row_h * (1.85 if above_status else 0.6)   # 2 lines clear the status bar
	_hint.text = text
	_hint.visible = true
	_hint_timer = seconds


func _status_text() -> String:
	if state == "error" and String(status.get("error", "")) != "":
		return _fit_line(_status_label, "Error: " + String(status.get("error", "")), "")
	var word := "PAUSED" if state == "paused" else "PLAYING" if state == "playing" else state.to_upper()
	# long names are clipped so the line fits the screen (measured once per name, worst-case rest)
	var n := _fit_line(_status_label, String(status.get("name", "")), "PLAYING      0:00:00 / 0:00:00   vol 100%")
	return "%s   %s   %s / %s   vol %d%%" % [word, n,
			_fmt_time(float(status.get("t", 0.0))), _fmt_time(float(status.get("duration", 0.0))),
			roundi(_volume * 100.0)]


## `s` clipped with "..." so that s + extra fits one line of label `l` across the screen.
## Cached: the status line is rebuilt every frame while it is visible.
func _fit_line(l: Label3D, s: String, extra: String) -> String:
	var key := s + "|" + extra
	if _fit_cache.has(key):
		return _fit_cache[key]
	var f: Font = l.font if l.font else ThemeDB.fallback_font
	var room := _size.x * 0.94 / l.pixel_size
	var out := s
	var n := s.length()
	while n > 1 and f.get_string_size(out + extra, HORIZONTAL_ALIGNMENT_LEFT, -1, l.font_size).x > room:
		n -= 1
		out = _clip(s, n)
	if _fit_cache.size() > 64:
		_fit_cache.clear()
	_fit_cache[key] = out
	return out


func _refresh_menu() -> void:
	if not _menu_open:
		return
	_title.text = "VIDEOS" if videos.is_empty() else "VIDEOS   %d / %d" % [_sel + 1, videos.size()]
	if _sel < _scroll:
		_scroll = _sel
	elif _sel >= _scroll + menu_rows:
		_scroll = _sel - menu_rows + 1
	_scroll = clampi(_scroll, 0, maxi(0, videos.size() - menu_rows))
	var max_chars := int(_size.x * 0.72 / (_row_h * 0.7 * 0.55))
	for r in menu_rows:
		var i := _scroll + r
		var l := _rows[r]
		var tl := _row_times[r]
		if videos.is_empty():
			l.visible = r == 0
			tl.visible = false
			l.text = _list_msg
			l.modulate = MENU_DIM
			continue
		l.visible = i < videos.size()
		tl.visible = l.visible
		if not l.visible:
			continue
		var v: Dictionary = videos[i]
		l.text = _clip(String(v.get("name", "?")), max_chars)
		tl.text = _fmt_time(float(v.get("duration", 0.0)))
		var on := i == _sel
		l.modulate = Color(0.08, 0.06, 0.04) if on else MENU_TEXT
		tl.modulate = Color(0.08, 0.06, 0.04) if on else MENU_DIM
		l.outline_modulate = Color(0, 0, 0, 0.0 if on else 0.8)
		tl.outline_modulate = l.outline_modulate
		if on:
			_highlight.visible = true
			_highlight.position.y = l.position.y
	if videos.is_empty():
		_highlight.visible = false
	_show_hint("Up/Down choose    Enter / A play    Esc / B leave", 1e9)


static func _clip(s: String, n: int) -> String:
	return s if s.length() <= n else s.substr(0, maxi(1, n - 3)) + "..."


static func _fmt_time(t: float) -> String:
	var s := maxi(0, int(t))
	if s >= 3600:
		return "%d:%02d:%02d" % [s / 3600, (s / 60) % 60, s % 60]
	return "%d:%02d" % [s / 60, s % 60]


# ------------------------------------------------------------ construction

func _ensure_collision() -> void:
	for c in get_children():
		if c is CollisionObject3D:
			return
	var body := StaticBody3D.new()
	body.name = "ScreenBody"
	var cs := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = Vector3(_size.x, _size.y, 0.08)
	cs.shape = box
	body.add_child(cs)
	body.transform = _surface.transform
	add_child(body)


func _flat_mat(c: Color, prio: int) -> StandardMaterial3D:
	var m := StandardMaterial3D.new()
	m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	m.albedo_color = c
	if c.a < 1.0:
		m.transparency = BaseMaterial3D.TRANSPARENCY_ALPHA
	m.render_priority = prio
	return m


func _quad(size: Vector2, c: Color, prio: int, z: float) -> MeshInstance3D:
	var q := QuadMesh.new()
	q.size = size
	var mi := MeshInstance3D.new()
	mi.mesh = q
	mi.material_override = _flat_mat(c, prio)
	mi.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
	mi.position.z = z
	_ui.add_child(mi)
	return mi


func _label(font_px: int, height_m: float, align: HorizontalAlignment, pos: Vector3) -> Label3D:
	var l := Label3D.new()
	l.font_size = font_px
	l.pixel_size = height_m / float(font_px)
	l.outline_size = maxi(4, font_px / 8)
	l.outline_modulate = Color(0, 0, 0, 0.8)
	l.horizontal_alignment = align
	l.double_sided = false
	l.no_depth_test = false
	l.render_priority = 4
	l.outline_render_priority = 3
	l.position = pos
	l.visible = false
	_ui.add_child(l)
	return l


func _build_ui() -> void:
	_ui = Node3D.new()
	_ui.name = "VideoUI"
	_ui.transform = _surface.transform
	add_child(_ui)
	var w := _size.x
	var h := _size.y
	_row_h = h / (menu_rows + 4.2)
	var fh := _row_h * 0.7             # glyph line height in metres
	var left := -w * 0.5 + w * 0.06
	var right := w * 0.5 - w * 0.06
	_backdrop = _quad(Vector2(w, h), Color(0.02, 0.025, 0.04, 0.88), 1, 0.012)
	_backdrop.visible = false
	_highlight = _quad(Vector2(w * 0.92, _row_h * 0.9), ACCENT, 2, 0.02)
	_highlight.visible = false
	_title = _label(96, _row_h * 0.8, HORIZONTAL_ALIGNMENT_LEFT, Vector3(left, h * 0.5 - _row_h * 0.85, 0.03))
	_title.modulate = ACCENT
	for r in menu_rows:
		var y := h * 0.5 - _row_h * (2.25 + r)
		_rows.append(_label(80, fh, HORIZONTAL_ALIGNMENT_LEFT, Vector3(left + w * 0.01, y, 0.03)))
		_row_times.append(_label(80, fh, HORIZONTAL_ALIGNMENT_RIGHT, Vector3(right - w * 0.01, y, 0.03)))
	_hint = _label(64, _row_h * 0.5, HORIZONTAL_ALIGNMENT_CENTER, Vector3(0, -h * 0.5 + _row_h * 0.6, 0.03))
	_hint.modulate = MENU_DIM
	_hint.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART   # the controls help is wider than the screen
	_hint.width = w * 0.94 / _hint.pixel_size
	_status_bar = _quad(Vector2(w, _row_h * 1.1), Color(0, 0, 0, 0.8), 1, 0.014)
	_status_bar.position.y = -h * 0.5 + _row_h * 0.55
	_status_bar.visible = false
	_status_label = _label(80, _row_h * 0.6, HORIZONTAL_ALIGNMENT_CENTER,
			Vector3(0, -h * 0.5 + _row_h * 0.55, 0.035))
	_status_label.modulate = MENU_TEXT

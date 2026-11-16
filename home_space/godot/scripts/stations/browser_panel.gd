extends Node3D
## Web browser terminal (Phase 4). Attach to a Station_Browser_N node.
## The page is rendered by a headless Edge in the media service (home_space/service/browser.py)
## and arrives over WS /ws/browser as JPEG frames, drawn on the station's "Surface" mesh.
##
## Use it: look at the screen and press E / A. While using it (Esc / B leaves):
##   mouse / right stick      move the cursor on the page (the view does not turn)
##   left click / A           click            right click     right click
##   wheel / left stick Y     scroll           keyboard        types into the page
##   Ctrl+L / Y               address bar (type a URL or search words, Enter goes)
##   Alt+Left / LB  back      Alt+Right / RB  forward          F5  reload     Home key on the bar: start page
##   Ctrl+G / F2              gaze cursor on/off (the cursor follows where you look)
## Nobody near (> idle_distance) and not in use -> the screencast stops to save CPU.

@export var service_url := "ws://127.0.0.1:8090"
@export var page_w := 1280                 ## page image size in pixels (16:9 matches the Surface)
@export var page_h := 720
@export var zoom := 1.25                   ## page zoom so text is big in the headset
@export var start_url := ""                ## empty = the service's start page
@export var max_upload_fps := 30.0         ## texture uploads per second at most
@export var mipmaps := true                ## smoother text at a distance, costs a little CPU per frame
@export var cursor_speed := 1.6            ## page pixels per mouse pixel
@export var stick_cursor_speed := 900.0    ## page pixels per second at full right stick
@export var stick_scroll_speed := 1400.0   ## page pixels per second at full left stick
@export var idle_distance := 4.0
@export var gaze_cursor := false

var frames_received := 0
var frames_shown := 0
var page_title := ""
var page_url := ""
var loading := false

var _ws: WebSocketPeer
var _outbox: Array[String] = []
var _opened := false              # a page was requested on this connection
var _active_sent := false
var _got_packet := false         # anything arrived on this connection (else: service not running)
var _connect_msec := 0
const CONNECT_TIMEOUT_MS := 3000
var _last_url := ""
var _player: Node
var _focused := false

var _surface: MeshInstance3D
var _surf_size := Vector2(1.8, 1.0)
var _surf_z := 0.0
var _mat: StandardMaterial3D
var _img := Image.new()
var _tex: ImageTexture
var _jpeg := PackedByteArray()
var _have_jpeg := false
var _upload_timer := 0.0
var _decode_task := -1
var _decode_buf := PackedByteArray()
var _decode_ok := false
var decode_ms := 0.0              ## smoothed worker-thread JPEG decode time
var upload_ms := 0.0              ## smoothed main-thread texture upload time

var _cursor := Vector2.ZERO
var _cursor_node: Node3D
var _move_dirty := false
var _mouse_down := {}             # "left"/"right" -> true while held on the page
var _scroll_accum := 0.0
var _scroll_timer := 0.0

var _title_label: Label3D
var _station_label: Label3D
var _placeholder: Label3D
var _addr_root: Node3D
var _addr_label: Label3D
var _addr_text := ""
var _addr_open := false
var _blink := 0.0

const KEYMAP := {
	KEY_ENTER: "Enter", KEY_KP_ENTER: "Enter", KEY_BACKSPACE: "Backspace", KEY_TAB: "Tab",
	KEY_DELETE: "Delete", KEY_INSERT: "Insert", KEY_UP: "ArrowUp", KEY_DOWN: "ArrowDown",
	KEY_LEFT: "ArrowLeft", KEY_RIGHT: "ArrowRight", KEY_HOME: "Home", KEY_END: "End",
	KEY_PAGEUP: "PageUp", KEY_PAGEDOWN: "PageDown", KEY_SPACE: " ",
}


func _ready() -> void:
	_cursor = Vector2(page_w, page_h) * 0.5
	_surface = get_node_or_null("Surface") as MeshInstance3D
	if _surface == null:
		push_warning("browser_panel: %s has no Surface" % name)
		set_process(false)
		return
	var m := _surface.mesh
	if m is QuadMesh:
		_surf_size = (m as QuadMesh).size
	elif m is PlaneMesh:
		_surf_size = (m as PlaneMesh).size
	elif m is BoxMesh:
		_surf_size = Vector2((m as BoxMesh).size.x, (m as BoxMesh).size.y)
		_surf_z = (m as BoxMesh).size.z * 0.5
	_mat = StandardMaterial3D.new()
	_mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	_mat.texture_filter = BaseMaterial3D.TEXTURE_FILTER_LINEAR_WITH_MIPMAPS if mipmaps \
			else BaseMaterial3D.TEXTURE_FILTER_LINEAR
	_ensure_collision()
	_placeholder = get_node_or_null("Placeholder") as Label3D
	if _placeholder:
		_placeholder.text = "WEB BROWSER\n[E / A] to use"
	_station_label = get_node_or_null("Label") as Label3D
	_make_title_bar()
	_make_address_bar()
	_make_cursor()


# ---------------------------------------------------------------- player API
func prompt(_p) -> String:
	return "Use browser"


func interact(player) -> void:
	_player = player
	player.focus(self)
	_focused = true
	_cursor_node.visible = true
	_place_cursor()
	if _opened and _ws and _ws.get_ready_state() in [WebSocketPeer.STATE_OPEN, WebSocketPeer.STATE_CONNECTING]:
		_set_active(true)       # still connected: carry on where we left (no reload, keeps scroll/video/forms)
	else:
		open_page(_last_url if _last_url != "" else _home_url())
	if player.has_method("show_message"):
		player.show_message("Mouse: cursor  Click  Wheel: scroll\nCtrl+L / Y: address   Esc / B: leave", 3.5)


func on_unfocus() -> void:
	_focused = false
	_cursor_node.visible = false
	_close_address_bar()
	for b in _mouse_down.keys():
		_send({"cmd": "mouse", "type": "up", "x": _cursor.x, "y": _cursor.y, "button": b})
	_mouse_down.clear()


## Connects if needed, opens url and starts the screencast. Also used by tests.
func open_page(url := "") -> void:
	if url == "":
		url = _home_url()
	_last_url = url
	_connect_if_needed()
	_send({"cmd": "open", "url": url, "w": page_w, "h": page_h, "zoom": zoom})
	_opened = true
	_set_active(true)


func _home_url() -> String:
	if start_url != "":
		return start_url
	return service_url.replace("wss://", "https://").replace("ws://", "http://").trim_suffix("/") + "/start"


# ---------------------------------------------------------------- websocket
func _connect_if_needed() -> void:
	if _ws and _ws.get_ready_state() in [WebSocketPeer.STATE_OPEN, WebSocketPeer.STATE_CONNECTING]:
		return
	_ws = WebSocketPeer.new()
	_ws.inbound_buffer_size = 8 * 1024 * 1024
	_ws.outbound_buffer_size = 256 * 1024
	_outbox.clear()
	_active_sent = false
	_got_packet = false
	_connect_msec = Time.get_ticks_msec()
	var err := _ws.connect_to_url(service_url.trim_suffix("/") + "/ws/browser")
	if err != OK:
		push_warning("browser_panel: connect failed (%s)" % error_string(err))
		_set_title("Media service not running")


func _send(d: Dictionary) -> void:
	var s := JSON.stringify(d)
	if _ws and _ws.get_ready_state() == WebSocketPeer.STATE_OPEN:
		_ws.send_text(s)
	elif _ws and _ws.get_ready_state() == WebSocketPeer.STATE_CONNECTING:
		if d.get("cmd") == "mouse" and d.get("type") == "move":
			return
		_outbox.append(s)


func _set_active(on: bool) -> void:
	if on == _active_sent:
		return
	_active_sent = on
	_send({"cmd": "active", "on": on})


func _process(delta: float) -> void:
	if _ws:
		_ws.poll()
		var st := _ws.get_ready_state()
		if st == WebSocketPeer.STATE_OPEN:
			for s in _outbox:
				_ws.send_text(s)
			_outbox.clear()
			while _ws.get_available_packet_count() > 0:
				var pkt := _ws.get_packet()
				_got_packet = true
				if _ws.was_string_packet():
					_on_text(pkt.get_string_from_utf8())
				else:
					_jpeg = pkt        # newest frame wins; decoded below at most max_upload_fps
					_have_jpeg = true
					frames_received += 1
		elif st == WebSocketPeer.STATE_CONNECTING and Time.get_ticks_msec() - _connect_msec > CONNECT_TIMEOUT_MS:
			# On Windows a refused localhost connection never leaves CONNECTING: give up ourselves.
			_ws = null
			st = WebSocketPeer.STATE_CLOSED
		if st == WebSocketPeer.STATE_CLOSED:
			if _opened and not _got_packet:
				_set_title("Media service not running - E to retry")
				if _focused and _player and _player.has_method("show_message"):
					_player.show_message("Browser needs the media service\n(start_home.bat)", 4.0)
			elif _opened:
				_set_title("Browser disconnected - press E to retry")
			_ws = null
			_opened = false
			_active_sent = false
	_upload_timer -= delta
	if _decode_task != -1 and WorkerThreadPool.is_task_completed(_decode_task):
		WorkerThreadPool.wait_for_task_completion(_decode_task)
		_decode_task = -1
		if _decode_ok:
			_upload(_img)
	if _have_jpeg and _decode_task == -1 and _upload_timer <= 0.0:
		_upload_timer = 1.0 / maxf(1.0, max_upload_fps)
		_have_jpeg = false
		_decode_buf = _jpeg
		_decode_task = WorkerThreadPool.add_task(_decode_job, false, "browser jpeg")
	if _addr_open:
		_blink += delta
		_update_address_label()
	if not _focused and _ws and _opened:
		_set_active(_player_distance() <= idle_distance)


func _on_text(s: String) -> void:
	var d = JSON.parse_string(s)
	if typeof(d) != TYPE_DICTIONARY:
		return
	if d.get("type") == "info":
		page_url = str(d.get("url", ""))
		page_title = str(d.get("title", ""))
		loading = bool(d.get("loading", false))
		if page_url.begins_with("http"):
			_last_url = page_url
		_set_title(("... " if loading else "") + (page_title if page_title != "" else page_url))
	elif d.get("type") == "error":
		_set_title("Browser error: " + str(d.get("message", "")))


## Worker thread: JPEG -> RGBA8 (+ mipmaps). Converting here keeps the GPU upload on the main
## thread cheap (Vulkan has no RGB8, the renderer would otherwise convert on the main thread).
func _decode_into(img: Image, buf: PackedByteArray) -> bool:
	var t := Time.get_ticks_usec()
	if img.load_jpg_from_buffer(buf) != OK:
		return false
	img.convert(Image.FORMAT_RGBA8)
	if mipmaps:
		img.generate_mipmaps()
	decode_ms = lerpf(decode_ms, (Time.get_ticks_usec() - t) / 1000.0, 0.2)
	return true


func _decode_job() -> void:
	_decode_ok = _decode_into(_img, _decode_buf)


## Main thread: hand the decoded image to the GPU, reusing one ImageTexture.
func _upload(img: Image) -> void:
	var t := Time.get_ticks_usec()
	if _tex == null or _tex.get_width() != img.get_width() or _tex.get_height() != img.get_height() 			or _tex.get_format() != img.get_format() or _tex.has_mipmaps() != img.has_mipmaps():
		_tex = ImageTexture.create_from_image(img)
		_mat.albedo_texture = _tex
	else:
		_tex.update(img)
	if _surface.material_override != _mat:
		_surface.material_override = _mat
		if _placeholder:
			_placeholder.visible = false
	frames_shown += 1
	upload_ms = lerpf(upload_ms, (Time.get_ticks_usec() - t) / 1000.0, 0.2)


## Synchronous decode + upload (tests / tools).
func _show_jpeg(buf: PackedByteArray) -> void:
	var img := Image.new()
	if _decode_into(img, buf):
		_upload(img)


func _exit_tree() -> void:
	if _decode_task != -1:
		WorkerThreadPool.wait_for_task_completion(_decode_task)
		_decode_task = -1


func _player_distance() -> float:
	if _player == null or not is_instance_valid(_player):
		_player = get_tree().current_scene.get_node_or_null("Player") if get_tree().current_scene else null
		if _player == null:
			return 0.0
	return (_player as Node3D).global_position.distance_to(_surface.global_position)


# ---------------------------------------------------------------- focused input
func focused_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion:
		if not gaze_cursor:
			_move_cursor(event.relative * cursor_speed)
	elif event is InputEventMouseButton:
		var mb := event as InputEventMouseButton
		match mb.button_index:
			MOUSE_BUTTON_LEFT:
				_click("left", mb.pressed)
			MOUSE_BUTTON_RIGHT:
				_click("right", mb.pressed)
			MOUSE_BUTTON_WHEEL_UP, MOUSE_BUTTON_WHEEL_DOWN:
				if mb.pressed:
					var f := mb.factor if mb.factor > 0.0 else 1.0
					_wheel(0.0, (-120.0 if mb.button_index == MOUSE_BUTTON_WHEEL_UP else 120.0) * f)
			MOUSE_BUTTON_WHEEL_LEFT, MOUSE_BUTTON_WHEEL_RIGHT:
				if mb.pressed:
					_wheel(-120.0 if mb.button_index == MOUSE_BUTTON_WHEEL_LEFT else 120.0, 0.0)
	elif event is InputEventKey:
		_key(event as InputEventKey)
	elif event is InputEventJoypadButton:
		var jb := event as InputEventJoypadButton
		match jb.button_index:
			JOY_BUTTON_A:
				_click("left", jb.pressed)
			JOY_BUTTON_Y:
				if jb.pressed:
					_toggle_address_bar()
			JOY_BUTTON_LEFT_SHOULDER:
				if jb.pressed:
					_send({"cmd": "nav", "action": "back"})
			JOY_BUTTON_RIGHT_SHOULDER:
				if jb.pressed:
					_send({"cmd": "nav", "action": "forward"})
			JOY_BUTTON_X:
				if jb.pressed:
					_click("right", true)
					_click("right", false)


func focused_process(delta: float) -> void:
	# right stick -> cursor, left stick Y -> scroll (deadzone, squared response for precision)
	var rs := Vector2(Input.get_joy_axis(0, JOY_AXIS_RIGHT_X), Input.get_joy_axis(0, JOY_AXIS_RIGHT_Y))
	if rs.length() > 0.15:
		_move_cursor(rs * rs.length() * stick_cursor_speed * delta)
	var ly := Input.get_joy_axis(0, JOY_AXIS_LEFT_Y)
	if absf(ly) > 0.2:
		_scroll_accum += ly * absf(ly) * stick_scroll_speed * delta
	_scroll_timer -= delta
	if _scroll_timer <= 0.0 and absf(_scroll_accum) >= 1.0:
		_scroll_timer = 1.0 / 15.0
		_wheel(0.0, _scroll_accum)
		_scroll_accum = 0.0
	if gaze_cursor:
		_gaze()
	if _move_dirty:
		_move_dirty = false
		_send({"cmd": "mouse", "type": "move", "x": roundi(_cursor.x), "y": roundi(_cursor.y)})


func _move_cursor(d: Vector2) -> void:
	_cursor = (_cursor + d).clamp(Vector2.ZERO, Vector2(page_w - 1, page_h - 1))
	_move_dirty = true
	_place_cursor()


func _click(button: String, pressed: bool) -> void:
	if pressed:
		_mouse_down[button] = true
	elif not _mouse_down.has(button):
		return                               # release of the click that started using the panel
	else:
		_mouse_down.erase(button)
	if _addr_open and pressed:
		_close_address_bar()
	_send({"cmd": "mouse", "type": "down" if pressed else "up", "x": roundi(_cursor.x),
		"y": roundi(_cursor.y), "button": button})


func _wheel(dx: float, dy: float) -> void:
	_send({"cmd": "mouse", "type": "wheel", "x": roundi(_cursor.x), "y": roundi(_cursor.y), "dx": dx, "dy": dy})


func _key(k: InputEventKey) -> void:
	var code := k.keycode
	if k.pressed and not k.echo:
		if code == KEY_L and k.ctrl_pressed:
			_toggle_address_bar()
			return
		if k.alt_pressed and code in [KEY_LEFT, KEY_RIGHT]:
			_send({"cmd": "nav", "action": "back" if code == KEY_LEFT else "forward"})
			return
		if code == KEY_F5:
			_send({"cmd": "nav", "action": "reload"})
			return
		if (code == KEY_G and k.ctrl_pressed) or code == KEY_F2:
			# plain G would also be typed into the page, so the toggle is Ctrl+G (or F2)
			gaze_cursor = not gaze_cursor
			if _player and _player.has_method("show_message"):
				_player.show_message("Gaze cursor " + ("ON" if gaze_cursor else "OFF"), 1.5)
			return
	if _addr_open:
		if k.pressed:
			_address_key(k)
		return
	var mods := (1 if k.alt_pressed else 0) | (2 if k.ctrl_pressed else 0) | (4 if k.meta_pressed else 0) \
			| (8 if k.shift_pressed else 0)
	if KEYMAP.has(code):
		_send({"cmd": "key", "key": KEYMAP[code], "down": k.pressed, "mods": mods})
	elif k.ctrl_pressed and k.alt_pressed and k.unicode >= 32:
		if k.pressed:                        # AltGr (= Ctrl+Alt on Windows) characters: @ { } etc.
			_send({"cmd": "text", "text": char(k.unicode)})
	elif k.ctrl_pressed or k.alt_pressed:
		if code >= KEY_A and code <= KEY_Z:
			_send({"cmd": "key", "key": char(code + 32), "down": k.pressed, "mods": mods})
	elif k.pressed and k.unicode >= 32:
		_send({"cmd": "text", "text": char(k.unicode)})


func _gaze() -> void:
	if _player == null or not is_instance_valid(_player):
		return
	var cam: Camera3D = _player.get("cam")
	if cam == null:
		return
	var inv := _surface.global_transform.affine_inverse()
	var o: Vector3 = inv * cam.global_position
	var dir: Vector3 = (inv.basis * -cam.global_basis.z).normalized()
	if absf(dir.z) < 0.0001:
		return
	var t := (_surf_z - o.z) / dir.z
	if t <= 0.0:
		return
	var p := o + dir * t
	var u := p.x / _surf_size.x + 0.5
	var v := 0.5 - p.y / _surf_size.y
	if u < 0.0 or u > 1.0 or v < 0.0 or v > 1.0:
		return
	var c := Vector2(u * page_w, v * page_h)
	if c.distance_to(_cursor) > 0.5:
		_cursor = c
		_move_dirty = true
		_place_cursor()


# ---------------------------------------------------------------- address bar
func _toggle_address_bar() -> void:
	if _addr_open:
		_close_address_bar()
	else:
		_addr_open = true
		_addr_text = ""
		_blink = 0.0
		_addr_root.visible = true
		_title_label.visible = false
		_update_address_label()


func _close_address_bar() -> void:
	_addr_open = false
	if _addr_root:
		_addr_root.visible = false
	if _title_label:
		_title_label.visible = true


func _address_key(k: InputEventKey) -> void:
	match k.keycode:
		KEY_ENTER, KEY_KP_ENTER:
			var t := _addr_text.strip_edges()
			_close_address_bar()
			if t != "":
				_send({"cmd": "open", "url": t})   # the service turns words into a web search
		KEY_BACKSPACE:
			if k.ctrl_pressed:
				_addr_text = ""
			else:
				_addr_text = _addr_text.left(_addr_text.length() - 1)
		KEY_HOME:
			_close_address_bar()
			_send({"cmd": "nav", "action": "home"})
		_:
			if k.ctrl_pressed and k.keycode == KEY_V:
				_addr_text += DisplayServer.clipboard_get().strip_edges().replace("\n", " ")
			elif not k.ctrl_pressed and not k.alt_pressed and k.unicode >= 32:
				_addr_text += char(k.unicode)
	_blink = 0.0
	_update_address_label()


func _update_address_label() -> void:
	var shown := _addr_text
	if shown.length() > 36:
		shown = "..." + shown.right(35)
	_addr_label.text = "> " + shown + ("_" if fmod(_blink, 1.0) < 0.55 else " ")


# ---------------------------------------------------------------- visuals
func _set_title(t: String) -> void:
	if t.length() > 40:
		t = t.left(38) + "..."
	_title_label.text = t
	if _station_label:
		_station_label.visible = t == ""


func _ensure_collision() -> void:
	for c in _surface.get_children():
		if c is CollisionObject3D:
			return
	var body := StaticBody3D.new()
	body.name = "ScreenBody"
	var shape := CollisionShape3D.new()
	var box := BoxShape3D.new()
	box.size = Vector3(_surf_size.x, _surf_size.y, 0.04)
	shape.shape = box
	shape.position = Vector3(0, 0, -0.01)
	body.add_child(shape)
	_surface.add_child(body)


func _surface_point(c: Vector2, lift: float) -> Vector3:
	var local := Vector3((c.x / page_w - 0.5) * _surf_size.x, (0.5 - c.y / page_h) * _surf_size.y, _surf_z + lift)
	return _surface.transform * local


func _make_cursor() -> void:
	_cursor_node = Node3D.new()
	_cursor_node.name = "Cursor"
	var mat := StandardMaterial3D.new()
	mat.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	mat.albedo_color = Color(1.0, 0.85, 0.1)
	mat.no_depth_test = true
	mat.render_priority = 5
	var ring := MeshInstance3D.new()
	var torus := TorusMesh.new()
	torus.inner_radius = 0.024
	torus.outer_radius = 0.04
	torus.rings = 24
	torus.ring_segments = 6
	torus.material = mat
	ring.mesh = torus
	ring.rotation = Vector3(PI / 2, 0, 0)       # torus lies in XZ; stand it up to face +Z
	_cursor_node.add_child(ring)
	var dot := MeshInstance3D.new()
	var sphere := SphereMesh.new()
	sphere.radius = 0.009
	sphere.height = 0.018
	sphere.radial_segments = 8
	sphere.rings = 4
	var dmat := mat.duplicate() as StandardMaterial3D
	dmat.albedo_color = Color(0.05, 0.05, 0.05)
	sphere.material = dmat
	dot.mesh = sphere
	_cursor_node.add_child(dot)
	_cursor_node.visible = false
	add_child(_cursor_node)


func _place_cursor() -> void:
	if _cursor_node:
		_cursor_node.position = _surface_point(_cursor, 0.012)
		_cursor_node.basis = _surface.transform.basis


func _make_title_bar() -> void:
	_title_label = Label3D.new()
	_title_label.name = "TitleBar"
	var y := _surface.position.y + _surf_size.y * 0.5 + 0.17
	_title_label.position = Vector3(_surface.position.x, y, _surface.position.z + 0.01)
	_title_label.pixel_size = 0.0025
	_title_label.font_size = 56
	_title_label.outline_size = 12
	_title_label.outline_modulate = Color(0.05, 0.04, 0.03, 1)
	_title_label.modulate = Color(1, 0.96, 0.86)
	_title_label.text = ""
	add_child(_title_label)


func _make_address_bar() -> void:
	_addr_root = Node3D.new()
	_addr_root.name = "AddressBar"
	var y := _surface.position.y + _surf_size.y * 0.5 + 0.17
	_addr_root.position = Vector3(_surface.position.x, y, _surface.position.z + 0.02)
	var bg := MeshInstance3D.new()
	var q := QuadMesh.new()
	q.size = Vector2(_surf_size.x + 0.1, 0.24)
	var m := StandardMaterial3D.new()
	m.shading_mode = BaseMaterial3D.SHADING_MODE_UNSHADED
	m.albedo_color = Color(0.96, 0.95, 0.9)
	q.material = m
	bg.mesh = q
	bg.position.z = -0.005
	_addr_root.add_child(bg)
	_addr_label = Label3D.new()
	_addr_label.pixel_size = 0.0025
	_addr_label.font_size = 60
	_addr_label.outline_size = 0
	_addr_label.modulate = Color(0.05, 0.05, 0.08)
	_addr_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_LEFT
	_addr_label.position.x = -_surf_size.x * 0.5
	_addr_root.add_child(_addr_label)
	var hint := Label3D.new()
	hint.pixel_size = 0.0025
	hint.font_size = 34
	hint.outline_size = 8
	hint.modulate = Color(1, 0.85, 0.3)
	hint.text = "Type a web address or search words, Enter to go"
	hint.position.y = 0.2
	_addr_root.add_child(hint)
	_addr_root.visible = false
	add_child(_addr_root)

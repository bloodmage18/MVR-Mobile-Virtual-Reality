extends SceneTree
## Dev test for the browser terminals: loads the library, points Station_Browser_1 at a running
## media service, opens pages programmatically, stands at the station's ViewSpot and saves screenshots.
##   (start the service first:  python home_space/service/media_service.py --port 8092 --modules browser)
##   Godot --path home_space/godot --script res://tools/browser_station_test.gd -- <out_dir> [port] [url]
## Checks: frames arrive, the Godot cursor clicks the right spot on the page (mouse and gaze),
## the address bar navigates, leaving and re-entering does not reload the page, the screencast
## stops when walking away and resumes when coming back, a station with no service says so.
## Prints frames received/shown, upload timing and PASS/FAIL.

const STATION := "Floor1/Stations/Station_Browser_1"
const DEAD_STATION := "Floor1/Stations/Station_Browser_2"

## Button at CSS (100..300, 100..200) = page image (125..375, 125..250) at zoom 1.25.
## Top-left quarter red, bottom-right blue: shows the texture orientation in the screenshot.
const TEST_PAGE := "<html><head><title>ready</title></head><body style='margin:0;background:#eee;font:bold 40px sans-serif'>" \
	+ "<div style='position:absolute;left:0;top:0;width:512px;height:288px;background:#d22'></div>" \
	+ "<div style='position:absolute;left:512px;top:288px;width:512px;height:288px;background:#22d'></div>" \
	+ "<div style='position:absolute;left:560px;top:40px'>TOP RIGHT</div>" \
	+ "<button id=b style='position:absolute;left:100px;top:100px;width:200px;height:100px;font-size:30px'>CLICK</button>" \
	+ "<button id=g style='position:absolute;left:800px;top:100px;width:150px;height:100px;font-size:30px'>GAZE</button>" \
	+ "<script>b.onclick=e=>document.title='clicked '+e.clientX+','+e.clientY;" \
	+ "g.onclick=e=>document.title='gaze '+e.clientX+','+e.clientY;</script></body></html>"

var results := []


func check(name: String, ok: bool, detail := "") -> void:
	results.append(ok)
	print("[browser-test] %s %s %s" % ["ok  " if ok else "FAIL", name, detail])


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	var out: String = args[0] if args.size() > 0 else "user://"
	var port: String = args[1] if args.size() > 1 else "8092"
	var url: String = args[2] if args.size() > 2 else ""
	var scene: Node = load("res://library.tscn").instantiate()
	root.add_child(scene)
	current_scene = scene
	await process_frame
	var stereo := scene.get_node_or_null("HMDStereo")
	if stereo:
		stereo.enabled = false
		stereo._apply()
	var player: Node3D = scene.get_node("Player")
	player.set_physics_process(false)
	var st: Node3D = scene.get_node(STATION)
	st.service_url = "ws://127.0.0.1:%s" % port
	var dead: Node3D = scene.get_node(DEAD_STATION)
	dead.service_url = "ws://127.0.0.1:1"          # nothing listens there
	# Stand at the ViewSpot, feet on floor 1, looking at the screen (opposite to the station's +Z).
	var spot: Node3D = st.get_node("ViewSpot")
	var feet := spot.global_position
	var stand := Vector3(feet.x, 0.95, feet.z)
	player.global_position = stand
	var fwd := st.global_basis.z
	player.rotation.y = atan2(fwd.x, fwd.z)          # player looks along -Z; face back toward the screen
	var head: Node3D = player.get_node("Head")
	head.rotation = Vector3(deg_to_rad(-4), 0, 0)
	st._player = player

	var t0 := Time.get_ticks_msec()
	st.open_page(url if url != "" else "")
	var ok := await _wait(func(): return st.frames_shown > 0, 15.0)
	check("first frame after %d ms" % (Time.get_ticks_msec() - t0), ok)
	await _wait(func(): return st.page_title != "" and not st.loading, 10.0)
	await _frames(45)
	root.get_texture().get_image().save_png(out.path_join("browser_start.png"))
	print("[browser-test] title='%s' url='%s'" % [st.page_title, st.page_url])

	# ---- use it like a player: focus, then click a known button with the mouse cursor
	st.interact(player)
	st._send({"cmd": "open", "url": "data:text/html," + TEST_PAGE.uri_encode()})
	ok = await _wait(func(): return st.page_title == "ready", 10.0)
	await _frames(30)
	var target := Vector2(250, 188)                 # button centre in page-image pixels
	st.focused_input(_motion((target - st._cursor) / st.cursor_speed))
	st.focused_process(0.016)
	check("cursor moved to button", st._cursor.distance_to(target) < 1.5, str(st._cursor))
	await _frames(10)
	root.get_texture().get_image().save_png(out.path_join("browser_orientation_cursor.png"))
	st.focused_input(_button(MOUSE_BUTTON_LEFT, true))
	st.focused_input(_button(MOUSE_BUTTON_LEFT, false))
	ok = await _wait(func(): return st.page_title.begins_with("clicked"), 5.0)
	check("mouse click hits the button", st.page_title == "clicked 200,150", "title '%s'" % st.page_title)

	# ---- gaze cursor: look at the GAZE button (CSS 875,150 -> image 1094,188)
	var cam: Camera3D = player.get("cam")
	var gaze_target := Vector2(1094, 188)
	var aim: Vector3 = st._surface.global_transform * Vector3(
			(gaze_target.x / st.page_w - 0.5) * st._surf_size.x, (0.5 - gaze_target.y / st.page_h) * st._surf_size.y, 0)
	var saved_head := head.transform
	cam.look_at(aim)
	st.gaze_cursor = true
	st.focused_process(0.016)
	st.gaze_cursor = false
	check("gaze maps to the looked-at page spot", st._cursor.distance_to(gaze_target) < 3.0, str(st._cursor))
	st.focused_input(_button(MOUSE_BUTTON_LEFT, true))
	st.focused_input(_button(MOUSE_BUTTON_LEFT, false))
	await _wait(func(): return st.page_title.begins_with("gaze"), 5.0)
	check("gaze click hits the GAZE button", st.page_title == "gaze 875,150", "title '%s'" % st.page_title)
	cam.transform = Transform3D.IDENTITY
	head.transform = saved_head

	# ---- leave and come back: the page must not reload (title would go back to 'ready')
	player.unfocus()
	await _frames(20)
	st.interact(player)
	await _wait(func(): return st.page_title == "ready", 2.5)
	check("re-entering keeps the page (no reload)", st.page_title == "gaze 875,150", "title '%s'" % st.page_title)

	# ---- address bar
	var key := InputEventKey.new()
	key.pressed = true
	key.keycode = KEY_L
	key.ctrl_pressed = true
	st.focused_input(key)
	for ch in "example.com":
		var k := InputEventKey.new()
		k.pressed = true
		k.unicode = ch.unicode_at(0)
		k.keycode = OS.find_keycode_from_string(ch.to_upper()) if ch != "." else KEY_PERIOD
		st.focused_input(k)
	await _frames(20)
	root.get_texture().get_image().save_png(out.path_join("browser_addressbar.png"))
	var enter := InputEventKey.new()
	enter.pressed = true
	enter.keycode = KEY_ENTER
	st.focused_input(enter)
	var nav := await _wait(func(): return st.page_url.contains("example.com") and not st.loading, 20.0)
	check("address bar navigates", nav, "title '%s'" % st.page_title)

	# ---- animated page for the frame rate measurement
	var anim := "data:text/html," + ("<body style='margin:0;background:#000'><canvas id=c width=1024 height=576>" +
		"</canvas><script>let c=document.getElementById('c').getContext('2d'),i=0;function f(){" +
		"c.fillStyle='hsl('+(i++*3%360)+',70%,45%)';c.fillRect(0,0,1024,576);c.fillStyle='#fff';" +
		"c.font='bold 140px sans-serif';c.fillText('frame '+i,120,330);requestAnimationFrame(f)}f()</script>").uri_encode()
	st._send({"cmd": "open", "url": anim})
	await _wait(func(): return st.page_url.begins_with("data:"), 10.0)
	await _frames(30)
	var r0: int = st.frames_received
	var s0: int = st.frames_shown
	var c0 := Time.get_ticks_msec()
	var n := 0
	while Time.get_ticks_msec() - c0 < 5000:
		await process_frame
		n += 1
	var secs := (Time.get_ticks_msec() - c0) / 1000.0
	var fps_in: float = (st.frames_received - r0) / secs
	print("[browser-test] animated page: %.1f frames/s received, %.1f uploads/s, game loop %.0f fps"
		% [fps_in, (st.frames_shown - s0) / secs, n / secs])
	print("[browser-test] per frame: decode %.1f ms on a worker thread, upload %.1f ms on the main thread"
		% [st.decode_ms, st.upload_ms])
	check("animated page streams", fps_in > 10.0, "%.1f fps" % fps_in)

	# ---- back to a readable page, leave, walk away -> screencast stops; come back -> resumes
	st._send({"cmd": "open", "url": st._home_url()})
	await _wait(func(): return st.page_url.ends_with("/start") and not st.loading, 10.0)
	await _frames(40)
	root.get_texture().get_image().save_png(out.path_join("browser_final.png"))
	st._send({"cmd": "open", "url": anim})        # keeps changing, so frames would flow if casting
	await _frames(20)
	player.unfocus()
	player.global_position = Vector3(0, 0.95, 6)
	await _frames(30)
	var r1: int = st.frames_received
	await _frames(60)
	check("inactive when away", not st._active_sent and st.frames_received - r1 == 0,
		"frames while away %d" % (st.frames_received - r1))
	player.global_position = stand
	var r2: int = st.frames_received
	ok = await _wait(func(): return st.frames_received - r2 > 5, 5.0)
	check("screencast resumes when back", ok and st._active_sent, "frames %d" % (st.frames_received - r2))
	print("[browser-test] frames received %d, shown %d" % [st.frames_received, st.frames_shown])

	# ---- a station whose service is down says so instead of hanging
	dead._player = player
	dead.open_page()
	await _wait(func(): return dead._ws == null, 5.0)
	var dt: String = dead._title_label.text
	check("service down -> message", dt.contains("not running"), "'%s'" % dt)

	var pass_ := not results.has(false)
	print("[browser-test] ", "PASS" if pass_ else "FAIL")
	quit(0 if pass_ else 1)


func _motion(rel: Vector2) -> InputEventMouseMotion:
	var m := InputEventMouseMotion.new()
	m.relative = rel
	return m


func _button(idx: MouseButton, pressed: bool) -> InputEventMouseButton:
	var b := InputEventMouseButton.new()
	b.button_index = idx
	b.pressed = pressed
	return b


func _frames(n: int) -> void:
	for i in n:
		await process_frame


func _wait(cond: Callable, timeout: float) -> bool:
	var end := Time.get_ticks_msec() + int(timeout * 1000)
	while Time.get_ticks_msec() < end:
		if cond.call():
			return true
		await process_frame
	return cond.call()

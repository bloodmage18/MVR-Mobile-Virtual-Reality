extends SceneTree
## Automated test for the cinema video screen (scripts/stations/video_screen.gd).
## Needs the media service running with the video module, e.g.
##   python home_space/service/media_service.py --port 8091 --modules video
## then
##   Godot --path home_space/godot --script res://tools/video_station_test.gd -- [port=8091] [out_dir=user://]
## Opens the menu (screenshot), plays the first video as the menu would, measures frames/fps for
## ~3 s, saves screenshots from the seats, stops, and prints "[video] PASS" or "[video] FAIL ...".
## Optional third arg: "interact" (raycast, E/Enter/Esc through the player, seek spam, lounge
## screen, missing file) or "offline" (point the port at nothing: the service-not-running path).

const STATION := "Floor3/Stations/Station_Screen_Main"


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	var port: String = args[0] if args.size() > 0 else "8091"
	var out: String = args[1] if args.size() > 1 else "user://"
	var scene: Node = load("res://library.tscn").instantiate()
	var st: Node = scene.get_node(STATION)
	st.service_url = "ws://127.0.0.1:%s" % port
	root.add_child(scene)
	current_scene = scene
	await process_frame
	var stereo := scene.get_node_or_null("HMDStereo")
	if stereo:
		stereo.enabled = false
		stereo._apply()
	var player: Node3D = scene.get_node("Player")
	player.set_physics_process(false)
	var head: Node3D = player.get_node("Head")
	var mode: String = args[2] if args.size() > 2 else "basic"
	if mode == "interact":
		await _interact_test(scene, st, player, head, out)
		return
	if mode == "offline":
		await _offline_test(st, player, head, out)
		return

	# 1. the menu, as interact() would show it, seen from near the screen
	st.open_menu()
	var t0 := Time.get_ticks_msec()
	while st.videos.is_empty() and Time.get_ticks_msec() - t0 < 30000:
		await process_frame
	if st.videos.is_empty():
		print("[video] FAIL: no videos from http://127.0.0.1:%s/api/videos" % port)
		quit(1)
		return
	print("[video] list: %d video(s), first = %s" % [st.videos.size(), st.videos[0].get("name")])
	_place(player, head, Vector3(-4.5, 8, 0), 90.0, 4.0)
	await _frames(15)
	_shot(out, "video_menu_rowB")
	var b0 := Engine.get_process_frames()
	var bm := Time.get_ticks_msec()
	_place(player, head, Vector3(1.0, 8, 0), 90.0, 0.0)
	while Time.get_ticks_msec() - bm < 1500:
		await process_frame
	print("[video] engine baseline (no video): %.1f fps" % ((Engine.get_process_frames() - b0) / ((Time.get_ticks_msec() - bm) / 1000.0)))

	# 2. play the first item (what Enter / A does in the menu)
	st.play_item(0)
	t0 = Time.get_ticks_msec()
	while st.frames_received == 0 and Time.get_ticks_msec() - t0 < 10000:
		await process_frame
	var first_ms := Time.get_ticks_msec() - t0
	var f0: int = st.frames_received
	var p0: int = st.packets_received
	var e0 := Engine.get_process_frames()
	var m0 := Time.get_ticks_msec()
	_place(player, head, Vector3(1.0, 8, 0), 90.0, 0.0)
	while Time.get_ticks_msec() - m0 < 3000:
		await process_frame
	var secs := (Time.get_ticks_msec() - m0) / 1000.0
	var n: int = st.frames_received - f0
	var fps := n / secs
	print("[video] engine %.1f fps, %d frame packets received, %d uploaded"
			% [(Engine.get_process_frames() - e0) / secs, st.packets_received - p0, n])
	print("[video] first frame after %d ms; %d frames in %.2f s = %.1f fps (station fps %.1f); status t=%.2f state=%s %dx%d"
			% [first_ms, n, secs, fps, st.measured_fps(), float(st.status.get("t", 0)), st.state,
			int(st.status.get("w", 0)), int(st.status.get("h", 0))])
	_shot(out, "video_seat_x1")
	_place(player, head, Vector3(-4.5, 8, 0), 90.0, 4.0)
	await _frames(10)
	_shot(out, "video_rowB")

	# 3. pause through the station's own input handling, then stop
	var ev := InputEventKey.new()
	ev.physical_keycode = KEY_SPACE
	ev.pressed = true
	st.focused_input(ev)
	var q0 := Time.get_ticks_msec()
	while st.state != "paused" and Time.get_ticks_msec() - q0 < 3000:
		await process_frame
	await _frames(10)
	_shot(out, "video_paused_rowB")
	print("[video] after Space: state=%s" % st.state)
	var paused_ok: bool = st.state == "paused"
	st.stop_video()
	await _frames(20)
	var ok := n >= 45 and fps >= 20.0 and paused_ok
	print("[video] PASS" if ok else "[video] FAIL (frames %d, fps %.1f, paused %s)" % [n, fps, paused_ok])
	quit(0 if ok else 1)


## mode "interact": the real path a player takes. Raycast reaches the screen, E focuses and opens
## the menu, Enter plays, the help line fits, Esc leaves focus (video keeps playing), seek spam,
## then the small lounge screen. Args: -- <port> <out_dir> interact
func _interact_test(scene: Node, st: Node, player: Node3D, head: Node3D, out: String) -> void:
	var fails: Array[String] = []
	_place(player, head, Vector3(-7.6, 8, 0), 90.0, 8.0)     # 2.2 m in front of the main screen
	await _frames(5)
	player._update_target()
	print("[video] raycast target: %s  prompt: '%s'" % [player._target.name if player._target else "<none>", player._prompt.text])
	if player._target != st:
		fails.append("raycast does not reach the main screen")
	st.interact(player)
	if player.focused != st:
		fails.append("interact() did not focus the player")
	var t0 := Time.get_ticks_msec()
	while st.videos.is_empty() and Time.get_ticks_msec() - t0 < 30000:
		await process_frame
	_key(player, KEY_DOWN)
	_key(player, KEY_UP)
	await _frames(5)
	_shot(out, "ix_menu_near")
	_key(player, KEY_ENTER)                                  # through hmd_player._unhandled_input
	t0 = Time.get_ticks_msec()
	while st.frames_received < 10 and Time.get_ticks_msec() - t0 < 10000:
		await process_frame
	if st.frames_received < 10:
		fails.append("Enter in the menu did not start playback")
	player.unfocus()
	player.focused = null
	player._update_target()
	print("[video] prompt while playing: '%s'" % player._prompt.text)
	st.interact(player)                                      # second E: controls + help line
	await _frames(5)
	_shot(out, "ix_controls_hint_main")
	# seek spam: hold Right (key repeat = echo events) 15x, then check we land ~+150 s or at the end
	for i in 15:
		var ev := InputEventKey.new()
		ev.physical_keycode = KEY_RIGHT
		ev.pressed = true
		ev.echo = i > 0
		player._unhandled_input(ev)
		await process_frame
	t0 = Time.get_ticks_msec()
	while st.state == "playing" and Time.get_ticks_msec() - t0 < 4000:
		await process_frame
	print("[video] after 15x Right on a 30 s clip: state=%s (expect ended)" % st.state)
	if st.state != "ended":
		fails.append("seek spam did not reach the end")
	# Esc leaves focus
	_key(player, KEY_ESCAPE)
	await _frames(3)
	if player.focused != null:
		fails.append("Esc did not unfocus")
	# small lounge screen (max_w 960)
	var sm: Node = scene.get_node("Floor3/Stations/Station_Screen_Small")
	sm.service_url = st.service_url
	sm.play_path(String(st.videos[0].get("path")), String(st.videos[0].get("name")))
	t0 = Time.get_ticks_msec()
	while sm.frames_received < 10 and Time.get_ticks_msec() - t0 < 10000:
		await process_frame
	print("[video] small screen: %d frames, %sx%s" % [sm.frames_received, sm.status.get("w"), sm.status.get("h")])
	if int(sm.status.get("w", 0)) != 960:
		fails.append("small screen not decoded at 960 wide")
	# lounge screen faces -Z (south wall at z=7.88 in floor coords); stand 2 m in front of it
	_place(player, head, Vector3(3.2, 8, 5.9), 180.0, -8.0)
	await _frames(3)
	player._update_target()
	if player._target != sm:
		fails.append("raycast does not reach the lounge screen (target %s)" % [player._target])
	sm.interact(player)
	sm._show_status()
	await _frames(5)
	_shot(out, "ix_controls_hint_small")
	# a file that is gone (stale menu entry): error, back to the menu, nothing left playing
	sm.play_path("C:/nope/missing video.mp4", "missing")
	t0 = Time.get_ticks_msec()
	while sm.is_active() and Time.get_ticks_msec() - t0 < 5000:
		await process_frame
	await _frames(5)
	_shot(out, "ix_missing_small")
	print("[video] missing file: state=%s error='%s' active=%s menu=%s" % [sm.state, sm.status.get("error", ""), sm.is_active(), sm._menu_open])
	if sm.state != "error" or sm.is_active() or not sm._menu_open:
		fails.append("missing file not handled")
	player.unfocus()
	sm.stop_video()
	st.stop_video()
	await _frames(20)
	print("[video] PASS" if fails.is_empty() else "[video] FAIL: " + ", ".join(fails))
	quit(0 if fails.is_empty() else 1)


## mode "offline": service not running. Expect a readable message, no error spam, no hang.
## Args: -- <unused port> <out_dir> offline
func _offline_test(st: Node, player: Node3D, head: Node3D, out: String) -> void:
	_place(player, head, Vector3(-7.6, 8, 0), 90.0, 8.0)
	st.interact(player)
	var t0 := Time.get_ticks_msec()
	while st._list_msg == "Loading..." and Time.get_ticks_msec() - t0 < 30000:
		await process_frame
	var list_ms := Time.get_ticks_msec() - t0
	var list_msg: String = st._list_msg
	print("[video] offline menu message after %d ms: '%s'" % [list_ms, list_msg.replace("\n", " | ")])
	await _frames(5)
	_shot(out, "offline_menu")
	st.play_path("C:/nope.mp4", "nope")                     # what a stale menu entry would do
	var f0 := Engine.get_process_frames()
	t0 = Time.get_ticks_msec()
	while st.is_active() and Time.get_ticks_msec() - t0 < 30000:
		await process_frame
	print("[video] offline play: after %d ms state=%s error='%s' active=%s (%d frames rendered)"
			% [Time.get_ticks_msec() - t0, st.state, st.status.get("error", ""), st.is_active(), Engine.get_process_frames() - f0])
	await _frames(5)
	_shot(out, "offline_play")
	var ok: bool = list_msg.begins_with("Media service not running") and list_ms < 6000 \
			and st.state == "error" and not st.is_active()
	player.unfocus()
	print("[video] PASS" if ok else "[video] FAIL")
	quit(0 if ok else 1)


func _key(player: Node, k: Key) -> void:
	var ev := InputEventKey.new()
	ev.physical_keycode = k
	ev.keycode = k
	ev.pressed = true
	player._unhandled_input(ev)


func _place(player: Node3D, head: Node3D, feet: Vector3, yaw: float, pitch: float) -> void:
	player.global_position = feet + Vector3(0, 0.95, 0)
	player.rotation.y = deg_to_rad(yaw)
	head.rotation = Vector3(deg_to_rad(pitch), 0, 0)


func _frames(k: int) -> void:
	for i in k:
		await process_frame


func _shot(out: String, name: String) -> void:
	var path := out.path_join(name + ".png")
	root.get_texture().get_image().save_png(path)
	print("[video] saved ", ProjectSettings.globalize_path(path))

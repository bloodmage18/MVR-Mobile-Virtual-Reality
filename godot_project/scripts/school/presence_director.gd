extends Node
class_name PresenceDirector
## Runs the "presence" scares: nothing is ever seen, only heard or moved.
##
##   1. Distant footsteps cross the far end of the corridor and a classroom door creaks open.
##   2. The first classroom you walk into: its door slowly creaks shut behind you.
##   3. A locker bangs right next to you in the corridor.
##   4. Chairs scrape in rooms you are not in; something whispers behind you now and then.
##   5. The bell rings every school hour; the speaker announces lessons that aren't happening.
##   6. Taking the key: the corridor lights stutter and die, a door slams, a whisper at your ear.
##   7. With the key: footsteps follow you. They stop the moment you turn to look.
##   8. Through the gate onto the assembly ground: you got out.

@export var school_path: NodePath = ^"../School"
@export var player_path: NodePath = ^"../Player"
@export var clock_path: NodePath = ^"../SchoolClock"

var school: SchoolBuilder
var player: CharacterBody3D
var clock: SchoolClock
var cam: Camera3D
var rng := RandomNumberGenerator.new()
var snd := {}
var fired := {}
var ended := false

var _ambient: AudioStreamPlayer
var _start_msec := 0
var _next_scrape := 35.0
var _next_whisper := 75.0
var _elapsed := 0.0


func _ready() -> void:
	rng.randomize()
	school = get_node(school_path)
	player = get_node(player_path)
	clock = get_node(clock_path)
	cam = player.get_node("Head/Camera3D")
	for n in ["ambient", "door_creak", "door_slam", "locker_bang", "whisper", "breath",
			"chair_scrape", "school_bell", "footstep_1", "footstep_2", "footstep_3"]:
		snd[n] = SFX.load_sound(n)

	player.global_position = school.start_position
	player.rotation.y = 0.0          # facing north, down the corridor
	clock.labels = school.clock_labels
	clock.hour_passed.connect(_on_hour)
	if school.key_node:
		school.key_node.picked_up.connect(_on_key_taken)

	_ambient = AudioStreamPlayer.new()
	_ambient.stream = SFX.looped(snd.ambient)
	_ambient.volume_db = -12.0
	add_child(_ambient)
	_ambient.play()

	_start_msec = Time.get_ticks_msec()
	await _wait(1.5)
	_say("%s. Prep time ended hours ago.\nFind the key to the gate and get out." % clock.time_text(), 7.0)


func _process(delta: float) -> void:
	if ended:
		return
	_elapsed += delta
	var pos := player.global_position
	var tile := school.world_to_tile(pos)
	var room := school.room_at(pos)
	var in_corridor := room == school.corridor_id

	# 1. distant footsteps once you're a little way up the corridor
	if not fired.has("distant") and in_corridor and tile.y <= 18:
		fired["distant"] = true
		_distant_footsteps()

	# 2. a classroom door closes behind you
	if not fired.has("shut") and school.is_classroom(room):
		var door: SchoolDoor = school.rooms[room].door
		if door and door.is_open and pos.distance_to(door.global_position) > 3.5:
			fired["shut"] = true
			_shut_behind(door, room)

	# 3. locker bang at your shoulder
	if not fired.has("locker") and in_corridor and tile.y <= 12:
		fired["locker"] = true
		_locker_bang()

	# 4. background presence
	if _elapsed > _next_scrape:
		_next_scrape = _elapsed + rng.randf_range(30.0, 65.0)
		_chair_scrape(room)
	if _elapsed > _next_whisper:
		_next_whisper = _elapsed + rng.randf_range(60.0, 120.0)
		_whisper(0.9, -14.0)

	# 7. the follower, once you carry the key back into the corridor
	if fired.has("key") and not fired.has("follower") and in_corridor and _elapsed > fired.key + 8.0:
		fired["follower"] = true
		_follower()

	# 8. out through the gate
	if pos.z < school.exit_line_z:
		_escape()


# ── events ───────────────────────────────────────────────────────────────────

func _distant_footsteps() -> void:
	# Someone crosses the far end of the corridor and goes into a classroom.
	var target: SchoolDoor = null
	var best := INF
	for d in school.doors:
		if d.global_position.z < best:
			best = d.global_position.z
			target = d
	if target == null:
		return
	# start on the far side of the corridor and walk into that door's classroom
	var side := signf(target.global_position.x - school.corridor_x)
	var z := target.global_position.z + SchoolBuilder.DOOR_W * 0.5   # door hinge -> tile centre
	var from := Vector3(school.corridor_x - side * 1.6, 0.1, z)
	var to := Vector3(target.global_position.x + side * 1.0, 0.1, z)
	await _walk(from, to, 7, 0.62, -2.0)
	if not target.is_open:
		target.open_door(2.2)


func _shut_behind(door: SchoolDoor, room: int) -> void:
	await _wait(2.5)
	if school.room_at(player.global_position) == room and door.is_open:
		door.close_door(3.0)


func _locker_bang() -> void:
	var pos := player.global_position
	var best := Vector3.ZERO
	var best_d := INF
	for spot in school.locker_spots:
		var d := spot.distance_to(pos)
		if d < best_d:
			best_d = d
			best = spot
	if best_d < 8.0:
		SFX.play_at(_scene(), snd.locker_bang, best, 3.0, rng.randf_range(0.9, 1.05), 4.0)


func _chair_scrape(player_room: int) -> void:
	var options := []
	for id in school.rooms:
		if school.is_classroom(id) and id != player_room:
			options.append(id)
	if options.is_empty():
		return
	var id: int = options[rng.randi_range(0, options.size() - 1)]
	var c: Vector3 = school.rooms[id].center
	SFX.play_at(_scene(), snd.chair_scrape, c + Vector3(rng.randf_range(-2, 2), 0.3, rng.randf_range(-1, 1)),
			-1.0, rng.randf_range(0.8, 1.1), 4.0)


func _whisper(distance: float, volume: float) -> void:
	# Behind you, a little to one side.
	var back := cam.global_basis.z
	var side := cam.global_basis.x * (1.0 if rng.randf() < 0.5 else -1.0)
	var pos := cam.global_position + back * distance + side * 0.4
	SFX.play_at(_scene(), snd.whisper, pos, volume, rng.randf_range(0.85, 1.05), 1.0)


func _on_hour(hour: int, period: String) -> void:
	if ended:
		return
	SFX.play_at(_scene(), snd.school_bell, school.speaker_position, 2.0, 1.0, 8.0)
	# At night the speaker announces daytime lessons, out of order.
	var announced := period
	if period == "DIMENSIONAL":
		var day: Array = clock.day_periods()
		announced = day[rng.randi_range(0, day.size() - 1)]
	await _wait(3.2)
	_say("%02d:00. The bell rings.\nThe speaker crackles: \"%s.\"" % [hour, announced], 5.0)


func _on_key_taken() -> void:
	fired["key"] = _elapsed
	await _wait(1.0)
	for l in school.corridor_lights:
		l.flicker_burst(1.6)
	await _wait(1.6)
	for l in school.corridor_lights:
		l.blackout(5.0)
	await _wait(1.2)
	# slam the open door furthest from you
	var far: SchoolDoor = null
	var far_d := 0.0
	for d in school.doors:
		var dist := d.global_position.distance_to(player.global_position)
		if d.is_open and dist > far_d:
			far_d = dist
			far = d
	if far:
		far.close_door(0.3, true)
	await _wait(2.2)
	_whisper(0.5, -6.0)


func _follower() -> void:
	# Footsteps come up behind you. Turn around and they stop.
	var behind := player.global_basis.z.normalized()
	var pos := player.global_position + behind * 10.0
	pos.x = school.corridor_x
	pos.y = 0.1
	var steps := 0
	while not ended and steps < 40:
		var to_player := player.global_position - pos
		to_player.y = 0.0
		if to_player.length() < 1.6:
			SFX.play_at(_scene(), snd.breath, cam.global_position + cam.global_basis.z * 0.35, -2.0, 0.9, 1.0)
			_say("...", 2.0)
			return
		var dir_to_it := (pos - cam.global_position).normalized()
		var looking := (-cam.global_basis.z).dot(dir_to_it) > 0.55
		if looking and steps > 2:
			return                           # silence the moment you look
		var step: AudioStream = snd["footstep_%d" % (steps % 3 + 1)]
		SFX.play_at(_scene(), step, pos, 0.0, rng.randf_range(0.8, 0.95), 4.0)
		pos += to_player.normalized() * 0.75
		steps += 1
		await _wait(maxf(0.62 - steps * 0.015, 0.4))   # it speeds up


func _escape() -> void:
	ended = true
	var secs := (Time.get_ticks_msec() - _start_msec) / 1000.0
	var tw := create_tween()
	tw.tween_property(_ambient, "volume_db", -40.0, 4.0)
	SFX.play_at(_scene(), snd.school_bell, school.speaker_position, -4.0, 0.95, 8.0)
	_say("You got out.\nTime inside: %d:%02d" % [int(secs) / 60, int(secs) % 60], 8.0)
	await _wait(8.5)
	_say("F10 or Start to play again", 60.0)


# ── helpers ──────────────────────────────────────────────────────────────────

func _walk(from: Vector3, to: Vector3, steps: int, interval: float, volume: float) -> void:
	for i in steps:
		var p := from.lerp(to, float(i) / maxf(steps - 1, 1))
		var step: AudioStream = snd["footstep_%d" % (i % 3 + 1)]
		SFX.play_at(_scene(), step, p, volume, rng.randf_range(0.85, 1.0), 4.0)
		await _wait(interval)


func _say(text: String, seconds: float) -> void:
	if player.has_method("show_message"):
		player.show_message(text, seconds)


func _wait(seconds: float) -> Signal:
	return get_tree().create_timer(seconds).timeout


func _scene() -> Node:
	return get_tree().current_scene

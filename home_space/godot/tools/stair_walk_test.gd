extends SceneTree
## Dev test: walks the real Player (its own movement code, by pressing move_forward) from the
## entrance up both staircases to floor 3, then back down to floor 1. Prints PASS/FAIL.
##   Godot --path home_space/godot --script res://tools/stair_walk_test.gd

# Waypoints at feet level (x, z, expected floor y when reached)
const UP := [
	Vector3(8.9, 0.0, 0.0),    # stair approach, floor 1
	Vector3(9.0, 0.0, -1.0),   # flight 1A foot
	Vector3(9.0, 0.0, -6.3),   # landing 1
	Vector3(7.0, 0.0, -6.3),
	Vector3(7.0, 0.0, -0.6),   # top of 1B -> floor 2
	Vector3(9.0, 0.0, -0.6),
	Vector3(9.0, 0.0, -6.3),   # landing 2
	Vector3(7.0, 0.0, -6.3),
	Vector3(7.0, 0.0, 0.5),    # top of 2B -> floor 3
	Vector3(8.0, 0.0, 2.5),    # floor 3 landing zone
]
const DOWN := [
	Vector3(7.0, 0.0, 0.5), Vector3(7.0, 0.0, -6.3), Vector3(9.0, 0.0, -6.3),
	Vector3(9.0, 0.0, -0.6), Vector3(7.0, 0.0, -0.6), Vector3(7.0, 0.0, -6.3),
	Vector3(9.0, 0.0, -6.3), Vector3(9.0, 0.0, 0.5), Vector3(8.0, 0.0, 2.5),
]

var _player: CharacterBody3D
var _route: Array = []
var _i := 0
var _stuck := 0.0
var _last := Vector3.ZERO
var _frames := 0
var _phase := "up"
var _max_y := 0.0


func _initialize() -> void:
	var scene: Node = load("res://library.tscn").instantiate()
	root.add_child(scene)
	current_scene = scene
	_player = scene.get_node("Player")
	scene.get_node("Player/Head").set("enabled", false)
	_route = UP.duplicate()
	physics_frame.connect(_step)


func _step() -> void:
	_frames += 1
	if _frames < 10:
		return
	var p := _player.global_position
	_max_y = maxf(_max_y, p.y)
	var target: Vector3 = _route[_i]
	var flat := Vector2(target.x - p.x, target.z - p.z)
	if flat.length() < 0.35:
		_i += 1
		_stuck = 0.0
		if _i >= _route.size():
			if _phase == "up":
				var ok := p.y > 8.5
				print("[walk] reached floor 3: ", ok, "  body y=%.2f" % p.y)
				if not ok:
					return _finish(false, "did not reach floor 3")
				_phase = "down"
				_route = DOWN.duplicate()
				_i = 0
			else:
				return _finish(p.y < 1.5, "back on floor 1 (y=%.2f)" % p.y)
		return
	# face the waypoint (body yaw; -Z is forward) and walk forward with the real controls
	_player.rotation.y = atan2(-flat.x, -flat.y)
	Input.action_press("move_forward")
	if _frames % 30 == 0:
		if p.distance_to(_last) < 0.05:
			_stuck += 0.5
			if _stuck >= 2.0:
				return _finish(false, "stuck at %s heading to waypoint %d %s" % [p, _i, target])
		else:
			_stuck = 0.0
		_last = p
	if _frames > 60 * 120:
		_finish(false, "timeout")


func _finish(ok: bool, msg: String) -> void:
	Input.action_release("move_forward")
	print("[walk] ", "PASS" if ok else "FAIL", " (", _phase, "): ", msg, "  max y=%.2f" % _max_y)
	quit(0 if ok else 1)

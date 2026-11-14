extends Node3D
class_name SchoolDoor
## A hinged door (this node is the hinge). Built in code by SchoolBuilder.
## Look at it and press E / controller A to open or close. Locked gates need the key.

signal opened(door: SchoolDoor)
signal closed(door: SchoolDoor)

var kind := "door"           # "door" or "gate" (used in prompts and sounds)
var locked := false
var needs_key := false
var is_open := false
var open_angle := 95.0
var width := 1.3
var height := 2.3
var room_id := -1

var _base_rot := 0.0
var _tween: Tween
var _snd := {}


func build(w: float, h: float, mat: Material, handle_mat: Material) -> void:
	width = w
	height = h
	_base_rot = rotation.y
	for n in ["door_creak", "gate_creak", "door_slam", "rattle", "unlock"]:
		_snd[n] = SFX.load_sound(n)

	var body := AnimatableBody3D.new()
	body.position = Vector3(w * 0.5, h * 0.5, 0.0)
	add_child(body)
	var slab := MeshInstance3D.new()
	var bm := BoxMesh.new()
	bm.size = Vector3(w, h, 0.07)
	slab.mesh = bm
	slab.material_override = mat
	body.add_child(slab)
	var handle := MeshInstance3D.new()
	var hm := BoxMesh.new()
	hm.size = Vector3(0.14, 0.04, 0.16)
	handle.mesh = hm
	handle.material_override = handle_mat
	handle.position = Vector3(w * 0.5 - 0.12, 1.0 - h * 0.5, 0.0)
	body.add_child(handle)
	var cs := CollisionShape3D.new()
	var shape := BoxShape3D.new()
	shape.size = bm.size
	cs.shape = shape
	body.add_child(cs)


func set_angle_instant(angle_deg: float) -> void:
	rotation.y = _base_rot + deg_to_rad(angle_deg)
	is_open = absf(angle_deg) > 5.0


func prompt(player: Node) -> String:
	if locked:
		if needs_key and player.get("has_key") == true:
			return "Unlock the %s" % kind
		return "Try the %s" % kind
	return ("Close the %s" if is_open else "Open the %s") % kind


func interact(player: Node) -> String:
	if locked:
		if needs_key and player.get("has_key") == true:
			locked = false
			SFX.play_at(_scene(), _snd.unlock, _handle_pos(), 0.0)
			get_tree().create_timer(0.6).timeout.connect(open_door.bind(2.5))
			return "The key turns."
		SFX.play_at(_scene(), _snd.rattle, _handle_pos(), -2.0)
		return "Locked. It needs a key." if needs_key else "It won't open."
	if is_open:
		close_door()
	else:
		open_door()
	return ""


func open_door(duration := 1.3) -> void:
	is_open = true
	var sound: AudioStream = _snd.gate_creak if kind == "gate" else _snd.door_creak
	_swing(open_angle, duration, sound, -3.0, Tween.EASE_IN_OUT)
	opened.emit(self)


func close_door(duration := 1.5, slam := false) -> void:
	is_open = false
	if slam:
		_swing(0.0, 0.28, _snd.door_slam, 4.0, Tween.EASE_IN)
	else:
		_swing(0.0, duration, _snd.door_creak, -6.0, Tween.EASE_IN_OUT)
	closed.emit(self)


func _swing(angle_deg: float, duration: float, sound: AudioStream, vol: float, ease_type: Tween.EaseType) -> void:
	if _tween:
		_tween.kill()
	_tween = create_tween().set_trans(Tween.TRANS_SINE).set_ease(ease_type)
	_tween.tween_property(self, "rotation:y", _base_rot + deg_to_rad(angle_deg), duration)
	SFX.play_at(_scene(), sound, _handle_pos(), vol, randf_range(0.9, 1.05))


func _handle_pos() -> Vector3:
	return global_position + global_basis.x * (width * 0.8) + Vector3.UP * 1.0


func _scene() -> Node:
	return get_tree().current_scene

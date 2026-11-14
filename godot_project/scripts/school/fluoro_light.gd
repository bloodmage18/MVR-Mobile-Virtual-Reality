extends Node3D
class_name FluoroLight
## Ceiling fluorescent fixture: a light, a glowing tube, and a buzz.
## Modes: ON, FLICKER (unreliable tube), OFF (dead). The director can black it out
## or make it stutter for a moment.

enum Mode { ON, FLICKER, OFF }

var mode := Mode.ON
var energy := 0.9

var _light: OmniLight3D
var _tube: StandardMaterial3D
var _buzz: AudioStreamPlayer3D
var _lit := true
var _flick_timer := 0.0
var _burst_until := 0.0
var _blackout_until := 0.0


func build(buzz_stream: AudioStream) -> void:
	var housing := MeshInstance3D.new()
	var hb := BoxMesh.new()
	hb.size = Vector3(0.32, 0.06, 1.25)
	housing.mesh = hb
	var hmat := StandardMaterial3D.new()
	hmat.albedo_color = Color(0.25, 0.25, 0.24)
	housing.material_override = hmat
	add_child(housing)

	var tube := MeshInstance3D.new()
	var tb := BoxMesh.new()
	tb.size = Vector3(0.2, 0.04, 1.15)
	tube.mesh = tb
	tube.position = Vector3(0, -0.04, 0)
	_tube = StandardMaterial3D.new()
	_tube.albedo_color = Color(0.85, 0.9, 0.95)
	_tube.emission_enabled = true
	_tube.emission = Color(0.8, 0.9, 1.0)
	tube.material_override = _tube
	add_child(tube)

	_light = OmniLight3D.new()
	_light.position = Vector3(0, -0.35, 0)
	_light.omni_range = 7.5
	_light.light_color = Color(0.8, 0.9, 1.0)
	add_child(_light)

	if buzz_stream:
		_buzz = AudioStreamPlayer3D.new()
		_buzz.stream = SFX.looped(buzz_stream)
		_buzz.unit_size = 1.5
		_buzz.max_distance = 12.0
		_buzz.volume_db = -14.0
		add_child(_buzz)
		_buzz.play()
	_set_lit(mode != Mode.OFF)


func blackout(seconds: float) -> void:
	_blackout_until = _now() + seconds


func flicker_burst(seconds: float) -> void:
	_burst_until = _now() + seconds


func _process(delta: float) -> void:
	var now := _now()
	if now < _blackout_until or mode == Mode.OFF:
		_set_lit(false)
		return
	var bursting := now < _burst_until
	if mode == Mode.ON and not bursting:
		_set_lit(true)
		return
	_flick_timer -= delta
	if _flick_timer <= 0.0:
		_set_lit(not _lit)
		if bursting:
			_flick_timer = randf_range(0.02, 0.12)
		elif _lit:
			_flick_timer = randf_range(0.08, 2.8)   # stays on a while...
		else:
			_flick_timer = randf_range(0.03, 0.3)   # ...then drops out briefly


func _set_lit(on: bool) -> void:
	_lit = on
	_light.light_energy = energy if on else 0.0
	_tube.emission_energy_multiplier = 2.5 if on else 0.0
	if _buzz:
		_buzz.volume_db = -14.0 if on else -60.0


func _now() -> float:
	return Time.get_ticks_msec() / 1000.0

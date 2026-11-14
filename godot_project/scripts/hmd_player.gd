extends CharacterBody3D
## Player body for HMD play.
##   Mouse X / right stick X     -> turn left/right (yaw, rotates this body)
##   WASD / arrows / left stick  -> move
##   Phone ($Head, hmd_head.gd)  -> look up/down + roll (tilt mode, or full 3-DOF with a gyro/IMU)
##   No phone pose arriving      -> mouse Y / right stick Y look up/down instead
##   E / left click / A          -> interact (doors, the key)
##   F / Y                       -> flashlight on/off
##   R / Select                  -> recenter view
##   F10 / hold Start 1.5 s      -> restart
##   Esc = release mouse, click = capture mouse again

@export var mouse_sensitivity := 0.15      ## degrees per pixel of mouse movement
@export var invert_mouse_y := false
@export var stick_turn_speed := 140.0      ## degrees per second at full right-stick
@export var move_speed := 2.5              ## m/s, walking pace suits horror
@export var stick_deadzone := 0.15
@export var interact_distance := 2.4
@export var step_length := 0.8             ## metres between footstep sounds

@onready var head: Node3D = $Head
@onready var cam: Camera3D = $Head/Camera3D
@onready var flashlight: Light3D = get_node_or_null("Head/Flashlight")

var has_key := false

var _pitch_deg := 0.0
var _gravity: float = ProjectSettings.get_setting("physics/3d/default_gravity")
var _phone_was_active := false
var _msg: Label3D
var _prompt: Label3D
var _msg_timer := 0.0
var _steps: Array[AudioStream] = []
var _step_player: AudioStreamPlayer
var _step_accum := 0.0
var _click: AudioStream
var _start_held := 0.0
var _target: Node


func _ready() -> void:
	_setup_input_map()
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED
	_make_hud()
	for i in 3:
		var s := SFX.load_sound("footstep_%d" % (i + 1))
		if s:
			_steps.append(s)
	_click = SFX.load_sound("flashlight_click")
	_step_player = AudioStreamPlayer.new()
	_step_player.volume_db = -11.0
	add_child(_step_player)


## Floating text in front of the eyes (readable in both eyes in the headset).
func show_message(text: String, seconds := 4.0) -> void:
	_msg.text = text
	_msg.modulate.a = 1.0
	_msg_timer = seconds


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED:
		rotate_y(deg_to_rad(-event.relative.x * mouse_sensitivity))
		if not _phone_active():
			var dy: float = event.relative.y * (-1.0 if invert_mouse_y else 1.0)
			_pitch_deg = clampf(_pitch_deg - dy * mouse_sensitivity, -85.0, 85.0)
	elif event.is_action_pressed("ui_cancel"):
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
	elif event is InputEventMouseButton and event.pressed and Input.mouse_mode != Input.MOUSE_MODE_CAPTURED:
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED
		get_viewport().set_input_as_handled()   # this click only grabs the mouse


func _process(delta: float) -> void:
	if _msg_timer > 0.0:
		_msg_timer -= delta
		_msg.modulate.a = clampf(_msg_timer / 0.8, 0.0, 1.0)

	if Input.is_action_just_pressed("interact") and _target and is_instance_valid(_target):
		var result = _target.interact(self)
		if result is String and result != "":
			show_message(result, 2.5)
	if Input.is_action_just_pressed("flashlight") and flashlight:
		flashlight.visible = not flashlight.visible
		SFX.play_2d(self, _click, -6.0)
	if Input.is_action_just_pressed("restart"):
		get_tree().reload_current_scene()
	_start_held = _start_held + delta if Input.is_joy_button_pressed(0, JOY_BUTTON_START) else 0.0
	if _start_held > 1.5:
		get_tree().reload_current_scene()


func _physics_process(delta: float) -> void:
	# Right stick turns (and pitches when no phone)
	var rx := _deadzone(Input.get_joy_axis(0, JOY_AXIS_RIGHT_X))
	var ry := _deadzone(Input.get_joy_axis(0, JOY_AXIS_RIGHT_Y))
	rotate_y(deg_to_rad(-rx * stick_turn_speed * delta))

	var phone := _phone_active()
	if phone != _phone_was_active:
		print("[player] phone head tracking ", "ON (mouse = turn only)" if phone else "OFF (mouse = full look)")
		_phone_was_active = phone
	if not phone:
		_pitch_deg = clampf(_pitch_deg - ry * stick_turn_speed * delta, -85.0, 85.0)
		head.rotation = Vector3(deg_to_rad(_pitch_deg), 0.0, 0.0)

	# Movement relative to where the body faces
	var input := Input.get_vector("move_left", "move_right", "move_forward", "move_back")
	input += Vector2(_deadzone(Input.get_joy_axis(0, JOY_AXIS_LEFT_X)),
			_deadzone(Input.get_joy_axis(0, JOY_AXIS_LEFT_Y)))
	input = input.limit_length(1.0)
	var dir := transform.basis * Vector3(input.x, 0.0, input.y)
	velocity.x = dir.x * move_speed
	velocity.z = dir.z * move_speed
	velocity.y = 0.0 if is_on_floor() else velocity.y - _gravity * delta
	move_and_slide()
	_footsteps(delta)
	_update_target()


func _footsteps(delta: float) -> void:
	var speed := Vector2(velocity.x, velocity.z).length()
	if not is_on_floor() or speed < 0.3 or _steps.is_empty():
		_step_accum = step_length * 0.6   # first step comes quickly after starting
		return
	_step_accum += speed * delta
	if _step_accum >= step_length:
		_step_accum = 0.0
		_step_player.stream = _steps[randi() % _steps.size()]
		_step_player.pitch_scale = randf_range(0.92, 1.06)
		_step_player.play()


## What the player is looking at, within reach, that can be used.
func _update_target() -> void:
	var from := cam.global_position
	var to := from - cam.global_basis.z * interact_distance
	var query := PhysicsRayQueryParameters3D.create(from, to)
	query.exclude = [get_rid()]
	var hit := get_world_3d().direct_space_state.intersect_ray(query)
	_target = null
	if not hit.is_empty():
		var n: Node = hit.collider
		while n and not n.has_method("interact"):
			n = n.get_parent()
		_target = n
	if _target and _target.has_method("prompt"):
		_prompt.text = "[E / A]  %s" % _target.prompt(self)
	else:
		_prompt.text = ""


func _make_hud() -> void:
	_msg = _hud_label(Vector3(0, 0.06, -1.4), 40)
	_prompt = _hud_label(Vector3(0, -0.2, -1.4), 30)
	_prompt.modulate = Color(0.9, 0.88, 0.75, 0.85)


func _hud_label(pos: Vector3, size: int) -> Label3D:
	var l := Label3D.new()
	l.position = pos
	l.font_size = size
	l.pixel_size = 0.0012
	l.outline_size = 10
	l.outline_modulate = Color(0, 0, 0, 0.9)
	l.no_depth_test = true
	l.render_priority = 10
	l.outline_render_priority = 9
	l.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	cam.add_child(l)
	return l


func _phone_active() -> bool:
	return head.get("enabled") == true and head.get("connected") == true


func _deadzone(v: float) -> float:
	return 0.0 if absf(v) < stick_deadzone else v


func _setup_input_map() -> void:
	var keys := {
		&"move_forward": [KEY_W, KEY_UP],
		&"move_back": [KEY_S, KEY_DOWN],
		&"move_left": [KEY_A, KEY_LEFT],
		&"move_right": [KEY_D, KEY_RIGHT],
		&"hmd_recenter": [KEY_R],
		&"interact": [KEY_E],
		&"flashlight": [KEY_F],
		&"restart": [KEY_F10],
	}
	var pads := {
		&"hmd_recenter": JOY_BUTTON_BACK,
		&"interact": JOY_BUTTON_A,
		&"flashlight": JOY_BUTTON_Y,
	}
	for action in keys:
		if InputMap.has_action(action):
			continue
		InputMap.add_action(action)
		for k in keys[action]:
			var ev := InputEventKey.new()
			ev.physical_keycode = k
			InputMap.action_add_event(action, ev)
		if pads.has(action):
			var jb := InputEventJoypadButton.new()
			jb.button_index = pads[action]
			InputMap.action_add_event(action, jb)
	var click := InputEventMouseButton.new()
	click.button_index = MOUSE_BUTTON_LEFT
	if not InputMap.action_has_event(&"interact", click):
		InputMap.action_add_event(&"interact", click)

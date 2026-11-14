extends CharacterBody3D
## Player body for HMD play.
##   Mouse X / right stick X  -> turn left/right (yaw, rotates this body)
##   WASD / arrows / left stick -> move
##   Phone ($Head, hmd_head.gd) -> look up/down + roll (tilt mode, or full 3-DOF with a gyro/IMU)
##   No phone pose arriving    -> mouse Y / right stick Y look up/down instead
##   R = recenter, Esc = release mouse, click = capture mouse again

@export var mouse_sensitivity := 0.15      ## degrees per pixel of mouse movement
@export var invert_mouse_y := false
@export var stick_turn_speed := 140.0      ## degrees per second at full right-stick
@export var move_speed := 2.5              ## m/s, walking pace suits horror
@export var stick_deadzone := 0.15

@onready var head: Node3D = $Head
var _pitch_deg := 0.0
var _gravity: float = ProjectSettings.get_setting("physics/3d/default_gravity")
var _phone_was_active := false


func _ready() -> void:
	_setup_input_map()
	Input.mouse_mode = Input.MOUSE_MODE_CAPTURED


func _unhandled_input(event: InputEvent) -> void:
	if event is InputEventMouseMotion and Input.mouse_mode == Input.MOUSE_MODE_CAPTURED:
		rotate_y(deg_to_rad(-event.relative.x * mouse_sensitivity))
		if not _phone_active():
			var dy: float = event.relative.y * (-1.0 if invert_mouse_y else 1.0)
			_pitch_deg = clampf(_pitch_deg - dy * mouse_sensitivity, -85.0, 85.0)
	elif event.is_action_pressed("ui_cancel"):
		Input.mouse_mode = Input.MOUSE_MODE_VISIBLE
	elif event is InputEventMouseButton and event.pressed:
		Input.mouse_mode = Input.MOUSE_MODE_CAPTURED


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
	}
	for action in keys:
		if InputMap.has_action(action):
			continue
		InputMap.add_action(action)
		for k in keys[action]:
			var ev := InputEventKey.new()
			ev.physical_keycode = k
			InputMap.action_add_event(action, ev)
	var select := InputEventJoypadButton.new()
	select.button_index = JOY_BUTTON_BACK
	InputMap.action_add_event(&"hmd_recenter", select)

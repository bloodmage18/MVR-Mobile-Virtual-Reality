extends CanvasLayer
## Headset mode: two eye cameras side by side.
##
## Immersive mode (default, "lens on phone"): each eye is rendered WIDER than the screen can
## show (overscan) and sent undistorted. The phone applies the lens correction and pans the
## view inside the extra margin using its own sensor at 60-90 Hz (timewarp), so the image
## always runs past the screen edges and head movement shows up without waiting for the laptop.
## Settings are sent to the phone through the bridge (UDP 9101) whenever they change.
##
##   F2  stereo on/off (off = normal flat Windows view)
##   F3  lens distortion on/off
##   F4  lens correction on phone (immersive) / on laptop (classic framed view)
##   T   timewarp on/off (phone pans the view itself between frames)
##   [ ]  lens separation        - =  distortion strength      , .  zoom
##   9 0  field of view          ; '  eye distance (3D depth)
##   O P  overscan (extra margin) K L  timewarp latency
##   Backspace  reset to defaults

@export var source_camera: NodePath = ^"../Player/Head/Camera3D"
@export var enabled := true
## Stop the main window's own 3D render while in stereo (saves GPU). Turn off if the view goes black.
@export var disable_main_3d := true
@export var bridge_settings_port := 9101

const SAVE_PATH := "user://hmd_settings.cfg"
const LABEL_W := 420.0
const DEFAULTS := {
	"ipd_m": 0.063, "eye_fov": 90.0, "k1": 0.22, "k2": 0.10,
	"lens_offset": 0.08, "zoom": 1.0, "distortion": true, "chroma": 0.0,
	"lens_on_phone": true, "overscan": 1.4, "timewarp": true, "latency_ms": 90.0,
	"vignette": 0.06,
}

var s := DEFAULTS.duplicate()
var _cam: Camera3D
var _eyes: Array[SubViewport] = []
var _eye_cams: Array[Camera3D] = []
var _rect: ColorRect
var _mat: ShaderMaterial
var _labels: Array[Label] = []
var _label_timer := 0.0
var _udp := PacketPeerUDP.new()
var _send_timer := 0.0


func _ready() -> void:
	layer = 100
	_cam = get_node_or_null(source_camera)
	if _cam == null:
		push_error("hmd_stereo: source camera not found at %s" % source_camera)
		set_process(false)
		return
	_load()
	_udp.set_dest_address("127.0.0.1", bridge_settings_port)
	for i in 2:
		var vp := SubViewport.new()
		vp.world_3d = get_viewport().world_3d          # same world as the game
		vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS
		add_child(vp)
		var c := Camera3D.new()
		vp.add_child(c)
		c.current = true
		_eyes.append(vp)
		_eye_cams.append(c)

	_mat = ShaderMaterial.new()
	_mat.shader = load("res://shaders/hmd_lens.gdshader")
	_mat.set_shader_parameter("left_eye", _eyes[0].get_texture())
	_mat.set_shader_parameter("right_eye", _eyes[1].get_texture())
	_rect = ColorRect.new()
	_rect.set_anchors_preset(Control.PRESET_FULL_RECT)
	_rect.mouse_filter = Control.MOUSE_FILTER_IGNORE   # let mouse look reach the player
	_rect.material = _mat
	add_child(_rect)

	for i in 2:   # one tuning label per eye so it's readable in the headset
		var l := Label.new()
		l.add_theme_font_size_override("font_size", 18)
		l.add_theme_color_override("font_color", Color(0.5, 1.0, 0.6))
		l.mouse_filter = Control.MOUSE_FILTER_IGNORE
		l.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
		l.size = Vector2(LABEL_W, 160)
		add_child(l)
		_labels.append(l)

	get_viewport().size_changed.connect(_resize)
	_resize()
	_apply()


func _process(delta: float) -> void:
	_send_timer -= delta
	if _send_timer <= 0.0:          # resend once a second so a reconnecting phone catches up
		_send_settings()
	if not enabled:
		return
	var half: float = s.ipd_m * 0.5
	for i in 2:
		var side := -half if i == 0 else half
		_eye_cams[i].global_transform = _cam.global_transform * Transform3D(Basis.IDENTITY, Vector3(side, 0, 0))
		_eye_cams[i].near = _cam.near
		_eye_cams[i].far = _cam.far
	if _label_timer > 0.0:
		_label_timer -= delta
		if _label_timer <= 0.0:
			for l in _labels:
				l.visible = false


func _unhandled_input(event: InputEvent) -> void:
	if not (event is InputEventKey and event.pressed and not event.echo):
		return
	var handled := true
	match event.physical_keycode:
		KEY_F2: enabled = not enabled
		KEY_F3: s.distortion = not s.distortion
		KEY_F4: s.lens_on_phone = not s.lens_on_phone
		KEY_T: s.timewarp = not s.timewarp
		KEY_BRACKETLEFT: s.lens_offset -= 0.005
		KEY_BRACKETRIGHT: s.lens_offset += 0.005
		KEY_MINUS: s.k1 -= 0.02
		KEY_EQUAL: s.k1 += 0.02
		KEY_COMMA: s.zoom -= 0.02
		KEY_PERIOD: s.zoom += 0.02
		KEY_9: s.eye_fov -= 5.0
		KEY_0: s.eye_fov += 5.0
		KEY_SEMICOLON: s.ipd_m -= 0.002
		KEY_APOSTROPHE: s.ipd_m += 0.002
		KEY_O: s.overscan -= 0.05
		KEY_P: s.overscan += 0.05
		KEY_K: s.latency_ms -= 10.0
		KEY_L: s.latency_ms += 10.0
		KEY_BACKSPACE: s = DEFAULTS.duplicate()
		_: handled = false
	if handled:
		s.zoom = clampf(s.zoom, 0.5, 2.0)
		s.eye_fov = clampf(s.eye_fov, 50.0, 130.0)
		s.ipd_m = clampf(s.ipd_m, 0.0, 0.12)
		s.overscan = clampf(s.overscan, 1.0, 2.0)
		s.latency_ms = clampf(s.latency_ms, 0.0, 300.0)
		_apply()
		_save()
		_show_settings()
		get_viewport().set_input_as_handled()


func _resize() -> void:
	var size := get_viewport().get_visible_rect().size
	var eye := Vector2i(int(size.x / 2.0), int(size.y))
	for vp in _eyes:
		vp.size = eye
	_mat.set_shader_parameter("eye_aspect", float(eye.x) / float(eye.y))
	_place_labels()


## Put each eye's label at the same spot relative to what THAT eye sees, so the two
## copies fuse into one. With the lens on the phone the labels are part of the image the
## phone bends, so the middle of each half is right. With the lens on the laptop, each
## eye's picture is shifted toward the nose by lens_offset, so the labels must follow.
func _place_labels() -> void:
	var size := get_viewport().get_visible_rect().size
	var half_w := size.x * 0.5
	for i in 2:
		var cx := half_w * (0.5 + i)
		if not s.lens_on_phone:
			cx += (1.0 if i == 0 else -1.0) * s.lens_offset * half_w
		_labels[i].position = Vector2(cx - LABEL_W * 0.5, size.y * 0.5 - 80.0)


## Camera FOV actually rendered. With the lens on the phone it's wider than what the
## lens shows (eye_fov), by the overscan factor (in tangent space).
func render_fov() -> float:
	if not s.lens_on_phone:
		return s.eye_fov
	return rad_to_deg(2.0 * atan(tan(deg_to_rad(s.eye_fov) * 0.5) * s.overscan))


func _apply() -> void:
	_rect.visible = enabled
	for vp in _eyes:
		vp.render_target_update_mode = SubViewport.UPDATE_ALWAYS if enabled else SubViewport.UPDATE_DISABLED
	for c in _eye_cams:
		c.fov = render_fov()
	if disable_main_3d:
		get_viewport().disable_3d = enabled
	for k in ["k1", "k2", "lens_offset", "zoom", "chroma"]:
		_mat.set_shader_parameter(k, s[k])
	# Immersive mode: the laptop sends the plain wide image; the phone bends it
	_mat.set_shader_parameter("distortion", s.distortion and not s.lens_on_phone)
	_place_labels()
	_send_settings()


func _send_settings() -> void:
	_send_timer = 1.0
	var msg := s.duplicate()
	msg["stereo"] = enabled
	msg["render_fov"] = render_fov()
	_udp.put_packet(JSON.stringify(msg).to_utf8_buffer())


func _show_settings() -> void:
	var text := ("STEREO %s   lens on %s   distortion %s   timewarp %s\n" +
		"separation %.3f  [ ]     distortion %.2f  - =     zoom %.2f  , .\n" +
		"fov %d  9 0     eye dist %d mm  ; '\n" +
		"overscan %.2f  O P     latency %d ms  K L") % [
		"ON" if enabled else "OFF", "PHONE" if s.lens_on_phone else "LAPTOP",
		"ON" if s.distortion else "OFF", "ON" if s.timewarp else "OFF",
		s.lens_offset, s.k1, s.zoom, int(s.eye_fov), int(round(s.ipd_m * 1000.0)),
		s.overscan, int(s.latency_ms)]
	print(text.replace("\n", " | "))
	for l in _labels:
		l.text = text.replace("     ", "\n")
		l.visible = enabled
	_label_timer = 3.0


func _save() -> void:
	var cfg := ConfigFile.new()
	for k in s:
		cfg.set_value("hmd", k, s[k])
	cfg.set_value("hmd", "enabled", enabled)
	cfg.save(SAVE_PATH)


func _load() -> void:
	var cfg := ConfigFile.new()
	if cfg.load(SAVE_PATH) != OK:
		return
	for k in DEFAULTS:
		s[k] = cfg.get_value("hmd", k, DEFAULTS[k])
	enabled = cfg.get_value("hmd", "enabled", enabled)

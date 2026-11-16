extends Node3D
## Phone head tracking for Godot 4.x (tested target: 4.7).
## Attach to a "Head" Node3D that holds the Camera3D (or the stereo rig).
## Keep body yaw / movement on the parent (mouse, keyboard, controller); this node
## only sets its own local rotation from the phone.
##
## Receives 24-byte UDP packets from bridge/hmd_bridge.py:
##   qx, qy, qz, qw (float32 LE) + phone timestamp ms (float64), or the text "RECENTER".

@export var listen_port := 9100
## 0 = raw (lowest latency). 0.3–0.6 hides compass jitter on phones without a gyro.
@export_range(0.0, 0.95) var smoothing := 0.0
## Optional InputMap action (e.g. bind R or a controller button) to recenter yaw.
@export var recenter_action := &"hmd_recenter"
## Turn off to let the mouse drive this node when no phone is connected.
@export var enabled := true
## Manual fixes if a direction still feels reversed in the headset
@export var invert_pitch := false
@export var invert_roll := false

var connected := false
var last_packet_msec := 0

var _udp := PacketPeerUDP.new()
# Web build (the phone itself runs the game): poses come from the page's sensor script
# (phone_host/game_web/hmd_web.js) as window.hmdHeadStr = "seq,recenterSeq,9 rotation numbers".
var _web := OS.has_feature("web")
var _web_seq := -1
var _web_recenter_seq := -1
# Phone sensor world is Z-up (X east-ish, Y north-ish). Godot is Y-up, -Z forward.
# Rotating -90° about X maps sensor +Z → Godot +Y and sensor +Y → Godot -Z.
var _world_fix := Basis(Vector3.RIGHT, -PI / 2)
var _raw := Basis.IDENTITY
var _yaw_offset := Basis.IDENTITY
var _needs_recenter := true


func _ready() -> void:
	if _web:
		return
	var err := _udp.bind(listen_port, "127.0.0.1")
	if err != OK:
		push_error("hmd_head: could not bind UDP port %d (error %d)" % [listen_port, err])


func _exit_tree() -> void:
	_udp.close()


func _process(_delta: float) -> void:
	if _web:
		_poll_web()
	# Drain everything; only the newest pose matters
	while not _web and _udp.get_available_packet_count() > 0:
		var pkt := _udp.get_packet()
		if pkt.size() == 24:
			var q := Quaternion(pkt.decode_float(0), pkt.decode_float(4),
					pkt.decode_float(8), pkt.decode_float(12)).normalized()
			# The phone's screen frame already matches Godot's camera axes
			# (X right, Y up, looking along -Z), so only the world needs fixing.
			_raw = _world_fix * Basis(q)
			last_packet_msec = Time.get_ticks_msec()
		elif pkt.get_string_from_ascii() == "RECENTER":
			_needs_recenter = true

	var timeout_ms := 1500 if _web else 500   # web: the page's sensor events and our frames interleave
	connected = Time.get_ticks_msec() - last_packet_msec < timeout_ms and last_packet_msec > 0
	if InputMap.has_action(recenter_action) and Input.is_action_just_pressed(recenter_action):
		_needs_recenter = true
	if not (enabled and connected):
		return
	if _needs_recenter:
		recenter()

	var target := (_yaw_offset * _raw).orthonormalized()
	if invert_pitch or invert_roll:
		var e := target.get_euler()   # YXZ order: x = pitch, y = yaw, z = roll
		if invert_pitch:
			e.x = -e.x
		if invert_roll:
			e.z = -e.z
		target = Basis.from_euler(e)
	if smoothing > 0.0:
		basis = basis.orthonormalized().slerp(target, 1.0 - smoothing)
	else:
		basis = target


## Web build: read the newest head rotation the page computed from the phone sensors.
## The page already gives camera->world in Godot axes (Y up, -Z forward), so no world fix.
func _poll_web() -> void:
	var v = JavaScriptBridge.eval("window.hmdHeadStr || ''", true)
	if not (v is String) or v == "":
		return
	var p: PackedStringArray = v.split(",")
	if p.size() < 11:
		return
	var seq := p[0].to_int()
	var rc := p[1].to_int()
	if rc != _web_recenter_seq:
		if _web_recenter_seq != -1:
			_needs_recenter = true
		_web_recenter_seq = rc
	if seq == _web_seq:
		return
	_web_seq = seq
	var r: Array[float] = []
	for i in range(2, 11):
		r.append(p[i].to_float())
	# rows of the rotation matrix -> Basis columns
	_raw = Basis(Vector3(r[0], r[3], r[6]), Vector3(r[1], r[4], r[7]), Vector3(r[2], r[5], r[8]))
	last_packet_msec = Time.get_ticks_msec()


## Makes the direction the player is facing right now "straight ahead" (yaw only).
## If the phone is pointing (nearly) straight up or down, e.g. lying on the desk when the
## game starts, its heading is meaningless; recentering then locked in a random turn so
## "forward" walked you sideways. Wait until it points within ~60 degrees of level.
func recenter() -> void:
	var forward := -_raw.z
	if Vector2(forward.x, forward.z).length() < 0.5:
		_needs_recenter = true   # try again next frame
		return
	var yaw := atan2(-forward.x, -forward.z)
	_yaw_offset = Basis(Vector3.UP, -yaw)
	_needs_recenter = false

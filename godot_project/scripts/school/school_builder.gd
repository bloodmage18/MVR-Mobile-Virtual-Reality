extends Node3D
class_name SchoolBuilder
## Builds the abandoned school from the text map below. Edit MAP to change the level:
## every character is one 1.5 m tile. Rows run north (top) to south (bottom).
##
##   #  wall              W  wall with a window      L  wall with lockers
##   B  wall with a blackboard                       C  wall with a clock
##   .  floor             d  student desk + chair    T  teacher desk / staff-room table
##   D  classroom door    X  the locked gate          k  possible key spot (one is chosen)
##   S  player start      O  outside (assembly ground, no roof)

signal built

const TILE := 1.5
const WALL_H := 3.2
const DOOR_W := 1.3
const DOOR_H := 2.3
const WALLS := "#WLBC"
const FLOORS := ".dTkSDXO"

const MAP := [
	"OOOOOOOOOOOOOOOOOOOOOOOOO",
	"OOOOOOOOOOOOOOOOOOOOOOOOO",
	"OOOOOOOOOOOOOOOOOOOOOOOOO",
	"###BBB######X######BBB###",
	"#....T....#...#....T....#",
	"W.........#...#.........W",
	"#.d.d.d.d.L...L.d.d.d.d.#",
	"W.d.d.d.d.#...C.d.d.d.d.W",
	"#.......k.D...D.........#",
	"###BBB#####...#####BBB###",
	"#....T....#...#....T....#",
	"W.........#...#.........W",
	"#.d.d.d.d.L...L.d.d.d.d.#",
	"W.d.d.d.d.#...#.d.d.d.d.W",
	"#.........D...D......k..#",
	"###BBB#####...#####BBB###",
	"#....T....#...#.........#",
	"W.........#...#..TTTT...W",
	"#.d.d.d.d.L...L..TTTT...#",
	"W.d.d.d.d.#...#.....k...W",
	"#.........D...D.........#",
	"###########...###########",
	"##########.....##########",
	"##########..S..##########",
	"#########################",
]

# Room names in map order (top-left, top-right, middle-left, ...). Last one is the staff room.
const ROOM_NAMES := ["Form 2A Visual Arts", "Form 1B General Arts", "Form 3C Science",
		"Form 2B Business", "Form 1A Home Economics", "Staff Room"]
const BOARD_TEXT := [
	"VISUAL ARTS\nPicture making: sketch this classroom",
	"CORE MATHEMATICS\nHomework p. 42, Q1-Q10",
	"ENGLISH LANGUAGE\nComprehension: \"The Empty School\"",
	"SOCIAL STUDIES\nTopic: Our Community",
	"GKA\nBring your sketchbooks",
	"ASSEMBLY AT MIDNIGHT\nEVERYONE",
	"PREP TIME 4PM - 5PM\nNo one stays behind",
	"WHY DID YOU STAY?",
	"INTEGRATED SCIENCE\nThe water cycle",
	"Duty roster: Form 2A",
]
# Corridor fixtures by row: ON, FLICKER, OFF. The far end stays lit to draw you north.
const CORRIDOR_LIGHTS := {23: 0, 20: 1, 17: 2, 14: 0, 11: 1, 8: 2, 5: 0}

var rng := RandomNumberGenerator.new()
var room_of := {}            # Vector2i tile -> component id
var rooms := {}              # id -> {name, cells, door, center}
var corridor_id := -1
var outside_id := -1
var doors: Array[SchoolDoor] = []
var exit_gate: SchoolDoor
var corridor_lights: Array[FluoroLight] = []
var locker_spots: Array[Vector3] = []
var clock_labels: Array[Label3D] = []
var key_node: KeyPickup
var start_position := Vector3.ZERO
var exit_line_z := 0.0
var corridor_x := 0.0
var speaker_position := Vector3.ZERO

var _m := {}
var _board_i := 0


func _ready() -> void:
	rng.randomize()
	_make_materials()
	_check_map()
	_find_components()
	_build_floors_and_ceilings()
	_build_walls()
	_build_furniture()
	_build_doors()
	_build_lights()
	_place_key()
	built.emit()


# ── map helpers ──────────────────────────────────────────────────────────────

func cell(c: int, r: int) -> String:
	if r < 0 or r >= MAP.size():
		return "#"
	var row: String = MAP[r]
	if c < 0 or c >= row.length():
		return "#"
	return row[c]


func is_floor(ch: String) -> bool:
	return FLOORS.contains(ch)


func tile_center(c: int, r: int) -> Vector3:
	return Vector3((c + 0.5) * TILE, 0.0, (r + 0.5) * TILE)


func world_to_tile(p: Vector3) -> Vector2i:
	return Vector2i(floori(p.x / TILE), floori(p.z / TILE))


func room_at(p: Vector3) -> int:
	return room_of.get(world_to_tile(p), -1)


func is_classroom(id: int) -> bool:
	return rooms.has(id) and id != corridor_id and id != outside_id


func _check_map() -> void:
	var w: int = MAP[0].length()
	for r in MAP.size():
		if MAP[r].length() != w:
			push_error("School map row %d is %d wide, expected %d" % [r, MAP[r].length(), w])


# ── rooms: connected floor areas, doors are the boundaries ───────────────────

func _find_components() -> void:
	var next_id := 0
	for r in MAP.size():
		for c in MAP[r].length():
			var ch := cell(c, r)
			var t := Vector2i(c, r)
			if not is_floor(ch) or ch == "D" or ch == "X" or room_of.has(t):
				continue
			var cells: Array[Vector2i] = []
			var stack: Array[Vector2i] = [t]
			room_of[t] = next_id
			var is_corridor := false
			var is_outside := false
			while not stack.is_empty():
				var cur: Vector2i = stack.pop_back()
				cells.append(cur)
				var cc := cell(cur.x, cur.y)
				is_corridor = is_corridor or cc == "S"
				is_outside = is_outside or cc == "O"
				for d in [Vector2i(1, 0), Vector2i(-1, 0), Vector2i(0, 1), Vector2i(0, -1)]:
					var n: Vector2i = cur + d
					var nc := cell(n.x, n.y)
					if is_floor(nc) and nc != "D" and nc != "X" and not room_of.has(n):
						room_of[n] = next_id
						stack.append(n)
			var center := Vector3.ZERO
			for cl in cells:
				center += tile_center(cl.x, cl.y)
			center /= cells.size()
			rooms[next_id] = {"name": "", "cells": cells, "door": null, "center": center}
			if is_corridor:
				corridor_id = next_id
				rooms[next_id].name = "Corridor"
			elif is_outside:
				outside_id = next_id
				rooms[next_id].name = "Assembly Ground"
			next_id += 1
	var i := 0
	for id in rooms:
		if is_classroom(id):
			rooms[id].name = ROOM_NAMES[i] if i < ROOM_NAMES.size() else "Classroom %d" % (i + 1)
			i += 1


# ── geometry ─────────────────────────────────────────────────────────────────

func _build_floors_and_ceilings() -> void:
	for r in MAP.size():
		var c := 0
		var w: int = MAP[r].length()
		while c < w:
			if not is_floor(cell(c, r)):
				c += 1
				continue
			var outside := cell(c, r) == "O"
			var start := c
			while c < w and is_floor(cell(c, r)) and (cell(c, r) == "O") == outside:
				c += 1
			var length := (c - start) * TILE
			var mid_x := (start * TILE) + length * 0.5
			var z := (r + 0.5) * TILE
			_box(Vector3(length, 0.2, TILE), Vector3(mid_x, -0.1, z),
					_m.ground if outside else _m.floor, true)
			if not outside:
				_box(Vector3(length, 0.2, TILE), Vector3(mid_x, WALL_H + 0.1, z), _m.ceiling, false)


func _build_walls() -> void:
	for r in MAP.size():
		var c := 0
		var w: int = MAP[r].length()
		while c < w:
			if not WALLS.contains(cell(c, r)):
				c += 1
				continue
			var start := c
			while c < w and WALLS.contains(cell(c, r)):
				c += 1
			var length := (c - start) * TILE
			var mid := Vector3(start * TILE + length * 0.5, 0.0, (r + 0.5) * TILE)
			# Two-tone school paint: dark lower band, pale upper wall. One collision box.
			var body := StaticBody3D.new()
			body.position = mid
			add_child(body)
			_mesh(body, Vector3(length, 1.1, TILE), Vector3(0, 0.55, 0), _m.wall_low)
			_mesh(body, Vector3(length, WALL_H - 1.1, TILE), Vector3(0, 1.1 + (WALL_H - 1.1) * 0.5, 0), _m.wall_up)
			_collider(body, Vector3(length, WALL_H, TILE), Vector3(0, WALL_H * 0.5, 0))
		# decorations on the faces of special wall tiles
	for r in MAP.size():
		for c in MAP[r].length():
			var ch := cell(c, r)
			if "WLBC".contains(ch):
				_decorate(c, r, ch)
	# lintels over door openings
	for r in MAP.size():
		for c in MAP[r].length():
			if cell(c, r) == "D" or cell(c, r) == "X":
				var p := tile_center(c, r)
				_box(Vector3(TILE, WALL_H - DOOR_H, TILE), p + Vector3(0, DOOR_H + (WALL_H - DOOR_H) * 0.5, 0), _m.wall_up, false)


func _decorate(c: int, r: int, ch: String) -> void:
	for d in [Vector2i(1, 0), Vector2i(-1, 0), Vector2i(0, 1), Vector2i(0, -1)]:
		var nc := cell(c + d.x, r + d.y)
		if not is_floor(nc) or nc == "O" or nc == "D" or nc == "X":
			continue   # nothing on faces pointing outside or into a doorway
		var dir := Vector3(d.x, 0, d.y)
		var face := tile_center(c, r) + dir * (TILE * 0.5)
		var rot := atan2(dir.x, dir.z)
		var tangent := Vector3(dir.z, 0, -dir.x)
		match ch:
			"L":
				# a bank of three lockers sticking out into the corridor
				var depth := 0.45
				var body := StaticBody3D.new()
				body.position = face + dir * (depth * 0.5)
				body.rotation.y = rot
				add_child(body)
				for i in 3:
					var col: Material = _m.locker if rng.randf() > 0.2 else _m.locker_dent
					_mesh(body, Vector3(0.46, 1.9, depth), Vector3((i - 1) * 0.47, 0.95, 0), col)
					_mesh(body, Vector3(0.3, 0.02, 0.01), Vector3((i - 1) * 0.47, 1.65, depth * 0.5), _m.dark)
				_collider(body, Vector3(1.42, 1.9, depth), Vector3(0, 0.95, 0))
				locker_spots.append(face + dir * 0.3 + Vector3.UP * 1.2)
			"B":
				_box(Vector3(TILE - 0.04, 1.2, 0.05), face + dir * 0.03 + Vector3.UP * 1.55, _m.board, false, rot)
				_box(Vector3(TILE - 0.04, 0.05, 0.08), face + dir * 0.05 + Vector3.UP * 0.93, _m.wood, false, rot)
				# one chalk message per board, written on the middle tile of the run
				var left := cell(c - int(tangent.x), r - int(tangent.z))
				var right := cell(c + int(tangent.x), r + int(tangent.z))
				if left == "B" and right == "B":
					var lab := _label(BOARD_TEXT[_board_i % BOARD_TEXT.size()], 48, Color(0.85, 0.85, 0.8, 0.85))
					_board_i += 1
					lab.position = face + dir * 0.06 + Vector3.UP * 1.6
					lab.rotation.y = rot
			"W":
				_box(Vector3(1.1, 1.3, 0.04), face + dir * 0.02 + Vector3.UP * 1.75, _m.glass, false, rot)
				_box(Vector3(1.2, 0.06, 0.06), face + dir * 0.04 + Vector3.UP * 1.75, _m.frame, false, rot)
				_box(Vector3(0.06, 1.4, 0.06), face + dir * 0.04 + Vector3.UP * 1.75, _m.frame, false, rot)
			"C":
				_box(Vector3(0.5, 0.5, 0.05), face + dir * 0.03 + Vector3.UP * 2.45, _m.clock, false, rot)
				var lab := _label("--:--", 72, Color(0.15, 0.12, 0.1))
				lab.position = face + dir * 0.07 + Vector3.UP * 2.45
				lab.rotation.y = rot
				clock_labels.append(lab)


func _build_furniture() -> void:
	for r in MAP.size():
		for c in MAP[r].length():
			var ch := cell(c, r)
			var p := tile_center(c, r)
			if ch == "d":
				_student_desk(p)
			elif ch == "T":
				var joined := cell(c - 1, r) == "T" or cell(c + 1, r) == "T" or cell(c, r - 1) == "T" or cell(c, r + 1) == "T"
				if joined:   # staff-room table: tiles join into one long table
					_box(Vector3(TILE, 0.06, TILE), p + Vector3(0, 0.76, 0), _m.wood, false)
					_box(Vector3(TILE * 0.9, 0.72, TILE * 0.9), p + Vector3(0, 0.36, 0), _m.wood_dark, true)
				else:
					_box(Vector3(1.4, 0.78, 0.7), p + Vector3(0, 0.39, -0.2), _m.wood_dark, true)
					_box(Vector3(0.45, 0.05, 0.35), p + Vector3(0.3, 0.8, -0.2), _m.paper, false)


func _student_desk(p: Vector3) -> void:
	# Desks face north (toward the blackboard). Some are pushed around; some chairs are knocked over.
	var jitter := Vector3(rng.randf_range(-0.12, 0.12), 0, rng.randf_range(-0.1, 0.1))
	var yaw := rng.randf_range(-0.12, 0.12) + (0.5 if rng.randf() < 0.1 else 0.0)
	var body := StaticBody3D.new()
	body.position = p + jitter + Vector3(0, 0, -0.15)
	body.rotation.y = yaw
	add_child(body)
	_mesh(body, Vector3(0.9, 0.04, 0.55), Vector3(0, 0.74, 0), _m.wood)
	_mesh(body, Vector3(0.86, 0.5, 0.03), Vector3(0, 0.45, -0.25), _m.metal)
	for sx in [-0.42, 0.42]:
		_mesh(body, Vector3(0.03, 0.72, 0.5), Vector3(sx, 0.36, 0), _m.metal)
	_collider(body, Vector3(0.9, 0.76, 0.55), Vector3(0, 0.38, 0))
	# chair behind the desk
	var chair := StaticBody3D.new()
	chair.position = body.position + Vector3(0, 0, 0.5)
	chair.rotation.y = yaw + rng.randf_range(-0.3, 0.3)
	add_child(chair)
	if rng.randf() < 0.15:
		chair.rotation.z = PI * 0.5   # knocked over
		chair.position.y = 0.21
	_mesh(chair, Vector3(0.42, 0.04, 0.42), Vector3(0, 0.45, 0), _m.wood)
	_mesh(chair, Vector3(0.42, 0.4, 0.04), Vector3(0, 0.67, 0.2), _m.wood)
	_mesh(chair, Vector3(0.4, 0.45, 0.4), Vector3(0, 0.22, 0), _m.metal_frame)
	_collider(chair, Vector3(0.42, 0.45, 0.42), Vector3(0, 0.22, 0))


func _build_doors() -> void:
	for r in MAP.size():
		for c in MAP[r].length():
			var ch := cell(c, r)
			if ch != "D" and ch != "X":
				continue
			var p := tile_center(c, r)
			var door := SchoolDoor.new()
			var east_west := is_floor(cell(c - 1, r)) and is_floor(cell(c + 1, r))
			if east_west:
				# passage runs west-east, so the door panel spans north-south
				door.position = p + Vector3(0, 0, -DOOR_W * 0.5)
				door.rotation.y = -PI * 0.5
			else:
				door.position = p + Vector3(-DOOR_W * 0.5, 0, 0)
			add_child(door)
			if ch == "X":
				door.kind = "gate"
				door.locked = true
				door.needs_key = true
				door.build(DOOR_W, DOOR_H, _m.gate, _m.metal)
				exit_gate = door
				exit_line_z = r * TILE - 0.3
				_exit_sign(p + Vector3(0, DOOR_H + 0.35, TILE * 0.5 + 0.02))
			else:
				door.build(DOOR_W, DOOR_H, _m.door, _m.metal)
				# which classroom does this door belong to?
				for side in [Vector2i(-1, 0), Vector2i(1, 0), Vector2i(0, -1), Vector2i(0, 1)]:
					var id: int = room_of.get(Vector2i(c, r) + side, -1)
					if is_classroom(id):
						door.room_id = id
						rooms[id].door = door
				# abandoned: about half the doors were left ajar
				if rng.randf() < 0.5:
					door.set_angle_instant(rng.randf_range(35.0, 95.0))
				doors.append(door)


func _build_lights() -> void:
	var buzz := SFX.load_sound("fluorescent_buzz")
	var corridor_cols := []
	for cl in rooms[corridor_id].cells:
		if not corridor_cols.has(cl.x):
			corridor_cols.append(cl.x)
	corridor_cols.sort()
	corridor_x = (corridor_cols[corridor_cols.size() / 2] + 0.5) * TILE
	for row in CORRIDOR_LIGHTS:
		var f := FluoroLight.new()
		f.mode = CORRIDOR_LIGHTS[row]
		f.position = Vector3(corridor_x, WALL_H - 0.04, (row + 0.5) * TILE)
		add_child(f)
		f.build(buzz)
		corridor_lights.append(f)
	speaker_position = Vector3(corridor_x, WALL_H - 0.3, MAP.size() * TILE * 0.5)

	# moonlight through the classroom windows
	for id in rooms:
		if not is_classroom(id):
			continue
		var cells: Array = rooms[id].cells
		var min_x := INF
		var max_x := -INF
		for cl in cells:
			min_x = minf(min_x, cl.x)
			max_x = maxf(max_x, cl.x)
		var west_side: bool = rooms[id].center.x < corridor_x
		var lx := (min_x + 1.0) * TILE if west_side else (max_x) * TILE
		var moon := OmniLight3D.new()
		moon.light_color = Color(0.45, 0.55, 0.95)
		moon.light_energy = 0.45
		moon.omni_range = 8.0
		moon.position = Vector3(lx, 2.4, rooms[id].center.z)
		add_child(moon)

	# the assembly ground outside
	if outside_id >= 0:
		var c: Vector3 = rooms[outside_id].center
		var sky := OmniLight3D.new()
		sky.light_color = Color(0.5, 0.6, 1.0)
		sky.light_energy = 0.6
		sky.omni_range = 18.0
		sky.position = c + Vector3(0, 7, -2)
		add_child(sky)
		_box(Vector3(0.08, 7.0, 0.08), c + Vector3(3, 3.5, -1), _m.metal, true)   # flag pole


func _place_key() -> void:
	var spots: Array[Vector2i] = []
	for r in MAP.size():
		for c in MAP[r].length():
			if cell(c, r) == "k":
				spots.append(Vector2i(c, r))
	if spots.is_empty():
		push_error("School map has no 'k' key spot")
		return
	var s: Vector2i = spots[rng.randi_range(0, spots.size() - 1)]
	var p := tile_center(s.x, s.y)
	_box(Vector3(0.5, 0.8, 0.4), p + Vector3(0, 0.4, 0), _m.wood_dark, true)   # small cabinet
	key_node = KeyPickup.new()
	key_node.position = p + Vector3(0, 0.84, 0)
	key_node.rotation.y = rng.randf() * TAU   # lying flat on the cabinet
	add_child(key_node)
	key_node.build()
	for r in MAP.size():
		for c in MAP[r].length():
			if cell(c, r) == "S":
				start_position = tile_center(c, r) + Vector3(0, 0.95, 0)


func _exit_sign(pos: Vector3) -> void:
	_box(Vector3(0.7, 0.25, 0.06), pos, _m.exit_sign, false)
	var lab := _label("EXIT", 64, Color(0.75, 1.0, 0.8))
	lab.position = pos + Vector3(0, 0, 0.04)
	var glow := OmniLight3D.new()
	glow.light_color = Color(0.3, 1.0, 0.45)
	glow.light_energy = 0.7
	glow.omni_range = 4.5
	glow.position = pos + Vector3(0, -0.3, 0.4)
	add_child(glow)


# ── tiny scene-building helpers ──────────────────────────────────────────────

func _box(size: Vector3, pos: Vector3, mat: Material, collide: bool, rot_y := 0.0) -> Node3D:
	if collide:
		var body := StaticBody3D.new()
		body.position = pos
		body.rotation.y = rot_y
		add_child(body)
		_mesh(body, size, Vector3.ZERO, mat)
		_collider(body, size, Vector3.ZERO)
		return body
	var mi := _mesh(self, size, pos, mat)
	mi.rotation.y = rot_y
	return mi


func _mesh(parent: Node, size: Vector3, pos: Vector3, mat: Material) -> MeshInstance3D:
	var mi := MeshInstance3D.new()
	var bm := BoxMesh.new()
	bm.size = size
	mi.mesh = bm
	mi.material_override = mat
	mi.position = pos
	parent.add_child(mi)
	return mi


func _collider(body: Node, size: Vector3, pos: Vector3) -> void:
	var cs := CollisionShape3D.new()
	var shape := BoxShape3D.new()
	shape.size = size
	cs.shape = shape
	cs.position = pos
	body.add_child(cs)


func _label(text: String, size: int, color: Color) -> Label3D:
	var l := Label3D.new()
	l.text = text
	l.font_size = size
	l.pixel_size = 0.004
	l.modulate = color
	l.outline_size = 0
	l.double_sided = false
	add_child(l)
	return l


func _make_materials() -> void:
	_m.floor = _mat(Color(0.23, 0.25, 0.23), 0.85)        # worn terrazzo
	_m.ceiling = _mat(Color(0.42, 0.41, 0.38), 0.95)
	_m.ground = _mat(Color(0.16, 0.15, 0.13), 1.0)        # dusty assembly ground
	_m.wall_low = _mat(Color(0.16, 0.27, 0.25), 0.8)      # school green-blue band
	_m.wall_up = _mat(Color(0.55, 0.52, 0.44), 0.95)      # faded cream
	_m.locker = _mat(Color(0.16, 0.26, 0.38), 0.6, 0.4)
	_m.locker_dent = _mat(Color(0.13, 0.2, 0.3), 0.75, 0.3)
	_m.board = _mat(Color(0.07, 0.14, 0.1), 0.9)
	_m.wood = _mat(Color(0.36, 0.25, 0.16), 0.8)
	_m.wood_dark = _mat(Color(0.24, 0.16, 0.1), 0.8)
	_m.metal = _mat(Color(0.3, 0.3, 0.32), 0.5, 0.7)
	_m.metal_frame = _mat(Color(0.2, 0.2, 0.22), 0.5, 0.7)
	_m.door = _mat(Color(0.3, 0.2, 0.12), 0.85)
	_m.gate = _mat(Color(0.22, 0.17, 0.14), 0.7, 0.6)
	_m.dark = _mat(Color(0.03, 0.03, 0.03), 1.0)
	_m.paper = _mat(Color(0.6, 0.58, 0.5), 1.0)
	_m.frame = _mat(Color(0.18, 0.17, 0.16), 0.8)
	_m.clock = _mat(Color(0.75, 0.72, 0.65), 0.8)
	_m.glass = _mat(Color(0.05, 0.08, 0.14), 0.1)
	_m.glass.emission_enabled = true
	_m.glass.emission = Color(0.25, 0.35, 0.6)
	_m.glass.emission_energy_multiplier = 0.4
	_m.exit_sign = _mat(Color(0.05, 0.3, 0.1), 0.5)
	_m.exit_sign.emission_enabled = true
	_m.exit_sign.emission = Color(0.2, 0.9, 0.35)
	_m.exit_sign.emission_energy_multiplier = 1.5


func _mat(color: Color, rough := 0.9, metal := 0.0) -> StandardMaterial3D:
	var m := StandardMaterial3D.new()
	m.albedo_color = color
	m.roughness = rough
	m.metallic = metal
	return m

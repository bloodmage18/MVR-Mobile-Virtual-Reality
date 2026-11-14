extends SceneTree
## Dev tool: renders the school from a few viewpoints (flat view) and saves PNGs.
##   Godot --path godot_project --script res://tools/screenshot_tour.gd -- <output folder>

const SHOTS := [
	# position, yaw (deg), pitch (deg), name
	[Vector3(18.75, 0.95, 35.25), 0.0, 0.0, "1_start_corridor"],
	[Vector3(18.75, 0.95, 21.75), 0.0, 5.0, "2_mid_corridor"],
	[Vector3(15.75, 0.95, 21.75), 90.0, -5.0, "3_into_classroom"],
	[Vector3(8.25, 0.95, 12.75), 0.0, 3.0, "4_classroom_board"],
	[Vector3(30.75, 0.95, 30.75), 30.0, -10.0, "5_staff_room"],
	[Vector3(18.75, 0.95, 9.75), 0.0, 12.0, "6_exit_gate"],
]


func _initialize() -> void:
	var out: String = OS.get_cmdline_user_args()[0] if OS.get_cmdline_user_args().size() > 0 else "user://"
	var scene: Node = load("res://school.tscn").instantiate()
	root.add_child(scene)
	current_scene = scene
	await process_frame
	var stereo := scene.get_node("HMDStereo")
	stereo.enabled = false
	stereo._apply()
	var player := scene.get_node("Player")
	for s in SHOTS:
		player.global_position = s[0]
		player.rotation.y = deg_to_rad(s[1])
		player.set("_pitch_deg", s[2])
		for i in 25:
			await process_frame
		var img := root.get_texture().get_image()
		img.save_png(out.path_join("%s.png" % s[3]))
		print("saved ", s[3])
	quit()

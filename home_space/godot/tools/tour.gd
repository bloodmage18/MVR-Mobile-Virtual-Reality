extends SceneTree
## Dev tool: renders flat (non-stereo) screenshots of a scene from given viewpoints.
##   Godot --path home_space/godot --script res://tools/tour.gd -- <out_dir> [scene] [x,y,z,yaw,pitch,name ...]
## y is the FEET height (floor level); eye height is added automatically.
## yaw 0 looks toward -Z (north), 90 looks toward -X (west), -90 toward +X (east), 180 toward +Z (south).
## Without viewpoints it uses a default tour of all three library floors.

const DEFAULT := [
	"0,0,6,0,0,f1_entrance_north", "-6,0,0,-90,0,f1_west_east", "4,0,4,135,0,f1_east_southwest",
	"0,4,4,0,0,f2_north", "-6,4,-4,-135,0,f2_corner", "4,4,0,90,0,f2_west",
	"0,8,6,0,0,f3_screen", "0,8,-5,180,0,f3_seats", "8,0,1,0,10,stairs_bottom",
]


func _initialize() -> void:
	var args := OS.get_cmdline_user_args()
	var out: String = args[0] if args.size() > 0 else "user://"
	var scene_path: String = args[1] if args.size() > 1 else "res://library.tscn"
	var shots: Array = Array(args.slice(2)) if args.size() > 2 else DEFAULT
	var scene: Node = load(scene_path).instantiate()
	root.add_child(scene)
	current_scene = scene
	await process_frame
	var stereo := scene.get_node_or_null("HMDStereo")
	if stereo:
		stereo.enabled = false
		stereo._apply()
	var player := scene.get_node("Player")
	player.set_physics_process(false)
	for s in shots:
		var p: PackedStringArray = String(s).split(",")
		player.global_position = Vector3(p[0].to_float(), p[1].to_float() + 0.95, p[2].to_float())
		player.rotation.y = deg_to_rad(p[3].to_float())
		player.get_node("Head").rotation = Vector3(deg_to_rad(p[4].to_float()), 0, 0)
		for i in 20:
			await process_frame
		root.get_texture().get_image().save_png(out.path_join("%s.png" % p[5]))
		print("saved ", p[5])
	quit()

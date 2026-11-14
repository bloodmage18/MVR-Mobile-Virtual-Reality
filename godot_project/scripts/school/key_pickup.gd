extends StaticBody3D
class_name KeyPickup
## The gate key. A faint glint so the flashlight can find it.

signal picked_up

var _keys_sound: AudioStream


func build() -> void:
	_keys_sound = SFX.load_sound("keys")
	var metal := StandardMaterial3D.new()
	metal.albedo_color = Color(0.75, 0.65, 0.35)
	metal.metallic = 0.9
	metal.roughness = 0.35
	metal.emission_enabled = true
	metal.emission = Color(0.9, 0.75, 0.3)
	metal.emission_energy_multiplier = 0.35

	var ring := MeshInstance3D.new()
	var torus := TorusMesh.new()
	torus.inner_radius = 0.02
	torus.outer_radius = 0.035
	ring.mesh = torus
	ring.material_override = metal
	add_child(ring)
	var blade := MeshInstance3D.new()
	var bm := BoxMesh.new()
	bm.size = Vector3(0.11, 0.008, 0.022)
	blade.mesh = bm
	blade.position = Vector3(0.085, 0, 0)
	blade.material_override = metal
	add_child(blade)

	var glint := OmniLight3D.new()
	glint.light_color = Color(1.0, 0.85, 0.5)
	glint.light_energy = 0.25
	glint.omni_range = 0.7
	glint.position = Vector3(0, 0.1, 0)
	add_child(glint)

	var cs := CollisionShape3D.new()
	var sphere := SphereShape3D.new()
	sphere.radius = 0.3        # generous, easy to target with a wobbly head
	cs.shape = sphere
	add_child(cs)


func prompt(_player: Node) -> String:
	return "Take the key"


func interact(player: Node) -> String:
	player.set("has_key", true)
	SFX.play_2d(get_tree().current_scene, _keys_sound, -4.0)
	picked_up.emit()
	queue_free()
	return "You found the gate key."

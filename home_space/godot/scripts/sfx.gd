class_name SFX
## Small helpers for one-shot and looping sounds.


## Plays a positional sound once at `pos`, then frees the player.
static func play_at(parent: Node, stream: AudioStream, pos: Vector3, volume_db := 0.0,
		pitch := 1.0, unit_size := 3.0) -> AudioStreamPlayer3D:
	if stream == null or parent == null:
		return null
	var p := AudioStreamPlayer3D.new()
	p.stream = stream
	p.volume_db = volume_db
	p.pitch_scale = pitch
	p.unit_size = unit_size
	p.max_distance = 60.0
	parent.add_child(p)
	p.global_position = pos
	p.play()
	p.finished.connect(p.queue_free)
	return p


## Plays a non-positional sound once (UI, the player's own body).
static func play_2d(parent: Node, stream: AudioStream, volume_db := 0.0, pitch := 1.0) -> AudioStreamPlayer:
	if stream == null or parent == null:
		return null
	var p := AudioStreamPlayer.new()
	p.stream = stream
	p.volume_db = volume_db
	p.pitch_scale = pitch
	parent.add_child(p)
	p.play()
	p.finished.connect(p.queue_free)
	return p


## A copy of a WAV stream that loops end to end.
static func looped(stream: AudioStream) -> AudioStream:
	var wav := stream as AudioStreamWAV
	if wav == null:
		return stream
	var s := wav.duplicate() as AudioStreamWAV
	s.loop_mode = AudioStreamWAV.LOOP_FORWARD
	s.loop_begin = 0
	s.loop_end = int(s.get_length() * s.mix_rate)
	return s


static func load_sound(name: String) -> AudioStream:
	var path := "res://audio/%s.wav" % name
	return load(path) if ResourceLoader.exists(path) else null

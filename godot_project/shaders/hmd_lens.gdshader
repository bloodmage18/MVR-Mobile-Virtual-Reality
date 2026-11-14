shader_type canvas_item;
// Side-by-side stereo for phone VR headsets.
// Left half of the screen shows left_eye, right half shows right_eye, each pre-distorted
// with barrel distortion so the headset's magnifying lens (pincushion) cancels it out.

uniform sampler2D left_eye : filter_linear, repeat_disable;
uniform sampler2D right_eye : filter_linear, repeat_disable;
uniform bool distortion = true;
uniform float k1 = 0.22;            // main barrel strength
uniform float k2 = 0.10;            // edge barrel strength
uniform float lens_offset = 0.08;   // lens centre shift toward the nose (fraction of one eye's width)
uniform float zoom = 1.0;           // >1 shows less of the image (bigger), <1 shows more
uniform float eye_aspect = 1.111;   // eye width / height in pixels (800 / 720)
uniform float chroma = 0.0;         // red/blue split to cancel lens colour fringing (try 0.01–0.03)

vec2 warp(vec2 d, float scale) {
	d.x *= eye_aspect;
	float r2 = dot(d, d);
	d *= (1.0 + k1 * r2 + k2 * r2 * r2) * scale / zoom;
	d.x /= eye_aspect;
	return d + vec2(0.5);
}

bool outside(vec2 s) {
	return s.x < 0.0 || s.x > 1.0 || s.y < 0.0 || s.y > 1.0;
}

void fragment() {
	bool right = UV.x >= 0.5;
	vec2 e = vec2(fract(UV.x * 2.0), UV.y);   // 0..1 inside this eye's half
	if (!distortion) {
		COLOR = right ? texture(right_eye, e) : texture(left_eye, e);
	} else {
		// Lens centre sits closer to the nose than the middle of each half
		vec2 c = vec2(0.5 + (right ? -lens_offset : lens_offset), 0.5);
		vec2 d = e - c;
		vec2 sg = warp(d, 1.0);
		if (outside(sg)) {
			COLOR = vec4(0.0, 0.0, 0.0, 1.0);
		} else if (chroma <= 0.0) {
			COLOR = right ? texture(right_eye, sg) : texture(left_eye, sg);
		} else {
			vec2 sr = warp(d, 1.0 - chroma);
			vec2 sb = warp(d, 1.0 + chroma);
			float r = right ? texture(right_eye, sr).r : texture(left_eye, sr).r;
			float g = right ? texture(right_eye, sg).g : texture(left_eye, sg).g;
			float b = right ? texture(right_eye, sb).b : texture(left_eye, sb).b;
			COLOR = vec4(r, g, b, 1.0);
		}
	}
}

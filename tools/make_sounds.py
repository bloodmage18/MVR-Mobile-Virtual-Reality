"""
Generates every sound the school level uses, from scratch (no downloads, no licences).

    .venv\\Scripts\\python tools\\make_sounds.py

Writes 16-bit mono WAVs to godot_project/audio/. Re-run after tweaking anything here;
Godot re-imports them when the editor next opens (or with --import).
"""

from pathlib import Path
import wave

import numpy as np

SR = 22050
OUT = Path(__file__).resolve().parent.parent / "godot_project" / "audio"
rng = np.random.default_rng(7)


# ── building blocks ──────────────────────────────────────────────────────────

def t(sec):
    return np.arange(int(sec * SR)) / SR


def noise(sec):
    return rng.uniform(-1, 1, int(sec * SR))


def lowpass(x, cutoff):
    """One-pole low-pass. cutoff may be a number or an array (sweeps)."""
    cutoff = np.broadcast_to(np.asarray(cutoff, dtype=float), x.shape)
    a = 1.0 - np.exp(-2 * np.pi * cutoff / SR)
    y = np.empty_like(x)
    acc = 0.0
    for i in range(len(x)):
        acc += a[i] * (x[i] - acc)
        y[i] = acc
    return y


def highpass(x, cutoff):
    return x - lowpass(x, cutoff)


def bandpass(x, lo, hi):
    return lowpass(highpass(x, lo), hi)


def env(n, attack, decay, sustain_sec=0.0):
    """Attack (s), hold, then exponential decay with time-constant `decay` (s)."""
    tt = np.arange(n) / SR
    e = np.minimum(tt / max(attack, 1e-4), 1.0)
    after = np.clip(tt - attack - sustain_sec, 0, None)
    return e * np.exp(-after / decay)


def echo(x, delays=(0.09, 0.17, 0.29), gain=0.35):
    """Cheap room reverb: a few decaying taps."""
    y = np.concatenate([x, np.zeros(int((max(delays) + 0.1) * SR))])
    for i, d in enumerate(delays):
        k = int(d * SR)
        y[k:k + len(x)] += x * gain ** (i + 1)
    return y


def norm(x, peak=0.9):
    m = np.max(np.abs(x)) or 1.0
    return x / m * peak


def fade(x, sec=0.01):
    n = min(int(sec * SR), len(x) // 2)
    ramp = np.linspace(0, 1, n)
    x = x.copy()
    x[:n] *= ramp
    x[-n:] *= ramp[::-1]
    return x


def loopable(x, xfade=0.5):
    """Crossfade the tail into the head so the file loops without a click."""
    n = int(xfade * SR)
    head, tail = x[:n], x[-n:]
    ramp = np.linspace(0, 1, n)
    body = x[n:-n].copy()
    return np.concatenate([tail * (1 - ramp) + head * ramp, body])


def save(name, x, peak=0.9):
    OUT.mkdir(parents=True, exist_ok=True)
    pcm = (norm(x, peak) * 32767).astype(np.int16)
    with wave.open(str(OUT / f"{name}.wav"), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())
    print(f"  {name}.wav  {len(x) / SR:.2f}s")


# ── sounds ───────────────────────────────────────────────────────────────────

def ambient():
    """Empty building at night: low rumble, faint mains hum, slow air movement."""
    sec = 16
    rumble = lowpass(lowpass(noise(sec), 60), 60) * 6
    tt = t(sec)
    hum = 0.05 * np.sin(2 * np.pi * 50 * tt) + 0.02 * np.sin(2 * np.pi * 100 * tt)
    air = bandpass(noise(sec), 200, 900) * (0.25 + 0.2 * np.sin(2 * np.pi * tt / 8.0))
    return loopable(rumble + hum + air * 0.4)


def buzz():
    """Old fluorescent tube: 100 Hz hum with harmonics and a slight rattle."""
    sec = 2.0
    tt = t(sec)
    x = sum(np.sin(2 * np.pi * 100 * k * tt) / k ** 1.3 for k in range(1, 9))
    x += 0.15 * bandpass(noise(sec), 2000, 5000)
    return loopable(x, 0.2)


def footstep(variant):
    """Hard shoe on terrazzo: low thump + short gritty scuff."""
    sec = 0.35
    n = int(sec * SR)
    thump = lowpass(noise(sec), 180 + 40 * variant) * env(n, 0.002, 0.05) * 5
    scuff = bandpass(noise(sec), 1500, 5000) * env(n, 0.004, 0.03) * 0.6
    return fade(echo(thump + scuff, (0.06, 0.13), 0.25))


def door_creak(sec=1.6, base=55.0):
    """Hinge creak: stick-slip pulses with a wandering pitch, resonated by the door."""
    tt = t(sec)
    pitch = base * (1 + 0.35 * np.sin(2 * np.pi * 0.7 * tt) + 0.15 * np.sin(2 * np.pi * 2.3 * tt))
    phase = np.cumsum(pitch) / SR
    pulses = (np.mod(phase, 1.0) < 0.08).astype(float)
    x = bandpass(pulses + 0.1 * noise(sec), 300, 2500)
    x *= env(len(x), 0.08, sec * 0.6, sec * 0.3)
    return fade(echo(x))


def door_slam():
    sec = 1.2
    n = int(sec * SR)
    boom = lowpass(noise(sec), 90) * env(n, 0.002, 0.25) * 8
    crack = bandpass(noise(sec), 800, 4000) * env(n, 0.001, 0.04) * 2
    rattle = bandpass(noise(sec), 1500, 3500) * env(n, 0.05, 0.12) * 0.3
    return echo(boom + crack + rattle, (0.11, 0.23, 0.41, 0.6), 0.45)


def locker_bang():
    """Thin sheet metal: inharmonic partials + clang."""
    sec = 1.8
    tt = t(sec)
    n = len(tt)
    partials = [(187, 1.0), (412, 0.7), (643, 0.5), (921, 0.4), (1388, 0.3), (2011, 0.2)]
    metal = sum(a * np.sin(2 * np.pi * f * tt + rng.uniform(0, 6)) * np.exp(-tt * (3 + f / 300))
                for f, a in partials)
    hit = lowpass(noise(sec), 400) * env(n, 0.001, 0.03) * 4
    return fade(echo(metal + hit, (0.1, 0.21, 0.37), 0.4))


def whisper():
    """Unvoiced syllables: shaped hiss with formant-like bands, no words."""
    sec = 2.4
    tt = t(sec)
    syll = np.clip(np.sin(2 * np.pi * 2.6 * tt + 0.8 * np.sin(2 * np.pi * 0.9 * tt)), 0, None) ** 2
    h = noise(sec)
    x = bandpass(h, 900, 2200) * 0.7 + bandpass(h, 2800, 5200) * 0.4
    x *= syll * env(len(x), 0.2, 0.8, 1.2)
    return fade(x, 0.05)


def breath():
    sec = 2.2
    tt = t(sec)
    shape = np.sin(np.pi * np.clip(tt / 0.9, 0, 1)) ** 2 * 1.0 + np.sin(np.pi * np.clip((tt - 1.1) / 1.0, 0, 1)) ** 2 * 0.7
    x = bandpass(noise(sec), 300, 1800) * shape
    return fade(x, 0.05)


def chair_scrape():
    """Metal chair legs dragged on a hard floor."""
    sec = 1.3
    tt = t(sec)
    pitch = 140 + 60 * np.sin(2 * np.pi * 0.8 * tt)
    phase = np.cumsum(pitch) / SR
    stick = (np.mod(phase, 1.0) < 0.15).astype(float)
    x = bandpass(stick + 0.3 * noise(sec), 400, 3000) * env(len(tt), 0.05, 0.5, 0.6)
    return fade(echo(x))


def school_bell():
    """Electric school bell: a hammer at ~25 Hz striking a metal gong."""
    sec = 3.2
    tt = t(sec)
    gong = sum(a * np.sin(2 * np.pi * f * tt) for f, a in [(820, 1), (1640, 0.4), (2470, 0.3), (3310, 0.15)])
    strikes = 0.5 + 0.5 * np.sign(np.sin(2 * np.pi * 24 * tt))
    on = (tt < 2.6).astype(float)
    x = gong * lowpass(strikes, 200) * on
    x = x * env(len(tt), 0.01, 0.35, 2.5)
    return fade(echo(x, (0.13, 0.27, 0.45, 0.7), 0.5))


def keys():
    """Small bunch of keys: bright metallic ticks."""
    sec = 0.7
    x = np.zeros(int(sec * SR))
    for k in range(6):
        start = int(rng.uniform(0, 0.35) * SR)
        f = rng.uniform(2500, 5200)
        tt = np.arange(len(x) - start) / SR
        x[start:] += np.sin(2 * np.pi * f * tt) * np.exp(-tt * 40) * rng.uniform(0.4, 1.0)
    return fade(x)


def unlock():
    sec = 0.6
    n = int(sec * SR)
    click = bandpass(noise(sec), 1500, 6000) * env(n, 0.001, 0.01)
    clunk = lowpass(noise(sec), 300) * env(n, 0.002, 0.06)
    clunk = np.roll(clunk, int(0.18 * SR)) * 3
    return fade(echo(click + clunk, (0.07,), 0.3))


def rattle():
    """Locked handle being tried."""
    sec = 0.8
    x = np.zeros(int(sec * SR))
    for k in range(5):
        s = int((0.03 + k * 0.13) * SR)
        burst = bandpass(noise(0.08), 900, 4000) * env(int(0.08 * SR), 0.001, 0.02)
        x[s:s + len(burst)] += burst
    return fade(echo(x, (0.08,), 0.3))


def flashlight_click():
    sec = 0.12
    return fade(bandpass(noise(sec), 2000, 7000) * env(int(sec * SR), 0.0005, 0.008))


def main():
    print(f"Writing sounds to {OUT}")
    save("ambient", ambient(), 0.5)
    save("fluorescent_buzz", buzz(), 0.6)
    for v in range(3):
        save(f"footstep_{v + 1}", footstep(v), 0.8)
    save("door_creak", door_creak())
    save("gate_creak", door_creak(3.0, 38.0))
    save("door_slam", door_slam())
    save("locker_bang", locker_bang())
    save("whisper", whisper(), 0.7)
    save("breath", breath(), 0.7)
    save("chair_scrape", chair_scrape(), 0.8)
    save("school_bell", school_bell(), 0.8)
    save("keys", keys(), 0.7)
    save("unlock", unlock(), 0.8)
    save("rattle", rattle(), 0.8)
    save("flashlight_click", flashlight_click(), 0.5)


if __name__ == "__main__":
    main()

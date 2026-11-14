import whisper
import sounddevice as sd
import numpy as np
import json
import re
import sys
import os
import time
import wave

CONFIG_FILE = "stt_config.jsonc"
SAMPLE_RATE = 16000  # Whisper's native sample rate


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r"//[^\n]*", "", raw)
    return json.loads(raw)


# ── Device helpers ────────────────────────────────────────────────────────────

def list_devices():
    print(f"\n{'IDX':<5} {'CH':<4} {'NAME'}")
    print("─" * 60)
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            tag = ""
            if i == sd.default.device[0]:
                tag = "  ← default mic"
            if "loopback" in dev["name"].lower():
                tag = "  ← speaker loopback"
            print(f"{i:<5} {int(dev['max_input_channels']):<4} {dev['name']}{tag}")
    print()


def find_loopback_device() -> int | None:
    """Return the index of the first WASAPI loopback device, or None."""
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and "loopback" in dev["name"].lower():
            return i
    return None


def get_device_index(cfg: dict, source: str) -> int | None:
    live = cfg["live"]
    if source == "microphone":
        return live.get("microphone_device_index")
    if source == "speaker":
        explicit = live.get("speaker_device_index")
        return explicit if explicit is not None else find_loopback_device()
    return None


# ── Audio helpers ─────────────────────────────────────────────────────────────

def record_chunk(duration_secs: float, device_index: int | None) -> np.ndarray:
    """Synchronously record a fixed-length chunk. Returns float32 mono array."""
    frames = sd.rec(
        int(duration_secs * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
        device=device_index,
    )
    sd.wait()
    return frames.flatten()


def is_silent(audio: np.ndarray, threshold: float) -> bool:
    return float(np.abs(audio).mean()) < threshold


def save_wav(audio: np.ndarray, path: str):
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(path, "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


# ── Transcription kwargs (reused from stt.py logic) ──────────────────────────

def build_transcribe_kwargs(cfg: dict) -> dict:
    t = cfg["transcription"]
    d = cfg["decoding"]
    kwargs = {
        "language":                    t.get("language"),
        "task":                        t.get("task", "transcribe"),
        "temperature":                 t.get("temperature", [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]),
        "initial_prompt":              t.get("initial_prompt"),
        "condition_on_previous_text":  t.get("condition_on_previous_text", True),
        "carry_initial_prompt":        t.get("carry_initial_prompt", False),
        "no_speech_threshold":         t.get("no_speech_threshold", 0.6),
        "logprob_threshold":           t.get("logprob_threshold", -1.0),
        "compression_ratio_threshold": t.get("compression_ratio_threshold", 2.4),
        "word_timestamps":             False,
        "verbose":                     False,
        "beam_size":                   d.get("beam_size", 5),
        "best_of":                     d.get("best_of", 5),
        "fp16":                        d.get("fp16", False),
    }
    return {k: v for k, v in kwargs.items() if v is not None or k in
            ("language", "initial_prompt")}


# ── Streaming mode (piece by piece) ──────────────────────────────────────────

def run_streaming(model: whisper.Whisper, cfg: dict, source: str):
    """
    Record in fixed-length chunks and transcribe each one immediately.
    Recommended for real-time feedback — output appears as you speak.
    """
    live = cfg["live"]
    chunk_secs    = live.get("chunk_duration_seconds", 5)
    silence_thresh = live.get("silence_threshold", 0.01)
    save_audio    = live.get("save_recording", False)
    save_path     = live.get("recording_output", f"{source}_recording.wav")
    device_index  = get_device_index(cfg, source)

    print(f"[streaming] source={source}  chunk={chunk_secs}s  "
          f"device={'default' if device_index is None else device_index}")
    print("Press Ctrl+C to stop.\n")

    transcript_parts = []
    all_audio        = []
    prev_text        = cfg["transcription"].get("initial_prompt") or ""

    try:
        while True:
            chunk = record_chunk(chunk_secs, device_index)

            if is_silent(chunk, silence_thresh):
                print("·", end="", flush=True)
                continue

            if save_audio:
                all_audio.append(chunk)

            kwargs = build_transcribe_kwargs(cfg)
            # Feed last ~200 chars as rolling context so chunks stay coherent
            if prev_text and cfg["transcription"].get("condition_on_previous_text", True):
                kwargs["initial_prompt"] = prev_text[-200:]

            result = model.transcribe(chunk, **kwargs)
            text   = result["text"].strip()

            if text:
                print(f"\n{text}")
                transcript_parts.append(text)
                prev_text = text

    except KeyboardInterrupt:
        print("\n\nStopped.")

    full_text = " ".join(transcript_parts)

    if save_audio and all_audio:
        save_wav(np.concatenate(all_audio), save_path)
        print(f"Audio saved → {save_path}")

    return full_text


# ── Full mode (record everything, transcribe once) ────────────────────────────

def run_full(model: whisper.Whisper, cfg: dict, source: str):
    """
    Record until Ctrl+C, then transcribe the entire audio at once.
    Higher accuracy than streaming; no real-time output.
    """
    live         = cfg["live"]
    save_audio   = live.get("save_recording", False)
    save_path    = live.get("recording_output", f"{source}_recording.wav")
    device_index = get_device_index(cfg, source)

    print(f"[full] source={source}  "
          f"device={'default' if device_index is None else device_index}")
    print("Recording… Press Ctrl+C to stop and transcribe.\n")

    frames = []

    def _callback(indata, _frames, _time, _status):
        frames.append(indata.copy())

    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                            device=device_index, callback=_callback):
            while True:
                time.sleep(0.05)
    except KeyboardInterrupt:
        pass

    if not frames:
        print("No audio captured.")
        return ""

    audio = np.concatenate(frames).flatten()
    duration = len(audio) / SAMPLE_RATE
    print(f"\nCaptured {duration:.1f}s — transcribing…")

    if save_audio:
        save_wav(audio, save_path)
        print(f"Audio saved → {save_path}")

    result = model.transcribe(audio, **build_transcribe_kwargs(cfg))
    return result["text"].strip()


# ── Save transcript ───────────────────────────────────────────────────────────

def save_transcript(text: str, cfg: dict, source: str):
    live     = cfg["live"]
    out_dir  = cfg["output"].get("output_dir") or "."
    suffix   = live.get("transcript_suffix") or f"_{source}_live"  # config has null → default
    fmt      = cfg["output"].get("format", "txt")

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename  = f"transcript{suffix}_{timestamp}.{fmt}"
    path      = os.path.join(out_dir, filename)

    os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"Transcript saved → {path}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]

    if "--list-devices" in args or "-l" in args:
        list_devices()
        return

    config_path = next((args[i + 1] for i, a in enumerate(args)
                        if a in ("--config", "-c") and i + 1 < len(args)), CONFIG_FILE)
    cfg  = load_config(config_path)
    live = cfg["live"]

    source   = live.get("source", "microphone")     # "microphone" | "speaker"
    strategy = live.get("strategy", "streaming")    # "streaming"  | "full"

    # CLI overrides
    if "--mic" in args:
        source = "microphone"
    if "--speaker" in args:
        source = "speaker"
    if "--full" in args:
        strategy = "full"
    if "--stream" in args:
        strategy = "streaming"

    # Validate speaker loopback availability
    if source == "speaker" and get_device_index(cfg, "speaker") is None:
        print("ERROR: No speaker loopback device found.")
        print("Enable 'Stereo Mix' in Windows Sound settings, or set")
        print("'speaker_device_index' in stt_config.jsonc after running --list-devices.")
        print("\nAvailable input devices:")
        list_devices()
        return

    m = cfg["model"]
    print(f"Loading Whisper model '{m['name']}'…")
    model = whisper.load_model(
        m["name"],
        device=m.get("device"),
        download_root=m.get("download_root"),
        in_memory=m.get("in_memory", False),
    )

    runner = run_streaming if strategy == "streaming" else run_full
    text   = runner(model, cfg, source)

    if text:
        if cfg["output"].get("print_to_console", True):
            print("\n─── Full Transcript ───")
            print(text)
        save_transcript(text, cfg, source)


if __name__ == "__main__":
    main()

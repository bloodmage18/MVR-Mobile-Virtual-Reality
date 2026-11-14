import whisper
import json
import sys
import os

CONFIG_FILE = "stt_config.jsonc"


def load_config(path: str) -> dict:
    import re
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    raw = re.sub(r"//[^\n]*", "", raw)  # strip // comments (JSONC style)
    return json.loads(raw)


def resolve_output_path(audio_file: str, cfg: dict) -> str:
    base = os.path.splitext(os.path.basename(audio_file))[0]
    suffix = cfg["output"].get("filename_suffix", "")
    ext = "." + cfg["output"].get("format", "txt")
    filename = base + suffix + ext

    out_dir = cfg["output"].get("output_dir") or os.path.dirname(os.path.abspath(audio_file))
    return os.path.join(out_dir, filename)


def build_transcribe_kwargs(cfg: dict) -> dict:
    t = cfg["transcription"]
    d = cfg["decoding"]

    kwargs = {
        "language":                   t.get("language"),
        "task":                       t.get("task", "transcribe"),
        "temperature":                t.get("temperature", [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]),
        "initial_prompt":             t.get("initial_prompt"),
        "condition_on_previous_text": t.get("condition_on_previous_text", True),
        "carry_initial_prompt":       t.get("carry_initial_prompt", False),
        "no_speech_threshold":        t.get("no_speech_threshold", 0.6),
        "logprob_threshold":          t.get("logprob_threshold", -1.0),
        "compression_ratio_threshold":t.get("compression_ratio_threshold", 2.4),
        "word_timestamps":            t.get("word_timestamps", False),
        "verbose":                    t.get("verbose", False),
        "beam_size":                  d.get("beam_size", 5),
        "best_of":                    d.get("best_of", 5),
        "patience":                   d.get("patience"),
        "length_penalty":             d.get("length_penalty"),
        "suppress_blank":             d.get("suppress_blank", True),
        "suppress_tokens":            d.get("suppress_tokens", [-1]),
        "fp16":                       d.get("fp16", False),
    }
    return {k: v for k, v in kwargs.items() if v is not None or k in (
        "language", "initial_prompt", "patience", "length_penalty"
    )}


def transcribe(audio_file: str, cfg: dict) -> dict:
    if not os.path.exists(audio_file):
        raise FileNotFoundError(f"Audio file not found: {audio_file}")

    m = cfg["model"]
    print(f"Loading Whisper model '{m['name']}'...")
    model = whisper.load_model(
        m["name"],
        device=m.get("device"),
        download_root=m.get("download_root"),
        in_memory=m.get("in_memory", False),
    )

    print(f"Transcribing: {audio_file}")
    return model.transcribe(audio_file, **build_transcribe_kwargs(cfg))


def save_output(result: dict, output_path: str, fmt: str):
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

    if fmt == "json":
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
    elif fmt == "srt":
        with open(output_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(result.get("segments", []), 1):
                start = _fmt_timestamp(seg["start"])
                end   = _fmt_timestamp(seg["end"])
                f.write(f"{i}\n{start} --> {end}\n{seg['text'].strip()}\n\n")
    else:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result["text"].strip())


def _fmt_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


if __name__ == "__main__":
    config_path = sys.argv[2] if len(sys.argv) > 2 else CONFIG_FILE
    cfg = load_config(config_path)

    audio_file = sys.argv[1] if len(sys.argv) > 1 else cfg["input"].get("audio_file", "Recording (3).m4a")

    result = transcribe(audio_file, cfg)

    if cfg["output"].get("print_to_console", True):
        print("\n--- Transcription ---")
        print(result["text"].strip())

    output_path = resolve_output_path(audio_file, cfg)
    fmt = cfg["output"].get("format", "txt")
    save_output(result, output_path, fmt)
    print(f"\nSaved to: {output_path}")

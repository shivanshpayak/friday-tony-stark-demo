"""DIAGNOSTIC (temporary): is the command BEFORE or AFTER wake-word detection?

Round 1 showed openwakeword fires late — the audio after detection is a word
*ending* (RMS 963->403->43->silence) followed by 2s of nothing. That means the
command is already in the past when we get control.

WakeWordListener keeps the last 2.5s in a ring buffer (it feeds speaker
verification). This dumps that buffer's energy profile and decodes it three
ways, to confirm the command is in there and pick the grammar that finds it.

Run:  uv run python diag_fastpath.py
Say "Hey Jarvis, mute" naturally. Ctrl+C to stop. Delete when done.
"""
import json
import sys

import numpy as np

from friday.fastpath import registry
from friday.fastpath.model import ensure_model
from friday_launcher import AUDIO_CHUNK, AUDIO_RATE, WakeWordListener

WAKE_PREFIXES = ("hey jarvis", "jarvis")


def wake_prefixed_grammar() -> list[str]:
    """Command phrases, plus the same phrases with the wake word in front."""
    phrases = [p for p in registry.grammar_phrases() if p != registry.UNK]
    out = list(phrases)
    for prefix in WAKE_PREFIXES:
        out.extend(f"{prefix} {p}" for p in phrases)
    out.extend(WAKE_PREFIXES)
    return list(dict.fromkeys(out)) + [registry.UNK]


def strip_wake_prefix(text: str) -> str:
    t = registry.normalize(text)
    for prefix in WAKE_PREFIXES:
        if t.startswith(prefix + " "):
            return t[len(prefix) + 1:]
    return t


def decode(model, grammar, pcm_bytes):
    from vosk import KaldiRecognizer

    rec = KaldiRecognizer(model, AUDIO_RATE, json.dumps(grammar))
    step = AUDIO_CHUNK * 2
    for i in range(0, len(pcm_bytes), step):
        rec.AcceptWaveform(pcm_bytes[i:i + step])
    return json.loads(rec.FinalResult()).get("text", "")


def profile(pcm_int16, label):
    print(f"\n  {label} — energy per 80ms (oldest -> newest):")
    n = AUDIO_CHUNK
    line = []
    for i in range(0, len(pcm_int16) - n, n):
        rms = float(np.sqrt(np.mean(pcm_int16[i:i + n].astype(np.float32) ** 2)))
        line.append("#" if rms > 300 else ("+" if rms > 60 else "."))
    print("    " + "".join(line))
    print("    " + "".join("^" if i % 12 == 0 else " " for i in range(len(line)))
          + "   (^ = every ~1s)")


def main():
    model_dir = ensure_model(download=False)
    if model_dir is None:
        print("No Vosk model — run the launcher once first.")
        return 1
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(model_dir))

    plain = registry.grammar_phrases()
    prefixed = wake_prefixed_grammar()

    ww = WakeWordListener()
    ww.start_stream()
    print("\nSay 'Hey Jarvis, mute' — naturally. (Ctrl+C to quit)\n")

    try:
        while True:
            if not ww.listen_once():
                continue

            # The 2.5s that ALREADY happened, captured at the trigger instant.
            past_f = ww.recent_audio_float()
            past = (past_f * 32768.0).astype(np.int16)
            past_bytes = past.tobytes()

            print("=" * 72)
            print("WAKE WORD DETECTED")
            profile(past, "RING BUFFER (before trigger)")

            print(f"\n  {'plain grammar':28s} -> {decode(model, plain, past_bytes)!r}")
            pre = decode(model, prefixed, past_bytes)
            print(f"  {'wake-prefixed grammar':28s} -> {pre!r}")
            stripped = strip_wake_prefix(pre)
            cmd = registry.lookup(stripped)
            print(f"  {'after stripping prefix':28s} -> {stripped!r}")
            print(f"  {'MATCHES':28s} -> {cmd.name if cmd else '(no match)'}")

            # And what the current forward-reading implementation would see.
            fwd = b"".join(ww.read_chunk() for _ in range(10))  # 800ms
            fwd_i16 = np.frombuffer(fwd, dtype=np.int16)
            profile(fwd_i16, "FORWARD 800ms (what we read today)")
            print(f"\n  {'forward, plain grammar':28s} -> {decode(model, plain, fwd)!r}")
            print("=" * 72 + "\n")
            ww.reset()

    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        ww.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())

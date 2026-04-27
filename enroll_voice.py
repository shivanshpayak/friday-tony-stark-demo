"""
JARVIS Voice Enrollment
=======================
Records a short sample of the user's voice and saves a speaker embedding
that the wake-word gate uses to reject non-user voices (TV, other people).

Usage:
    .venv/Scripts/python.exe enroll_voice.py

The script will:
  1. Prompt you to speak for ~20 seconds (keep talking naturally).
  2. Compute a Resemblyzer embedding from the clip.
  3. Save it to voice_embedding.npy next to this file.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pyaudio
from resemblyzer import VoiceEncoder

SAMPLE_RATE = 16000
CHUNK = 1600  # 100ms
RECORD_SECONDS = 20
OUTPUT_PATH = Path(__file__).parent / "voice_embedding.npy"
PROMPTS = [
    "Hey Jarvis, run a diagnostic and tell me how the systems are looking.",
    "Jarvis, give me a brief on the world right now, sir.",
    "Open the world monitor and show me what I'm looking at.",
    "Stand down for now, I'll call you back in a minute.",
    "Talk naturally about what you did today for the rest of the time.",
]


def record_clip(seconds: int) -> np.ndarray:
    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK,
    )
    total_chunks = int(seconds * SAMPLE_RATE / CHUNK)
    frames = []
    start = time.time()
    print()
    for i in range(total_chunks):
        raw = stream.read(CHUNK, exception_on_overflow=False)
        frames.append(np.frombuffer(raw, dtype=np.int16))
        elapsed = time.time() - start
        remaining = max(0, seconds - elapsed)
        bar_len = 30
        filled = int(bar_len * (i + 1) / total_chunks)
        bar = "#" * filled + "-" * (bar_len - filled)
        sys.stdout.write(f"\r  recording [{bar}] {remaining:4.1f}s left ")
        sys.stdout.flush()
    print()
    stream.stop_stream()
    stream.close()
    pa.terminate()
    return np.concatenate(frames).astype(np.float32) / 32768.0


def main():
    print("=" * 60)
    print("JARVIS Voice Enrollment")
    print("=" * 60)
    print()
    print("I'll record ~%d seconds of your voice to build a speaker" % RECORD_SECONDS)
    print("fingerprint. Speak naturally. Here are some prompts you can use:")
    print()
    for line in PROMPTS:
        print(f"  - {line}")
    print()
    input("Press ENTER when you're ready, then start talking...")

    wav = record_clip(RECORD_SECONDS)

    # Basic sanity check: is the clip loud enough?
    rms = float(np.sqrt(np.mean(wav ** 2)))
    print(f"  signal RMS: {rms:.4f}")
    if rms < 0.005:
        print()
        print("  WARNING: clip is very quiet. Check your mic and try again.")
        sys.exit(1)

    print()
    print("  computing voice embedding...")
    encoder = VoiceEncoder()
    embedding = encoder.embed_utterance(wav)
    print(f"  embedding shape: {embedding.shape}")

    np.save(OUTPUT_PATH, embedding)
    print()
    print(f"  saved -> {OUTPUT_PATH}")
    print()
    print("Done. Run 'uv run friday_start' and only your voice should wake it.")


if __name__ == "__main__":
    main()

"""
JARVIS Voice Enrollment
=======================
Builds the speaker fingerprint the wake-word gate uses to reject other voices
(TV, other people).

Usage:
    uv run python enroll_voice.py

You say "Hey Jarvis" a handful of times. Each time the wake word fires, the
script keeps exactly what the launcher would verify — the last 2.5 seconds of
mic audio — and the fingerprint is the average of those clips.

Why not just record 20 seconds of talking (the old approach): the gate never
sees 20 seconds of talking. It sees one short "Hey Jarvis" padded with room
noise, and a fingerprint made from long natural speech matched that only
marginally (accepted wakes averaged 0.74 against a 0.70 threshold, and about a
third were rejected). Enrolling on the same kind of clip closes that gap.

A running JARVIS is stopped first (it would answer every "Hey Jarvis") and
started again at the end with the new fingerprint.

Windows and Linux each keep their own fingerprint (voice_embedding.windows.npy
/ voice_embedding.linux.npy), so enroll once on each OS — enrolling on one
never changes the other.
"""

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pyaudio

from friday import plat

ROOT = Path(__file__).parent
OUTPUT_PATH = plat.voice_embedding_path()   # this OS's fingerprint
BACKUP_PATH = OUTPUT_PATH.with_name(OUTPUT_PATH.stem + ".previous.npy")
PID_FILE = ROOT / "runtime" / "friday.pid"

# These mirror friday_launcher.py — the clips must be captured the way the
# launcher captures them, or the enrollment stops matching what it verifies.
WAKE_MODEL = "hey_jarvis_v0.1"
WAKE_THRESHOLD = 0.7
AUDIO_RATE = 16000
AUDIO_CHUNK = 1280
SPEAKER_BUFFER_SECONDS = 2.5
SPEAKER_SIM_THRESHOLD = 0.70

SAMPLES = 10
SAMPLE_TIMEOUT = 25.0     # seconds to wait for one "Hey Jarvis"
SETTLE_SECONDS = 1.0      # pause between samples
# How to say each one, so the fingerprint covers how you actually call it.
STYLES = [
    "normally", "normally", "normally", "a little quieter", "a little louder",
    "leaning back from the laptop", "normally", "quickly", "slowly", "normally",
]


def _windows_launcher_pids() -> list[int]:
    """Running launcher processes, found by command line.

    The launcher only writes its PID file on Linux (Windows drives it with
    hotkeys), so on Windows the stop below used to find nothing and JARVIS
    answered every enrollment sample.
    """
    import psutil

    pids = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        name = (proc.info["name"] or "").lower()
        cmdline = " ".join(proc.info["cmdline"] or []).lower()
        if name.startswith("python") and (
                "friday_start" in cmdline or "friday_launcher" in cmdline):
            pids.append(proc.info["pid"])
    return pids


def _stop_running_jarvis() -> bool:
    if plat.IS_WINDOWS:
        pids = _windows_launcher_pids()
        for pid in pids:
            try:
                plat.kill_tree(pid)   # launcher, agent and tool server
            except Exception:
                pass
        if pids:
            time.sleep(1.0)   # let it release the mic
        return bool(pids)
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        return False
    for _ in range(40):
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.25)
    time.sleep(1.0)   # let it release the mic
    return True


def _restart_jarvis() -> None:
    if plat.IS_WINDOWS:
        script = ROOT / "start_friday.vbs"
        if not script.exists():
            return
        # Same detached spawn as the launcher's restart hotkey.
        subprocess.Popen(
            ["wscript.exe", str(script)], cwd=str(ROOT),
            creationflags=(subprocess.DETACHED_PROCESS
                           | subprocess.CREATE_NEW_PROCESS_GROUP),
            close_fds=True)
        return
    script = ROOT / "start_friday.sh"
    if not script.exists():
        return
    subprocess.Popen([str(script)], cwd=str(ROOT), stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def _capture_wake_clips(count: int) -> list[np.ndarray]:
    """Collect `count` clips, each the 2.5s of audio behind a wake-word hit."""
    from openwakeword.model import Model as WakeWordModel

    model = WakeWordModel(wakeword_models=[WAKE_MODEL], inference_framework="onnx")
    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=AUDIO_RATE,
                     input=True, frames_per_buffer=AUDIO_CHUNK)
    ring = np.zeros(int(AUDIO_RATE * SPEAKER_BUFFER_SECONDS), dtype=np.int16)
    clips: list[np.ndarray] = []
    try:
        while len(clips) < count:
            style = STYLES[len(clips) % len(STYLES)]
            print(f"  [{len(clips) + 1}/{count}] Say \"Hey Jarvis\" {style}… ",
                  end="", flush=True)
            model.reset()
            ring[:] = 0
            deadline = time.monotonic() + SAMPLE_TIMEOUT
            heard = False
            while time.monotonic() < deadline:
                audio = np.frombuffer(
                    stream.read(AUDIO_CHUNK, exception_on_overflow=False),
                    dtype=np.int16)
                ring = np.concatenate((ring[len(audio):], audio))
                score = max(model.predict(audio).values())
                if score >= WAKE_THRESHOLD:
                    clips.append(ring.astype(np.float32) / 32768.0)
                    print(f"got it (wake score {score:.2f})")
                    heard = True
                    break
            if not heard:
                print("didn't catch that — try again, a bit closer to the mic.")
                continue
            # Drain the tail of the word so it can't bleed into the next clip.
            for _ in range(int(SETTLE_SECONDS * AUDIO_RATE / AUDIO_CHUNK)):
                stream.read(AUDIO_CHUNK, exception_on_overflow=False)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()
    return clips


def _unit(vector: np.ndarray) -> np.ndarray:
    return vector / (np.linalg.norm(vector) + 1e-9)


def main():
    print("=" * 60)
    print("JARVIS Voice Enrollment")
    print("=" * 60)
    print()
    print(f"You'll say \"Hey Jarvis\" {SAMPLES} times. Wait for each prompt,")
    print("and sit where you normally sit when you talk to it.")
    print()
    input("Press ENTER when you're ready...")
    print()

    was_running = _stop_running_jarvis()
    if was_running:
        print("  (stopped the running JARVIS so it doesn't answer)")
    print("  loading the wake-word model...")
    print()

    try:
        clips = _capture_wake_clips(SAMPLES)

        print()
        print("  computing voice fingerprint...")
        from resemblyzer import VoiceEncoder
        encoder = VoiceEncoder(verbose=False)
        embeddings = [_unit(encoder.embed_utterance(clip)) for clip in clips]
        fingerprint = _unit(np.mean(embeddings, axis=0))

        # Honest self-check: score each clip against a fingerprint built from
        # the OTHER clips, so a sample can't vouch for itself.
        held_out = []
        for i, emb in enumerate(embeddings):
            others = _unit(np.mean(embeddings[:i] + embeddings[i + 1:], axis=0))
            held_out.append(float(np.dot(emb, others)))
        low, mean = min(held_out), sum(held_out) / len(held_out)
        passed = sum(score >= SPEAKER_SIM_THRESHOLD for score in held_out)

        if OUTPUT_PATH.exists():
            shutil.copy2(OUTPUT_PATH, BACKUP_PATH)
        np.save(OUTPUT_PATH, fingerprint.astype(np.float32))

        print()
        print(f"  saved -> {OUTPUT_PATH.name}  (previous kept as {BACKUP_PATH.name})")
        print(f"  match scores: lowest {low:.2f}, average {mean:.2f} "
              f"(JARVIS accepts {SPEAKER_SIM_THRESHOLD:.2f} and up)")
        print(f"  {passed} of {len(held_out)} samples would have been accepted.")
        if passed < len(held_out):
            print("  Some were below the bar — run this again in a quieter moment"
                  " if JARVIS still ignores you.")
    finally:
        if was_running:
            print()
            print("  restarting JARVIS with the new fingerprint...")
            _restart_jarvis()
    print()
    print("Done.")


if __name__ == "__main__":
    main()

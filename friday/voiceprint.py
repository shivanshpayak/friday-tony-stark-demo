"""
Clean a clip before it becomes (or is compared to) a voice fingerprint.

The clip behind a wake word is 2.5s of mic audio, but "Hey Jarvis" fills only
about a second of it; the rest is the room. Fed raw to the encoder, that
background counts as much as the voice, so a different fan, room or distance
from the mic dragged the owner's score under the bar. Three steps, in order:

1. High-pass at 300 Hz. This laptop's analog mic carries a loud rumble below
   70 Hz (measured -32 dBFS on 2026-10-11 — as loud as quiet speech — and
   swinging 15 dB from one second to the next) and a 120 Hz mains hum while
   charging. Neither is the voice, both change between sessions, and both
   landed in the fingerprint: two enrollments of the same person a day apart
   matched each other at only 0.68, and the owner passed about one wake in
   ten. Speaker identity survives without the low band (it is what a phone
   line carries); in simulation the same voice scored 0.65-0.92 across noise
   conditions unfiltered and 0.89-0.94 filtered.
2. Cut the silence, so what is left is mostly the word.
3. Level the volume.

Enrollment and both verifiers must run the same cleaning, or the fingerprint
stops matching what is checked against it — re-enroll after changing any of it.
"""

import numpy as np

from .voice_encoder import normalize_volume, trim_long_silences

# Below this much speech the trimmed clip is too short to embed reliably; keep
# the raw clip instead (the old behaviour) rather than guess from a sliver.
MIN_VOICED_SECONDS = 0.5
SAMPLE_RATE = 16000
# Everything below this is removed; the gain ramps up over the 40 Hz under it.
HIGHPASS_HZ = 300
_HIGHPASS_RAMP_HZ = 40


def highpass(wav: np.ndarray, cutoff: float = HIGHPASS_HZ) -> np.ndarray:
    """Remove everything below `cutoff` Hz from a whole clip.

    Done on the clip's spectrum rather than with a running filter: the clips
    are a couple of seconds long, and this needs nothing beyond numpy.
    """
    if len(wav) == 0:
        return wav
    spectrum = np.fft.rfft(wav)
    freqs = np.fft.rfftfreq(len(wav), 1 / SAMPLE_RATE)
    ramp = np.clip((freqs - (cutoff - _HIGHPASS_RAMP_HZ)) / _HIGHPASS_RAMP_HZ, 0, 1)
    gain = 0.5 - 0.5 * np.cos(np.pi * ramp)
    return np.fft.irfft(spectrum * gain, len(wav)).astype(np.float32)


def clean_clip(wav: np.ndarray) -> np.ndarray:
    """Return `wav` (float32, 16 kHz mono) filtered, silence-cut and levelled."""
    wav = np.clip(highpass(np.asarray(wav, dtype=np.float32)), -1.0, 1.0)
    try:
        voiced = trim_long_silences(wav)
    except Exception:
        voiced = wav
    if len(voiced) < MIN_VOICED_SECONDS * SAMPLE_RATE:
        voiced = wav
    return normalize_volume(voiced, increase_only=True)


def save_clip(path, wav: np.ndarray) -> None:
    """Write a float clip as 16-bit mono WAV (for enrollment and diagnosis)."""
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(wav, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())

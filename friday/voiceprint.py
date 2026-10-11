"""
Clean a clip before it becomes (or is compared to) a voice fingerprint.

The clip behind a wake word is 2.5s of mic audio, but "Hey Jarvis" fills only
about a second of it; the rest is the room. Fed raw to Resemblyzer, that
background counts as much as the voice, so a different fan, room or distance
from the mic dragged the owner's score under the bar. Cutting the silence and
levelling the volume first leaves the fingerprint mostly the voice.

Enrollment and both verifiers must run the same cleaning, or the fingerprint
stops matching what is checked against it.
"""

import numpy as np

# Below this much speech the trimmed clip is too short to embed reliably; keep
# the raw clip instead (the old behaviour) rather than guess from a sliver.
MIN_VOICED_SECONDS = 0.5
SAMPLE_RATE = 16000


def clean_clip(wav: np.ndarray) -> np.ndarray:
    """Return `wav` (float32, 16 kHz mono) with silences cut and volume levelled."""
    from resemblyzer.audio import normalize_volume, trim_long_silences
    from resemblyzer.hparams import audio_norm_target_dBFS

    wav = np.clip(np.asarray(wav, dtype=np.float32), -1.0, 1.0)
    try:
        voiced = trim_long_silences(wav)
    except Exception:
        voiced = wav
    if len(voiced) < MIN_VOICED_SECONDS * SAMPLE_RATE:
        voiced = wav
    return normalize_volume(voiced, audio_norm_target_dBFS, increase_only=True)

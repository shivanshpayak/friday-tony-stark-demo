"""The numpy speaker encoder reproduces Resemblyzer's embeddings.

Resemblyzer (torch + librosa + numba) cost the launcher ~265 MB of RAM to
fingerprint one short clip per wake word. friday/voice_encoder.py runs the same
network in numpy. Enrolled fingerprints were made with Resemblyzer, so the new
encoder has to land on the same vectors — the reference below was computed with
resemblyzer.VoiceEncoder on the same clip before the dependency was removed.
"""
import subprocess
import sys
from pathlib import Path

import numpy as np

from friday.voice_encoder import VoiceEncoder, trim_long_silences

REFERENCE = Path(__file__).parent / "data" / "resemblyzer_reference.npy"


def _clip(seconds: float = 2.5) -> np.ndarray:
    """A voice-like test signal: harmonics of a gliding pitch, syllable-gated."""
    t = np.arange(int(16000 * seconds)) / 16000
    pitch = 120 + 30 * np.sin(2 * np.pi * 0.7 * t)
    phase = 2 * np.pi * np.cumsum(pitch) / 16000
    voice = sum(np.sin(k * phase) / k for k in range(1, 12))
    envelope = 0.5 * (1 + np.sin(2 * np.pi * 3 * t))
    return (0.1 * voice * envelope).astype(np.float32)


def test_embedding_matches_resemblyzer():
    emb = VoiceEncoder().embed_utterance(_clip())
    ref = np.load(REFERENCE)
    assert emb.shape == (256,)
    assert abs(np.linalg.norm(emb) - 1) < 1e-5
    assert float(emb @ ref) > 0.9999
    assert np.abs(emb - ref).max() < 1e-4


def test_clip_shorter_than_one_partial_still_embeds():
    emb = VoiceEncoder().embed_utterance(_clip(0.4))
    assert emb.shape == (256,) and np.isfinite(emb).all()


def test_trimming_a_clip_shorter_than_the_smoothing_window():
    """np.convolve(mode="same") grows a mask shorter than its kernel."""
    clip = _clip(0.06)
    assert len(trim_long_silences(clip)) <= len(clip)


def test_encoder_needs_no_heavy_dependencies():
    code = (
        "import sys, numpy as np\n"
        "from friday.voice_encoder import VoiceEncoder\n"
        "from friday.voiceprint import clean_clip\n"
        "VoiceEncoder().embed_utterance(clean_clip(np.zeros(16000, np.float32) + 0.01))\n"
        "heavy = {'torch', 'librosa', 'numba', 'scipy', 'sklearn', 'resemblyzer'}\n"
        "print(sorted(heavy & {m.split('.')[0] for m in sys.modules}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=Path(__file__).parents[1], check=True)
    assert out.stdout.strip() == "[]"

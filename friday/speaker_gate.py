"""
In-session speaker verification using Resemblyzer voice embeddings.

Compares incoming audio against the enrolled voice (voice_embedding.npy).
If no embedding file exists, the gate is disabled and all audio passes through.
"""

import numpy as np
from .config import (
    VOICE_EMBEDDING_PATH, SPEAKER_SIM_THRESHOLD, SESSION_SPEAKER_GATE_ENABLED, logger,
)


class SessionSpeakerGate:
    """Verifies that incoming audio belongs to the enrolled user.

    Loaded once per process (the Resemblyzer VoiceEncoder is ~50 MB in RAM).
    """

    def __init__(self) -> None:
        self._encoder = None
        self._reference = None
        if VOICE_EMBEDDING_PATH.exists():
            try:
                self._reference = np.load(VOICE_EMBEDDING_PATH)
                from resemblyzer import VoiceEncoder
                self._encoder = VoiceEncoder(verbose=False)
                logger.info("Session speaker gate loaded (threshold=%.2f)",
                            SPEAKER_SIM_THRESHOLD)
            except Exception as e:
                logger.warning("Session speaker gate init failed (%s); disabled", e)

    @property
    def enabled(self) -> bool:
        return (
            SESSION_SPEAKER_GATE_ENABLED
            and self._encoder is not None
            and self._reference is not None
        )

    def verify(self, pcm_int16: np.ndarray, sample_rate: int = 16000) -> bool:
        """Return True if the audio matches the enrolled voice (or gate is off)."""
        if not self.enabled:
            return True
        try:
            wav = pcm_int16.astype(np.float32) / 32768.0
            if sample_rate != 16000:
                from scipy.signal import resample
                wav = resample(wav, int(len(wav) * 16000 / sample_rate))
            if len(wav) < 1600:  # < 100 ms — too short for reliable embedding
                return True
            emb = self._encoder.embed_utterance(wav)
            ref = self._reference
            sim = float(np.dot(emb, ref) /
                        (np.linalg.norm(emb) * np.linalg.norm(ref) + 1e-9))
            logger.debug("Speaker sim=%.3f (threshold=%.2f)", sim, SPEAKER_SIM_THRESHOLD)
            return sim >= SPEAKER_SIM_THRESHOLD
        except Exception as e:
            logger.warning("Speaker verify error (%s); allowing", e)
            return True


# Singleton — loaded once when the first session starts.
_speaker_gate: SessionSpeakerGate | None = None


def get_speaker_gate() -> SessionSpeakerGate:
    global _speaker_gate
    if _speaker_gate is None:
        _speaker_gate = SessionSpeakerGate()
    return _speaker_gate

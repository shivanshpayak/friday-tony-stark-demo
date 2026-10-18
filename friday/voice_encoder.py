"""
Speaker-embedding encoder: Resemblyzer's network, run in plain numpy.

Resemblyzer's model is small — a 3-layer LSTM and one linear layer, 1.4M
weights — but importing it pulled in torch, librosa, numba and llvmlite:
about 265 MB of resident memory in the launcher (measured 2026-10-11), plus a
~4.6s numba JIT on the first embedding, to fingerprint one 2.5s clip per wake
word. This module does the same arithmetic with numpy alone: ~10 MB, no JIT.

The weights in voice_encoder.npz are Resemblyzer's pretrained.pt (Apache-2.0),
converted unchanged, so embeddings match the originals to float rounding and
fingerprints enrolled with Resemblyzer stay valid. The mel spectrogram and
the silence trimming reproduce librosa's / Resemblyzer's defaults exactly;
tests/test_voice_encoder.py pins the output.
"""

import threading
from pathlib import Path

import numpy as np

WEIGHTS_PATH = Path(__file__).with_name("voice_encoder.npz")

SAMPLE_RATE = 16000
_N_FFT = 400            # 25 ms window
_HOP = 160              # 10 ms step
_N_MELS = 40
_PARTIAL_FRAMES = 160   # each partial utterance is 1.6s
_HIDDEN = 256
_LAYERS = 3

# Silence trimming (Resemblyzer's VAD hyperparameters).
_VAD_WINDOW = 480       # 30 ms
_VAD_AVERAGE_WIDTH = 8
_VAD_MAX_SILENCE = 6
_INT16_MAX = (2 ** 15) - 1
NORM_TARGET_DBFS = -30


def _mel_filterbank() -> np.ndarray:
    """librosa.filters.mel(sr=16000, n_fft=400, n_mels=40): Slaney scale and norm."""
    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0

    def hz_to_mel(hz: float) -> float:
        if hz >= min_log_hz:
            return min_log_mel + np.log(hz / min_log_hz) / logstep
        return hz / f_sp

    mels = np.linspace(hz_to_mel(0.0), hz_to_mel(SAMPLE_RATE / 2), _N_MELS + 2)
    mel_f = np.where(mels >= min_log_mel,
                     min_log_hz * np.exp(logstep * (mels - min_log_mel)),
                     f_sp * mels)
    fft_f = np.linspace(0, SAMPLE_RATE / 2, 1 + _N_FFT // 2)
    fdiff = np.diff(mel_f)
    ramps = mel_f[:, None] - fft_f[None, :]
    lower = -ramps[:-2] / fdiff[:-1, None]
    upper = ramps[2:] / fdiff[1:, None]
    weights = np.maximum(0, np.minimum(lower, upper))
    weights *= (2.0 / (mel_f[2:] - mel_f[:-2]))[:, None]
    return weights.astype(np.float32)


def _sigmoid(x: np.ndarray) -> np.ndarray:
    with np.errstate(over="ignore"):      # exp overflow gives the right limit, 0
        return 1.0 / (1.0 + np.exp(-x))


def trim_long_silences(wav: np.ndarray) -> np.ndarray:
    """Cut stretches without voice down to a short gap (Resemblyzer's routine)."""
    import webrtcvad

    wav = wav[:len(wav) - (len(wav) % _VAD_WINDOW)]
    pcm = np.round(wav * _INT16_MAX).astype(np.int16).tobytes()
    vad = webrtcvad.Vad(mode=3)
    step = _VAD_WINDOW * 2
    flags = np.array([vad.is_speech(pcm[i:i + step], sample_rate=SAMPLE_RATE)
                      for i in range(0, len(pcm), step)], dtype=float)

    # Smooth with a moving average, then widen the voiced regions.
    width = _VAD_AVERAGE_WIDTH
    padded = np.concatenate((np.zeros((width - 1) // 2), flags, np.zeros(width // 2)))
    total = np.cumsum(padded)
    total[width:] = total[width:] - total[:-width]
    mask = np.round(total[width - 1:] / width).astype(bool)
    reach = _VAD_MAX_SILENCE // 2
    mask = np.convolve(mask, np.ones(2 * reach + 1))[reach:reach + len(mask)] > 0.5
    return wav[np.repeat(mask, _VAD_WINDOW)]


def normalize_volume(wav: np.ndarray, target_dbfs: float = NORM_TARGET_DBFS,
                     increase_only: bool = False) -> np.ndarray:
    rms = np.sqrt(np.mean((wav * _INT16_MAX) ** 2))
    change = target_dbfs - 20 * np.log10(rms / _INT16_MAX)
    if change < 0 and increase_only:
        return wav
    return wav * (10 ** (change / 20))


class VoiceEncoder:
    """Drop-in for resemblyzer.VoiceEncoder.embed_utterance()."""

    def __init__(self, weights_path: Path = WEIGHTS_PATH) -> None:
        with np.load(weights_path) as w:
            # Per layer: input weights, recurrent weights, summed bias — each
            # transposed once so the forward pass is a plain `x @ W`.
            self._lstm = [
                (np.ascontiguousarray(w[f"weight_ih_l{k}"].T),
                 np.ascontiguousarray(w[f"weight_hh_l{k}"].T),
                 w[f"bias_ih_l{k}"] + w[f"bias_hh_l{k}"])
                for k in range(_LAYERS)
            ]
            self._linear_w = np.ascontiguousarray(w["linear_weight"].T)
            self._linear_b = w["linear_bias"]
        self._mel_basis = _mel_filterbank()
        # Periodic Hann, as scipy.signal.get_window("hann", n) returns.
        self._window = (0.5 - 0.5 * np.cos(
            2 * np.pi * np.arange(_N_FFT) / _N_FFT)).astype(np.float32)
        self._lock = threading.Lock()

    def _mel(self, wav: np.ndarray) -> np.ndarray:
        """Power mel spectrogram, shape (frames, 40) — not log-scaled."""
        padded = np.pad(wav.astype(np.float32), _N_FFT // 2)
        frames = np.lib.stride_tricks.sliding_window_view(padded, _N_FFT)[::_HOP]
        power = np.abs(np.fft.rfft(frames * self._window, axis=1)) ** 2
        return (power @ self._mel_basis.T).astype(np.float32)

    def _forward(self, mels: np.ndarray) -> np.ndarray:
        """(batch, frames, 40) mels -> (batch, 256) L2-normed embeddings."""
        x = mels
        batch = x.shape[0]
        h = None
        for w_ih, w_hh, bias in self._lstm:
            projected = x @ w_ih + bias              # every timestep at once
            h = np.zeros((batch, _HIDDEN), dtype=np.float32)
            c = np.zeros((batch, _HIDDEN), dtype=np.float32)
            out = np.empty((batch, x.shape[1], _HIDDEN), dtype=np.float32)
            for t in range(x.shape[1]):
                gates = projected[:, t] + h @ w_hh
                i = _sigmoid(gates[:, :_HIDDEN])
                f = _sigmoid(gates[:, _HIDDEN:2 * _HIDDEN])
                g = np.tanh(gates[:, 2 * _HIDDEN:3 * _HIDDEN])
                o = _sigmoid(gates[:, 3 * _HIDDEN:])
                c = f * c + i * g
                h = o * np.tanh(c)
                out[:, t] = h
            x = out
        raw = np.maximum(h @ self._linear_w + self._linear_b, 0)
        return raw / np.linalg.norm(raw, axis=1, keepdims=True)

    @staticmethod
    def _partial_slices(n_samples: int, rate: float, min_coverage: float) -> list[slice]:
        """Mel-frame slices of the 1.6s partials an utterance is split into."""
        n_frames = int(np.ceil((n_samples + 1) / _HOP))
        frame_step = int(np.round((SAMPLE_RATE / rate) / _HOP))
        steps = max(1, n_frames - _PARTIAL_FRAMES + frame_step + 1)
        slices = [slice(i, i + _PARTIAL_FRAMES) for i in range(0, steps, frame_step)]
        last = slices[-1]
        coverage = (n_samples - last.start * _HOP) / ((last.stop - last.start) * _HOP)
        if coverage < min_coverage and len(slices) > 1:
            slices = slices[:-1]
        return slices

    def embed_utterance(self, wav: np.ndarray, rate: float = 1.3,
                        min_coverage: float = 0.75) -> np.ndarray:
        """Embed one float32 16 kHz utterance; returns a unit vector of 256."""
        slices = self._partial_slices(len(wav), rate, min_coverage)
        needed = slices[-1].stop * _HOP
        if needed >= len(wav):
            wav = np.pad(wav, (0, needed - len(wav)))
        mel = self._mel(wav)
        with self._lock:
            partials = self._forward(np.stack([mel[s] for s in slices]))
        raw = partials.mean(axis=0)
        return raw / np.linalg.norm(raw)

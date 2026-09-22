"""Tail capture + closed-grammar recognition for launcher fast-path commands.

Flow, per wake-word hit:
  1. Read 80 ms chunks from the already-open wake-word mic stream.
  2. Sub-frame each chunk into 20 ms webrtcvad frames to find speech onset,
     trailing silence, and total speech duration.
  3. Stream the audio into a grammar-constrained Vosk recognizer as it arrives.
  4. Bail if speech runs past the cap; otherwise decode and look the phrase up.

There is deliberately no early-bail-on-partial step. The 2026-08-01 spike found
Vosk emits no partial at all for the first ~880 ms of capture — in-grammar and
out alike — so there is nothing to reject on before the cap fires anyway.

Every failure path returns None, which means "not a fast command" and lets the
launcher activate normally.
"""
from __future__ import annotations

import json
import logging
import time

import webrtcvad

from friday.config import (
    FASTPATH_LEAD_TIMEOUT_MS,
    FASTPATH_MAX_SPEECH_MS,
    FASTPATH_TRAILING_SILENCE_MS,
    FASTPATH_VAD_AGGRESSIVENESS,
)
from friday.fastpath import registry
from friday.fastpath.model import ensure_model
from friday.fastpath.registry import FastCommand

logger = logging.getLogger("friday-launcher")

SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280          # 80 ms — matches friday_launcher.AUDIO_CHUNK
CHUNK_MS = 80.0
VAD_FRAME_BYTES = 640         # 20 ms of 16-bit mono at 16 kHz
VAD_FRAMES_PER_CHUNK = 4
VOICED_FRAMES_FOR_SPEECH = 2  # majority of the 4 sub-frames

# Module-level aliases so tests can monkeypatch them per-case.
LEAD_TIMEOUT_MS = FASTPATH_LEAD_TIMEOUT_MS
TRAILING_SILENCE_MS = FASTPATH_TRAILING_SILENCE_MS
MAX_SPEECH_MS = FASTPATH_MAX_SPEECH_MS

class TailRecognizer:
    """Owns the Vosk model and decodes the speech that follows the wake word."""

    def __init__(self, download: bool = True) -> None:
        self._model = None
        self._grammar = json.dumps(registry.grammar_phrases())
        model_dir = ensure_model(download=download)
        if model_dir is None:
            logger.warning("Fast path disabled — no Vosk model")
            return
        try:
            from vosk import Model, SetLogLevel

            SetLogLevel(-1)
            self._model = Model(str(model_dir))
        except Exception as e:
            logger.warning("Vosk model load failed (%s) — fast path disabled", e)
            self._model = None
            return

        logger.info("Fast path ready — %d commands, %d grammar phrases",
                    len(registry.COMMANDS), len(registry.grammar_phrases()))

    @property
    def available(self) -> bool:
        return self._model is not None

    def _make_recognizer(self):
        from vosk import KaldiRecognizer

        return KaldiRecognizer(self._model, SAMPLE_RATE, self._grammar)

    @staticmethod
    def _chunk_is_voiced(vad, raw: bytes) -> bool:
        voiced = 0
        for i in range(VAD_FRAMES_PER_CHUNK):
            frame = raw[i * VAD_FRAME_BYTES:(i + 1) * VAD_FRAME_BYTES]
            if len(frame) != VAD_FRAME_BYTES:
                break
            if vad.is_speech(frame, SAMPLE_RATE):
                voiced += 1
        return voiced >= VOICED_FRAMES_FOR_SPEECH

    def capture_and_match(self, read_chunk) -> FastCommand | None:
        """Capture the tail after the wake word and resolve it to a command.

        `read_chunk` is a callable returning CHUNK_SAMPLES of raw int16 bytes.
        Returns the matched FastCommand, or None for "not a fast command".
        """
        if not self.available:
            return None
        started = time.monotonic()
        try:
            cmd, diag = self._capture(read_chunk)
        except Exception as e:
            logger.warning("fastpath: tail capture failed (%s)", e)
            return None
        total_ms = (time.monotonic() - started) * 1000.0
        logger.info(
            "fastpath: matched=%s heard=%r bail=%s lead_ms=%.0f speech_ms=%.0f "
            "decode_ms=%.0f total_ms=%.0f",
            cmd.name if cmd else "-", diag["text"], diag["bail"],
            diag["lead_ms"], diag["speech_ms"], diag["decode_ms"], total_ms,
        )
        return cmd

    def _capture(self, read_chunk) -> tuple[FastCommand | None, dict]:
        vad = webrtcvad.Vad(FASTPATH_VAD_AGGRESSIVENESS)
        rec = self._make_recognizer()

        speech_started = False
        lead_ms = 0.0
        speech_ms = 0.0
        silence_ms = 0.0
        diag = {"text": "", "bail": None, "lead_ms": 0.0,
                "speech_ms": 0.0, "decode_ms": 0.0}

        while True:
            raw = read_chunk()
            voiced = self._chunk_is_voiced(vad, raw)

            if not speech_started:
                if voiced:
                    speech_started = True
                    rec.AcceptWaveform(raw)
                    speech_ms += CHUNK_MS
                    continue
                lead_ms += CHUNK_MS
                if lead_ms >= LEAD_TIMEOUT_MS:
                    diag.update(bail="no-speech", lead_ms=lead_ms)
                    return None, diag        # bare wake word, nothing followed
                continue

            rec.AcceptWaveform(raw)
            if voiced:
                speech_ms += CHUNK_MS
                silence_ms = 0.0
            else:
                silence_ms += CHUNK_MS
                if silence_ms >= TRAILING_SILENCE_MS:
                    break                    # endpoint reached

            if speech_ms >= MAX_SPEECH_MS:
                diag.update(bail="too-long", lead_ms=lead_ms, speech_ms=speech_ms)
                return None, diag            # too long to be a fast command

        decode_started = time.monotonic()
        diag.update(lead_ms=lead_ms, speech_ms=speech_ms)
        try:
            text = json.loads(rec.FinalResult()).get("text", "")
        except Exception:
            diag.update(bail="bad-json",
                        decode_ms=(time.monotonic() - decode_started) * 1000.0)
            return None, diag
        diag.update(text=text,
                    decode_ms=(time.monotonic() - decode_started) * 1000.0)
        cmd = registry.lookup(text)
        if cmd is None:
            diag["bail"] = "no-match"
        return cmd, diag

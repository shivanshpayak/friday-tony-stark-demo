"""Tests for tail capture, VAD endpointing, and closed-grammar decode.

Vosk and webrtcvad are both stubbed so these run without the model or a mic.
Audio is represented as raw bytes; the fakes key off content, not acoustics.
"""
import json

import pytest

import friday.fastpath.recognizer as rec_mod

SPEECH = b"\x01\x01" * rec_mod.CHUNK_SAMPLES
SILENCE = b"\x00\x00" * rec_mod.CHUNK_SAMPLES


class FakeVad:
    """Treats any non-zero frame as speech."""

    def __init__(self, _aggressiveness):
        pass

    def is_speech(self, frame, _rate):
        return any(frame)


class FakeRecognizer:
    """Returns a scripted final text regardless of audio."""

    def __init__(self, final=""):
        self._final = final
        self.accepted = 0

    def AcceptWaveform(self, _data):
        self.accepted += 1
        return False

    def FinalResult(self):
        return json.dumps({"text": self._final})


def make_reader(chunks):
    """Return a read_chunk callable yielding `chunks`, then silence forever."""
    it = iter(chunks)

    def read_chunk():
        try:
            return next(it)
        except StopIteration:
            return SILENCE

    return read_chunk


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(rec_mod.webrtcvad, "Vad", FakeVad)
    return monkeypatch


def build(fake_rec):
    r = rec_mod.TailRecognizer.__new__(rec_mod.TailRecognizer)
    r._model = object()
    r._make_recognizer = lambda: fake_rec
    return r


def test_no_speech_within_lead_timeout_returns_none(patched):
    r = build(FakeRecognizer())
    # 300ms lead timeout / 80ms per chunk → 4 silent chunks is enough.
    assert r.capture_and_match(make_reader([SILENCE] * 10)) is None


def test_trailing_silence_ends_capture_and_matches(patched):
    r = build(FakeRecognizer(final="mute"))
    result = r.capture_and_match(make_reader([SPEECH] * 4 + [SILENCE] * 5))
    assert result is not None
    assert result.name == "mute"


def test_speech_cap_abandons_without_decoding(patched):
    # 1000ms cap / 80ms = 13 chunks. Feed 20 straight speech chunks.
    fake = FakeRecognizer(final="mute")
    r = build(fake)
    assert r.capture_and_match(make_reader([SPEECH] * 20)) is None


def test_unknown_final_returns_none(patched):
    r = build(FakeRecognizer(final="[unk]"))
    assert r.capture_and_match(make_reader([SPEECH] * 4 + [SILENCE] * 5)) is None


def test_multiword_command_matches(patched):
    r = build(FakeRecognizer(final="next track"))
    result = r.capture_and_match(make_reader([SPEECH] * 4 + [SILENCE] * 5))
    assert result is not None
    assert result.name == "next_track"


def test_audio_is_streamed_in_not_batched(patched):
    """Every captured chunk should reach the recognizer as it arrives."""
    fake = FakeRecognizer(final="mute")
    r = build(fake)
    r.capture_and_match(make_reader([SPEECH] * 4 + [SILENCE] * 5))
    # 4 speech chunks + 4 silence chunks before the 300ms endpoint trips.
    assert fake.accepted >= 4


def test_stream_read_error_returns_none_not_raises(patched):
    def exploding_reader():
        raise OSError("stream died")

    r = build(FakeRecognizer(final="mute"))
    assert r.capture_and_match(exploding_reader) is None


def test_malformed_final_json_returns_none(patched):
    class BadRecognizer(FakeRecognizer):
        def FinalResult(self):
            return "not json"

    r = build(BadRecognizer())
    assert r.capture_and_match(make_reader([SPEECH] * 4 + [SILENCE] * 5)) is None


def test_unavailable_recognizer_returns_none_without_reading():
    r = rec_mod.TailRecognizer.__new__(rec_mod.TailRecognizer)
    r._model = None
    reads = []

    def counting_reader():
        reads.append(1)
        return SILENCE

    assert r.capture_and_match(counting_reader) is None
    assert reads == []

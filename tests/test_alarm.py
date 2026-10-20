"""The alarm a timer rings when it fires while JARVIS is idle (friday/alarm.py).

Before it, a timer firing while idle spoke through a session nobody had opened:
the overlay went to "speaking" on the agent's PROCESSING/SPEAKING lines and
nothing ever put it away, so it sat there glowing until JARVIS was woken just
to be told "dismissed". Vosk and webrtcvad are stubbed; audio is raw bytes the fakes
key off.
"""
import json
import wave
from datetime import datetime, timedelta

import friday.alarm as alarm
from tests.test_session_recovery import _patch_and_import_launcher

CHUNK = 1280                       # 80 ms of 16 kHz int16
SPEECH = b"\x01\x01" * CHUNK
SILENCE = b"\x00\x00" * CHUNK


# --- stop phrases -------------------------------------------------------------

def test_the_usual_dismissal_stops_it():
    assert alarm.stop_phrase_in("Dismissed.") == "dismissed"
    assert alarm.stop_phrase_in("Stop") == "stop"
    assert alarm.stop_phrase_in("that will be all") == "that will be all"


def test_stop_phrase_anywhere_among_unknown_words():
    assert alarm.stop_phrase_in("[unk] stop") == "stop"
    assert alarm.stop_phrase_in("okay thank you") == "thank you"


def test_other_speech_is_not_a_stop():
    for text in ("", "[unk]", "[unk] [unk]", "stopwatch", "what time is it"):
        assert alarm.stop_phrase_in(text) is None, text


def test_grammar_is_the_phrases_plus_unknown():
    g = alarm.grammar()
    assert g[-1] == "[unk]"
    assert "stop" in g and "dismissed" in g
    # Vosk has no "that'll" and would silently drop the phrase.
    assert not any("that'll" in phrase for phrase in g)


# --- ring or just say it ------------------------------------------------------

def test_a_timer_that_just_came_due_rings():
    now = datetime(2026, 10, 20, 3, 3, 12)
    assert alarm.should_ring("timer", (now - timedelta(seconds=1)).isoformat(), now)


def test_a_reminder_is_said_once():
    now = datetime(2026, 10, 20, 3, 3, 12)
    assert not alarm.should_ring("reminder", now.isoformat(), now)


def test_a_timer_overdue_from_before_boot_does_not_ring():
    now = datetime(2026, 10, 20, 9, 0, 0)
    assert not alarm.should_ring("timer", (now - timedelta(hours=8)).isoformat(), now)


def test_a_timer_without_a_usable_time_rings():
    assert alarm.should_ring("timer", "")


# --- the chime ----------------------------------------------------------------

def test_clip_is_dim_and_the_reported_length(tmp_path):
    path = tmp_path / "alarm.wav"
    seconds = alarm.build_clip(path, volume=0.2)
    with wave.open(str(path), "rb") as wf:
        frames = wf.readframes(wf.getnframes())
        assert abs(wf.getnframes() / wf.getframerate() - seconds) < 0.01
    samples = memoryview(frames).cast("h")
    peak = max(abs(s) for s in samples)
    assert 0 < peak <= 0.2 * 32767 + 1


def test_ringer_replays_until_stopped_then_cuts_the_sound():
    played, stopped = [], []

    def play(path):
        played.append(path)
        return f"handle{len(played)}"

    ringer = alarm.Ringer("clip.wav", 0.01, play=play, stop=stopped.append)
    ringer.start()
    deadline = datetime.now() + timedelta(seconds=2)
    while len(played) < 3 and datetime.now() < deadline:
        pass
    ringer.stop()
    assert len(played) >= 3
    assert stopped == [f"handle{len(played)}"]


def test_ringer_survives_a_missing_player():
    def play(_path):
        raise RuntimeError("no audio player found")

    ringer = alarm.Ringer("clip.wav", 0.01, play=play, stop=lambda h: None)
    ringer.start()
    ringer.stop()        # returns; nothing raised on this thread


# --- listening for the stop ---------------------------------------------------

class FakeVad:
    def is_speech(self, frame, _rate):
        return any(frame)


class FakeRecognizer:
    def __init__(self, final):
        self.final = final
        self.accepted = []

    def AcceptWaveform(self, data):
        self.accepted.append(data)
        return False

    def FinalResult(self):
        return json.dumps({"text": self.final})


def _listener(finals):
    """A StopListener whose successive utterances decode to `finals`."""
    made = []
    finals = iter(finals)

    def make():
        rec = FakeRecognizer(next(finals, "[unk]"))
        made.append(rec)
        return rec

    listener = alarm.StopListener(make)
    listener._new_vad = FakeVad
    return listener, made


def _reader(chunks):
    it = iter(chunks)
    return lambda: next(it, SILENCE)


def _done_after(reads):
    count = [0]

    def done():
        count[0] += 1
        return count[0] > reads

    return done


def test_hears_a_stop_phrase():
    listener, _ = _listener(["stop"])
    read = _reader([SILENCE, SPEECH, SPEECH] + [SILENCE] * 6)
    assert listener.wait(read, _done_after(50)) == "stop"


def test_anything_else_is_ignored_until_done():
    listener, made = _listener(["[unk]", "[unk]"])
    utterance = [SPEECH, SPEECH] + [SILENCE] * 6
    read = _reader(utterance * 2)
    assert listener.wait(read, _done_after(40)) is None
    assert len(made) == 2          # both utterances were decoded, neither acted on


def test_pre_roll_keeps_the_start_of_the_word():
    """The "s" of "stop" reads as unvoiced; the chunks before onset go in too."""
    quiet_s = b"\x00\x00" * CHUNK
    listener, made = _listener(["stop"])
    read = _reader([quiet_s] * 5 + [SPEECH] + [SILENCE] * 6)
    listener.wait(read, _done_after(50))
    assert made[0].accepted[:alarm.PRE_ROLL_CHUNKS] == [quiet_s] * alarm.PRE_ROLL_CHUNKS


def test_a_never_ending_sound_is_still_decoded():
    """The chime itself can read as voiced; the cap forces a decode."""
    listener, _ = _listener(["stop"])
    read = _reader([SPEECH] * 200)
    assert listener.wait(read, _done_after(200)) == "stop"


# --- launcher protocol --------------------------------------------------------

def test_scheduled_fired_line_reaches_its_callback():
    fl = _patch_and_import_launcher()
    got = []
    agent = fl.AgentProcess(on_scheduled_fired=got.append)
    agent._dispatch_line('SCHEDULED_FIRED {"kind": "timer", "message": "Your timer is up, sir.",'
                         ' "fire_at": "2026-10-20T03:03:12"}')
    assert got == [{"kind": "timer", "message": "Your timer is up, sir.",
                    "fire_at": "2026-10-20T03:03:12"}]


def test_a_message_naming_a_signal_does_not_fire_it():
    fl = _patch_and_import_launcher()
    speaking = []
    agent = fl.AgentProcess(on_speaking=lambda: speaking.append(True),
                            on_scheduled_fired=lambda fired: None)
    agent._dispatch_line('SCHEDULED_FIRED {"kind": "reminder", "message": "SPEAKING notes"}')
    assert speaking == []


def test_announce_done_reaches_its_callback():
    fl = _patch_and_import_launcher()
    calls = []
    agent = fl.AgentProcess(on_announce_done=lambda: calls.append(True))
    agent._dispatch_line("ANNOUNCE_DONE")
    assert calls == [True]

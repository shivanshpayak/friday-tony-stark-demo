"""A turn starts and ends on words, so steady noise can't hold it open.

Deepgram's endpoint waits for silence; a fan is never silent, so a finished
request sat unanswered until the mic was muted.
"""
import asyncio

from livekit.agents import stt

from friday.turn_end import end_turn_on_words

T = stt.SpeechEventType
QUIET = 0.15


def _ev(kind, text=None):
    alts = [stt.SpeechData(language="en", text=text)] if text is not None else []
    return stt.SpeechEvent(type=kind, alternatives=alts)


def _run(script, hold=None):
    """script: (delay_before_seconds, event) pairs. Returns [(type, text)]."""
    async def source():
        for delay, ev in script:
            await asyncio.sleep(delay)
            yield ev
        await asyncio.sleep(max(QUIET, hold or 0) * 3)   # the stream stays open, like a mic

    async def drain():
        return [(e.type, e.alternatives[0].text if e.alternatives else None)
                async for e in end_turn_on_words(source(), QUIET, hold)]

    return asyncio.run(drain())


def test_fan_noise_no_endpoint_the_turn_still_ends():
    """Words, then nothing but noise: Deepgram never sends its endpoint."""
    out = _run([(0, _ev(T.START_OF_SPEECH)),
                (0, _ev(T.INTERIM_TRANSCRIPT, "open")),
                (0, _ev(T.FINAL_TRANSCRIPT, "Open Chrome."))])
    assert [t for t, _ in out] == [T.START_OF_SPEECH, T.INTERIM_TRANSCRIPT,
                                   T.FINAL_TRANSCRIPT, T.END_OF_SPEECH]


def test_quiet_room_deepgrams_own_endpoint_is_used_once():
    out = _run([(0, _ev(T.START_OF_SPEECH)),
                (0, _ev(T.FINAL_TRANSCRIPT, "Open Chrome.")),
                (0.02, _ev(T.END_OF_SPEECH))])
    assert [t for t, _ in out].count(T.END_OF_SPEECH) == 1


def test_more_words_keep_the_turn_open():
    """A final transcript mid-sentence must not end the turn while he talks."""
    out = _run([(0, _ev(T.FINAL_TRANSCRIPT, "Remind me to")),
                (QUIET * 0.6, _ev(T.INTERIM_TRANSCRIPT, "call")),
                (QUIET * 0.6, _ev(T.INTERIM_TRANSCRIPT, "call mom")),
                (QUIET * 0.6, _ev(T.FINAL_TRANSCRIPT, "call mom at six."))])
    kinds = [t for t, _ in out]
    assert kinds.count(T.END_OF_SPEECH) == 1 and kinds[-1] == T.END_OF_SPEECH
    assert kinds.count(T.START_OF_SPEECH) == 1


def test_noise_alone_never_starts_or_ends_a_turn():
    """A "speech started" from the fan cancelled the reply to a finished turn."""
    out = _run([(0, _ev(T.START_OF_SPEECH)), (QUIET * 2, _ev(T.START_OF_SPEECH))])
    assert out == []


def test_late_endpoint_after_the_turn_was_ended_here_is_dropped():
    """Muting later gives Deepgram its silence; that must not end a turn twice."""
    out = _run([(0, _ev(T.FINAL_TRANSCRIPT, "Open Chrome.")),
                (QUIET * 2, _ev(T.END_OF_SPEECH)),
                (0, _ev(T.FINAL_TRANSCRIPT, "Close it.")),
                (0.02, _ev(T.END_OF_SPEECH))])
    assert [t for t, _ in out] == [
        T.START_OF_SPEECH, T.FINAL_TRANSCRIPT, T.END_OF_SPEECH,
        T.START_OF_SPEECH, T.FINAL_TRANSCRIPT, T.END_OF_SPEECH]


def test_words_that_stop_changing_are_finalised_without_deepgram():
    """Under noise Deepgram sits on the final transcript for seconds too."""
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "open")),
                (0.02, _ev(T.INTERIM_TRANSCRIPT, "Open Chrome"))])
    assert out == [(T.START_OF_SPEECH, None), (T.INTERIM_TRANSCRIPT, "open"),
                   (T.INTERIM_TRANSCRIPT, "Open Chrome"),
                   (T.FINAL_TRANSCRIPT, "Open Chrome"), (T.END_OF_SPEECH, None)]


def test_repeated_identical_interims_do_not_keep_the_turn_open():
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "Open Chrome")),
                (QUIET * 0.7, _ev(T.INTERIM_TRANSCRIPT, "Open Chrome")),
                (QUIET * 0.7, _ev(T.INTERIM_TRANSCRIPT, "Open Chrome"))])
    kinds = [t for t, _ in out]
    assert kinds.count(T.FINAL_TRANSCRIPT) == 1 and kinds.count(T.END_OF_SPEECH) == 1


def test_deepgrams_late_final_for_the_same_words_is_not_a_second_request():
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "Open Chrome")),
                (QUIET * 2, _ev(T.INTERIM_TRANSCRIPT, "open chrome")),
                (0.02, _ev(T.FINAL_TRANSCRIPT, "Open Chrome.")),
                (0.02, _ev(T.END_OF_SPEECH))])
    assert out == [(T.START_OF_SPEECH, None), (T.INTERIM_TRANSCRIPT, "Open Chrome"),
                   (T.FINAL_TRANSCRIPT, "Open Chrome"), (T.END_OF_SPEECH, None)]


def test_speech_that_resumes_in_the_same_stretch_keeps_only_the_new_words():
    """He paused past the limit, then went on: the late final repeats the part
    already answered and adds the rest."""
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "Open Chrome")),
                (QUIET * 2, _ev(T.FINAL_TRANSCRIPT, "Open Chrome and close Spotify.")),
                (0.02, _ev(T.END_OF_SPEECH))])
    assert out[4:] == [(T.START_OF_SPEECH, None),
                       (T.FINAL_TRANSCRIPT, "and close Spotify."), (T.END_OF_SPEECH, None)]


def test_a_new_request_after_a_promoted_one_passes_whole():
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "Open Chrome")),
                (QUIET * 2, _ev(T.FINAL_TRANSCRIPT, "What time is it?")),
                (0.02, _ev(T.END_OF_SPEECH))])
    assert out[4:] == [(T.START_OF_SPEECH, None),
                       (T.FINAL_TRANSCRIPT, "What time is it?"), (T.END_OF_SPEECH, None)]


def test_stt_errors_still_surface():
    async def broken():
        yield _ev(T.FINAL_TRANSCRIPT, "Open")
        raise RuntimeError("deepgram connection closed")

    async def drain():
        return [e async for e in end_turn_on_words(broken(), QUIET)]

    try:
        asyncio.run(drain())
        raise AssertionError("expected the error to propagate")
    except RuntimeError:
        pass


# --- Deepgram going back over audio it already reported --------------------
# Real Deepgram words carry audio times; these reproduce the logged cases.

from livekit.agents.types import TimedString

import friday.turn_end as turn_end


def _tev(kind, text, start, step=0.3):
    """A transcript whose words are timed from `start`, `step` seconds apart."""
    words = [TimedString(w, start_time=start + i * step, end_time=start + i * step + step * 0.9)
             for i, w in enumerate(text.split())]
    return stt.SpeechEvent(type=kind, alternatives=[
        stt.SpeechData(language="en", text=text, start_time=start, words=words)])


def _finals(out):
    return [text for kind, text in out if kind == T.FINAL_TRANSCRIPT]


def test_a_copy_with_extra_words_in_front_is_not_a_second_timer():
    """Logged 10-18: one "set a timer" became two timers."""
    out = _run([(0, _tev(T.FINAL_TRANSCRIPT, "Set a timer for thirty minutes.", 10.0)),
                (0.02, _ev(T.END_OF_SPEECH)),
                (0.02, _tev(T.INTERIM_TRANSCRIPT, "are of Set a timer for thirty minutes.", 9.4)),
                (QUIET * 2, _tev(T.INTERIM_TRANSCRIPT, "are Set a timer for thirty minutes.", 9.7)),
                (QUIET * 2, _tev(T.FINAL_TRANSCRIPT, "of Set a timer for thirty minutes.", 9.7))])
    assert _finals(out) == ["Set a timer for thirty minutes."]


def test_pauses_while_remembering_lyrics_send_each_word_once():
    """Logged 10-18: "No." and "It's an upbeat" were each promoted, then
    Deepgram's late transcript repeated both and added the rest."""
    out = _run([(0, _tev(T.INTERIM_TRANSCRIPT, "No.", 1.0)),
                (QUIET * 2, _tev(T.INTERIM_TRANSCRIPT, "It's an upbeat", 2.0)),
                (QUIET * 2, _tev(T.FINAL_TRANSCRIPT,
                                 "No. It's an upbeat song. It goes like,", 1.0, step=0.5))])
    assert _finals(out) == ["No.", "It's an upbeat", "song. It goes like,"]


def test_saying_the_same_thing_again_later_still_counts():
    """He repeats himself when the first try failed; that is new audio."""
    out = _run([(0, _tev(T.FINAL_TRANSCRIPT, "Close Spotify.", 5.0)),
                (0.02, _ev(T.END_OF_SPEECH)),
                (0.02, _tev(T.FINAL_TRANSCRIPT, "Close Spotify.", 15.0)),
                (0.02, _ev(T.END_OF_SPEECH))])
    assert _finals(out) == ["Close Spotify.", "Close Spotify."]


def test_a_reconnected_stream_restarting_its_clock_is_not_mistaken_for_a_copy():
    out = _run([(0, _tev(T.FINAL_TRANSCRIPT, "Set a timer for thirty minutes.", 20.0)),
                (0.02, _ev(T.END_OF_SPEECH)),
                (0.02, _tev(T.FINAL_TRANSCRIPT, "Open Chrome.", 0.5)),
                (0.02, _ev(T.END_OF_SPEECH))])
    assert _finals(out) == ["Set a timer for thirty minutes.", "Open Chrome."]


def test_heard_words_are_forgotten_after_a_while(monkeypatch):
    monkeypatch.setattr(turn_end, "_MEMORY", QUIET)
    out = _run([(0, _tev(T.FINAL_TRANSCRIPT, "Open Chrome.", 20.0)),
                (0.02, _ev(T.END_OF_SPEECH)),
                (QUIET * 2, _tev(T.FINAL_TRANSCRIPT, "Open Chrome.", 0.5)),
                (0.02, _ev(T.END_OF_SPEECH))])
    assert _finals(out) == ["Open Chrome.", "Open Chrome."]


def test_untimed_promotions_add_up_until_deepgrams_final():
    """Same lyrics case from an STT without word times: the text fallback."""
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "No.")),
                (QUIET * 2, _ev(T.INTERIM_TRANSCRIPT, "No. It's an upbeat")),
                (QUIET * 2, _ev(T.FINAL_TRANSCRIPT, "No. It's an upbeat song."))])
    assert _finals(out) == ["No.", "It's an upbeat", "song."]



# --- waiting when the sentence is left hanging ---------------------------------

HOLD = QUIET * 3


def _turns(out):
    return [t for t, _ in out].count(T.END_OF_SPEECH)


def test_a_pause_after_play_does_not_end_the_turn():
    """Logged 10-19: "Play" (pause) toggled the music instead of waiting."""
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "Play")),
                (QUIET * 2, _ev(T.INTERIM_TRANSCRIPT, "Play my Night Car playlist"))], hold=HOLD)
    assert _finals(out) == ["Play my Night Car playlist"] and _turns(out) == 1


def test_deepgrams_endpoint_waits_on_a_hanging_word_too():
    out = _run([(0, _ev(T.FINAL_TRANSCRIPT, "Keep skipping until you get to")),
                (0.02, _ev(T.END_OF_SPEECH)),
                (QUIET * 2, _ev(T.FINAL_TRANSCRIPT, "Attractive by Jordan.")),
                (0.02, _ev(T.END_OF_SPEECH))], hold=HOLD)
    assert _turns(out) == 1
    assert _finals(out) == ["Keep skipping until you get to", "Attractive by Jordan."]


def test_a_hanging_word_with_nothing_after_still_ends_the_turn():
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "Play"))], hold=HOLD)
    assert _finals(out) == ["Play"] and _turns(out) == 1


def test_a_finished_sentence_does_not_wait_longer():
    out = _run([(0, _ev(T.INTERIM_TRANSCRIPT, "Next song")),
                (QUIET * 2, _ev(T.INTERIM_TRANSCRIPT, "Pause"))], hold=HOLD)
    assert _finals(out) == ["Next song", "Pause"] and _turns(out) == 2

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


def _run(script):
    """script: (delay_before_seconds, event) pairs. Returns [(type, text)]."""
    async def source():
        for delay, ev in script:
            await asyncio.sleep(delay)
            yield ev
        await asyncio.sleep(QUIET * 3)       # the stream stays open, like a mic

    async def drain():
        return [(e.type, e.alternatives[0].text if e.alternatives else None)
                async for e in end_turn_on_words(source(), QUIET)]

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

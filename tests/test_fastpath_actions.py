"""Tests for fast-path command actions."""
import friday.fastpath.actions as actions
import friday.media_control as mc


def test_mute_sets_mute_true(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "set_master_mute", lambda v: calls.append(v))
    actions.mute()
    assert calls == [True]


def test_unmute_sets_mute_false(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "set_master_mute", lambda v: calls.append(v))
    actions.unmute()
    assert calls == [False]


def test_play_pause_delegates(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "play_pause_media", lambda: calls.append("pp"))
    actions.play_pause()
    assert calls == ["pp"]


def test_next_track_delegates(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "next_track", lambda: calls.append("next"))
    actions.next_track()
    assert calls == ["next"]


def test_previous_track_delegates(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "previous_track", lambda: calls.append("prev"))
    actions.previous_track()
    assert calls == ["prev"]


def test_stop_delegates(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "stop_media", lambda: calls.append("stop"))
    actions.stop_media()
    assert calls == ["stop"]


def test_louder_steps_up(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "adjust_master_volume", lambda d: calls.append(d))
    actions.louder()
    assert calls == [actions.VOLUME_STEP]


def test_quieter_steps_down(monkeypatch):
    calls = []
    monkeypatch.setattr(mc, "adjust_master_volume", lambda d: calls.append(d))
    actions.quieter()
    assert calls == [-actions.VOLUME_STEP]

"""Tests for the shared media control layer."""
import friday.media_control as mc


def test_play_pause_sends_media_key(monkeypatch):
    sent = []
    monkeypatch.setattr(mc.keyboard, "send", lambda k: sent.append(k))
    mc.play_pause_media()
    assert sent == ["play/pause media"]


def test_next_track_sends_media_key(monkeypatch):
    sent = []
    monkeypatch.setattr(mc.keyboard, "send", lambda k: sent.append(k))
    mc.next_track()
    assert sent == ["next track"]


def test_previous_track_sends_media_key(monkeypatch):
    sent = []
    monkeypatch.setattr(mc.keyboard, "send", lambda k: sent.append(k))
    mc.previous_track()
    assert sent == ["previous track"]


def test_stop_media_sends_media_key(monkeypatch):
    sent = []
    monkeypatch.setattr(mc.keyboard, "send", lambda k: sent.append(k))
    mc.stop_media()
    assert sent == ["stop media"]


def test_set_master_mute_true(monkeypatch):
    calls = []

    class FakeVolume:
        def SetMute(self, value, _guid):
            calls.append(value)

    monkeypatch.setattr(mc, "_endpoint_volume", lambda: FakeVolume())
    mc.set_master_mute(True)
    assert calls == [1]


def test_set_master_mute_false(monkeypatch):
    calls = []

    class FakeVolume:
        def SetMute(self, value, _guid):
            calls.append(value)

    monkeypatch.setattr(mc, "_endpoint_volume", lambda: FakeVolume())
    mc.set_master_mute(False)
    assert calls == [0]


def test_adjust_master_volume_clamps_high(monkeypatch):
    monkeypatch.setattr(mc, "_get_master_volume", lambda: 0.95)
    applied = []
    monkeypatch.setattr(mc, "_set_master_volume", lambda f: applied.append(f))
    mc.adjust_master_volume(20)
    assert applied == [1.0]


def test_adjust_master_volume_clamps_low(monkeypatch):
    monkeypatch.setattr(mc, "_get_master_volume", lambda: 0.05)
    applied = []
    monkeypatch.setattr(mc, "_set_master_volume", lambda f: applied.append(f))
    mc.adjust_master_volume(-20)
    assert applied == [0.0]


def test_adjust_master_volume_steps(monkeypatch):
    monkeypatch.setattr(mc, "_get_master_volume", lambda: 0.50)
    applied = []
    monkeypatch.setattr(mc, "_set_master_volume", lambda f: applied.append(f))
    mc.adjust_master_volume(10)
    assert applied == [0.60]

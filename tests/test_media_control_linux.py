"""Linux backend of the media control layer: PipeWire (wpctl / pactl) for
volume, MPRIS over D-Bus for transport. Every command is captured — nothing
here touches the real mixer or a real player."""
import json
import sys

import pytest

import friday.media_control as mc

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Linux backend")


@pytest.fixture
def run(monkeypatch):
    """Record every command; answer from a canned {argv-prefix: stdout} table."""
    calls, replies = [], {}

    def fake(argv):
        calls.append(argv)
        for prefix, out in replies.items():
            if tuple(argv[:len(prefix)]) == prefix:
                return out
        return ""

    monkeypatch.setattr(mc, "_run", fake)
    fake.calls, fake.replies = calls, replies
    return fake


def test_mute_is_an_explicit_set_not_a_toggle(run):
    assert mc.set_master_mute(True) == "Muted."
    assert mc.set_master_mute(False) == "Unmuted."
    assert run.calls == [
        ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
        ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
    ]


def test_master_volume_reads_through_the_muted_suffix(run):
    run.replies[("wpctl", "get-volume")] = "Volume: 0.40 [MUTED]\n"
    assert mc._get_master_volume() == pytest.approx(0.40)


def test_set_master_volume_clamps(run):
    mc._set_master_volume(1.7)
    assert run.calls == [["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", "1.00"]]


def test_transport_prefers_the_player_that_is_playing(run):
    run.replies[("busctl", "--user", "list")] = (
        "org.mpris.MediaPlayer2.firefox 1 x\n"
        "org.freedesktop.Notifications 2 y\n"
        "org.mpris.MediaPlayer2.spotify 3 z\n"
    )
    run.replies[("busctl", "--user", "get-property",
                 "org.mpris.MediaPlayer2.spotify")] = 's "Playing"\n'
    run.replies[("busctl", "--user", "get-property",
                 "org.mpris.MediaPlayer2.firefox")] = 's "Paused"\n'

    assert mc.next_track() == "Skipped to next track."
    assert run.calls[-1] == [
        "busctl", "--user", "call", "org.mpris.MediaPlayer2.spotify",
        "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player", "Next",
    ]


def test_transport_without_any_player_says_so(run):
    run.replies[("busctl", "--user", "list")] = "org.freedesktop.Notifications 2 y\n"
    assert mc.play_pause_media() == "Nothing is playing right now."
    assert not any("call" in c for c in run.calls)


def test_now_playing_formats_artist_and_title(run):
    run.replies[("busctl", "--user", "list")] = "org.mpris.MediaPlayer2.spotify 3 z\n"
    run.replies[("busctl", "--user", "--json=short")] = json.dumps({"data": {
        "xesam:title": {"type": "s", "data": "Back in Black"},
        "xesam:artist": {"type": "as", "data": ["AC/DC"]},
    }})
    assert mc.now_playing("spotify") == "AC/DC - Back in Black"
    assert mc.now_playing("vlc") is None


def test_app_volume_targets_the_matching_stream(run):
    run.replies[("pactl", "--format=json")] = json.dumps([
        {"index": 7, "properties": {"application.name": "Firefox"},
         "volume": {"front-left": {"value_percent": "80%"}}},
        {"index": 9, "properties": {"application.name": "spotify"},
         "volume": {"front-left": {"value_percent": "40%"},
                    "front-right": {"value_percent": "60%"}}},
    ])
    assert mc._get_app_volume("Spotify") == (pytest.approx(0.5), "spotify")
    assert mc._set_app_volume("spotify", 0.3) == "spotify volume set to 30%."
    assert run.calls[-1] == ["pactl", "set-sink-input-volume", "9", "30%"]
    assert "Couldn't find" in mc._set_app_volume("vlc", 0.3)


# --- ducking: music turned down while an alarm rings -------------------------

def _stream(index, client, app, volume, corked=False, pid=None):
    props = {"application.name": app}
    if pid is not None:
        props["application.process.id"] = str(pid)
    return {"index": index, "client": str(client), "corked": corked, "properties": props,
            "volume": {f"ch{i}": {"value": v, "value_percent": f"{v * 100 // 65536}%"}
                       for i, v in enumerate(volume)}}


JARVIS_PID = 20888


def _mixer(run, streams):
    run.replies[("pactl", "--format=json", "list", "sink-inputs")] = json.dumps(streams)
    run.replies[("pactl", "--format=json", "list", "clients")] = json.dumps([
        # The agent's voice is an ALSA stream: no pid of its own, only its client's.
        {"index": 179, "properties": {"application.process.id": str(JARVIS_PID)}},
        {"index": 50, "properties": {"application.process.id": "4242"}},
    ])


def _volume_sets(run):
    return [c for c in run.calls if c[:2] == ["pactl", "set-sink-input-volume"]]


def test_duck_lowers_music_and_spares_jarvis_and_its_chimes(run):
    _mixer(run, [
        _stream(10, 50, "spotify", [65536, 65536]),
        _stream(11, 179, "PipeWire ALSA [python3.11]", [65536]),       # JARVIS's voice
        _stream(12, 60, "pw-play", [65536]),                          # a chime
        _stream(13, 50, "Firefox", [32768], corked=True),             # paused video
    ])
    ducked = mc.duck_playback(0.25, frozenset({JARVIS_PID}))
    assert _volume_sets(run) == [["pactl", "set-sink-input-volume", "10", "16384", "16384"]]
    assert ducked == [{"id": 10, "volume": [65536, 65536], "ducked": [16384, 16384]}]


def test_unduck_puts_the_exact_volume_back(run):
    _mixer(run, [_stream(10, 50, "spotify", [16384, 16384])])
    restored = mc.unduck_playback(
        [{"id": 10, "volume": [65536, 52428], "ducked": [16384, 16384]}])
    assert restored == 1
    assert _volume_sets(run) == [["pactl", "set-sink-input-volume", "10", "65536", "52428"]]


def test_unduck_leaves_a_volume_changed_by_hand(run):
    _mixer(run, [_stream(10, 50, "spotify", [40000, 40000])])
    assert mc.unduck_playback(
        [{"id": 10, "volume": [65536, 65536], "ducked": [16384, 16384]}]) == 0
    assert _volume_sets(run) == []


def test_unduck_skips_a_stream_that_has_gone(run):
    _mixer(run, [])
    assert mc.unduck_playback(
        [{"id": 10, "volume": [65536], "ducked": [16384]}]) == 0
    assert _volume_sets(run) == []

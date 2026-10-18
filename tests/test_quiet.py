"""Quiet mode: JARVIS must never speak on a school network unless told to."""
import types

import pytest

from friday import quiet


@pytest.fixture
def wifi(monkeypatch, tmp_path):
    """Isolated state file and a settable 'current network'."""
    monkeypatch.setattr(quiet, "STATE_FILE", tmp_path / "quiet.json")
    current = {"ssid": "Home"}
    monkeypatch.setattr(quiet, "current_ssid", lambda *, fresh=False: current["ssid"])
    return current


def test_normal_everywhere_until_a_school_network_is_marked(wifi):
    assert quiet.is_quiet() is False
    wifi["ssid"] = "School-WiFi"
    assert quiet.is_quiet() is False
    assert quiet.add_school() == "School-WiFi"
    assert quiet.is_quiet() is True
    wifi["ssid"] = "Home"
    assert quiet.is_quiet() is False


def test_allowing_speech_at_school_does_not_outlive_the_network(wifi):
    wifi["ssid"] = "School-WiFi"
    quiet.add_school()
    quiet.set_override(False)            # "you may talk here"
    assert quiet.is_quiet() is False
    wifi["ssid"] = "Home"                # went home...
    wifi["ssid"] = "School-WiFi"         # ...back the next morning
    assert quiet.is_quiet() is False     # same network: the grant still stands
    wifi["ssid"] = "School-Guest"
    quiet.add_school()
    assert quiet.is_quiet() is True      # a different school network: quiet


def test_manual_quiet_at_home_lapses_when_the_network_changes(wifi):
    quiet.set_override(True)
    assert quiet.is_quiet() is True
    assert quiet.status()["reason"] == "manual"
    wifi["ssid"] = "Cafe"
    assert quiet.is_quiet() is False


def test_auto_clears_the_override(wifi):
    wifi["ssid"] = "School-WiFi"
    quiet.add_school()
    quiet.set_override(False)
    state = quiet.set_override(None)
    assert state["quiet"] is True and state["reason"] == "school Wi-Fi"


def test_offline_is_never_mistaken_for_school(wifi):
    wifi["ssid"] = ""
    with pytest.raises(ValueError):
        quiet.add_school()
    assert quiet.is_quiet() is False


def test_corrupt_state_file_fails_open_to_normal(wifi):
    quiet.STATE_FILE.write_text("{not json", encoding="utf-8")
    assert quiet.is_quiet() is False


def test_network_lookup_never_starts_a_wifi_scan(monkeypatch):
    # A scan takes 3 s+, the lookup timed out, and "" meant "not at school".
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(stdout="no:Neighbour\nyes:School\\:WiFi\n")

    monkeypatch.setattr(quiet, "_ssid_cache", (0.0, ""))     # restored afterwards
    monkeypatch.setattr(quiet.shutil, "which", lambda name: "/usr/bin/nmcli")
    monkeypatch.setattr(quiet.subprocess, "run", fake_run)
    assert quiet.current_ssid(fresh=True) == "School:WiFi"
    assert calls[0][-2:] == ["--rescan", "no"]

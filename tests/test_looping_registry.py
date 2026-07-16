"""Tests for the curated loop action/check registry."""
import socket

import pytest

from friday.looping.registry import build_action, build_check, capabilities


def test_unknown_action_raises():
    with pytest.raises(ValueError):
        build_action("frobnicate", {})


def test_unknown_check_raises():
    with pytest.raises(ValueError):
        build_check("vibes_ok", {})


def test_wifi_connect_requires_ssid():
    with pytest.raises(ValueError):
        build_action("wifi_connect", {})


def test_host_reachable_requires_host():
    with pytest.raises(ValueError):
        build_check("host_reachable", {})


def test_call_tool_rejects_unlisted_tool():
    with pytest.raises(ValueError):
        build_action("call_tool", {"tool": "delete_file", "path": "x"})


def test_host_reachable_true_for_open_port_false_for_closed():
    # Bind a throwaway listener on localhost to get a definitely-open port.
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    open_port = srv.getsockname()[1]
    try:
        check_open = build_check("host_reachable", {"host": "127.0.0.1", "port": open_port})
        assert check_open() is True
    finally:
        srv.close()

    # After close, the port is no longer accepting.
    check_closed = build_check("host_reachable", {"host": "127.0.0.1", "port": open_port})
    assert check_closed() is False


def test_capabilities_lists_actions_and_checks():
    caps = capabilities()
    assert "wifi_reconnect" in caps
    assert "online" in caps

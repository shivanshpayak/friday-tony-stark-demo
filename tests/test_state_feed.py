"""The overlay-state long poll the Event Horizon wallpaper reacts to."""
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from friday.state_feed import StateFeed


def test_first_request_answers_at_once():
    feed = StateFeed(wait=5)
    assert feed.next_after(-1) == (0, "sleeping")


def test_repeated_state_is_not_news():
    feed = StateFeed(wait=0.05)
    feed.publish("listening")
    feed.publish("listening")
    assert feed.next_after(-1) == (1, "listening")


def test_waits_for_the_next_state():
    feed = StateFeed(wait=5)
    feed.publish("listening")
    threading.Timer(0.1, feed.publish, args=("thinking",)).start()
    started = time.monotonic()
    assert feed.next_after(1) == (2, "thinking")
    assert time.monotonic() - started < 2


def test_nothing_new_answers_with_the_current_state():
    feed = StateFeed(wait=0.05)
    feed.publish("speaking")
    assert feed.next_after(1) == (1, "speaking")


def test_seq_from_before_a_relaunch_answers_at_once():
    feed = StateFeed(wait=5)
    started = time.monotonic()
    assert feed.next_after(57) == (0, "sleeping")
    assert time.monotonic() - started < 1


@pytest.fixture
def served():
    feed = StateFeed(port=0, wait=5)
    feed.start()
    assert feed.listening(5)
    yield feed
    feed.stop()


def _get(feed, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{feed.port}{path}", timeout=5) as r:
        return json.loads(r.read())


def test_http_long_poll(served):
    assert _get(served, "/state?after=-1") == {"seq": 0, "state": "sleeping"}
    threading.Timer(0.1, served.publish, args=("waking",)).start()
    assert _get(served, "/state?after=0") == {"seq": 1, "state": "waking"}


def test_http_bad_after_is_a_first_request(served):
    assert _get(served, "/state?after=soon") == {"seq": 0, "state": "sleeping"}


def test_http_other_paths_404(served):
    with pytest.raises(urllib.error.HTTPError) as e:
        _get(served, "/")
    assert e.value.code == 404


def test_port_taken_is_not_fatal(served):
    second = StateFeed(port=served.port)
    second.start()          # must not raise
    assert not second.listening(0.3)
    second.stop()


def test_takes_the_port_once_the_old_launcher_lets_go():
    # friday_ctl restart: the new launcher starts while the old one holds the port.
    old = StateFeed(port=0)
    old.start()
    assert old.listening(5)
    new = StateFeed(port=old.port, retry=0.1)
    new.start()
    assert not new.listening(0.3)
    old.stop()
    try:
        assert new.listening(5)
        new.publish("listening")
        assert _get(new, "/state?after=-1") == {"seq": 1, "state": "listening"}
    finally:
        new.stop()


def test_stop_while_still_retrying():
    holder = StateFeed(port=0)
    holder.start()
    assert holder.listening(5)
    waiting = StateFeed(port=holder.port, retry=0.1)
    waiting.start()
    started = time.monotonic()
    waiting.stop()
    holder.stop()
    assert time.monotonic() - started < 2

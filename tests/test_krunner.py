"""KRunner plugin: only claims queries addressed to JARVIS, and never calls the
model while the user is still typing."""
import friday_krunner as kr


def test_prefixed_queries_become_one_ask_match():
    (match,) = kr.matches_for("j what time is it")
    match_id, text, _icon, kind, relevance, props = match
    assert match_id == "ask:what time is it"
    assert text == "Ask JARVIS: what time is it"
    assert kind == 100 and relevance == 1.0
    assert props["subtext"][0] == "s"


def test_accepts_the_spoken_style_prefixes():
    assert kr.matches_for("Jarvis, open YouTube")[0][0] == "ask:open YouTube"
    assert kr.matches_for("jarvis: mute")[0][0] == "ask:mute"


def test_ordinary_searches_are_left_alone():
    for query in ("firefox", "java tutorial", "j", "jarvis", "jarvis   ", ""):
        assert kr.matches_for(query) == []


def test_run_forwards_the_request_off_the_dbus_thread(monkeypatch):
    seen = []

    class InlineThread:
        def __init__(self, target, args, daemon):
            self._run = lambda: target(*args)

        def start(self):
            self._run()

    monkeypatch.setattr(kr.threading, "Thread", InlineThread)
    monkeypatch.setattr(kr, "_answer", seen.append)
    kr.run_match("ask:open youtube")
    kr.run_match("something-else")
    assert seen == ["open youtube"]

"""Tests for open_on_screen kind-detection and routing."""
import os
import webbrowser

import friday.tools.files as files_mod
from friday.tools.web import open_on_screen_impl


def test_auto_opens_bare_domain_as_url(monkeypatch):
    opened = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))
    monkeypatch.setattr(files_mod, "_resolve_and_check", lambda t: None)
    msg = open_on_screen_impl("github.com", "auto")
    assert opened["url"] == "https://github.com"
    assert "sir" in msg.lower()


def test_auto_falls_back_to_google_search(monkeypatch):
    opened = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))
    monkeypatch.setattr(files_mod, "_resolve_and_check", lambda t: None)
    open_on_screen_impl("weather in tokyo", "auto")
    assert opened["url"].startswith("https://www.google.com/search?q=")
    assert "weather" in opened["url"]


def test_explicit_search_kind(monkeypatch):
    opened = {}
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.setdefault("url", url))
    open_on_screen_impl("late 90s trip hop", "search")
    assert opened["url"].startswith("https://www.google.com/search?q=")


def test_file_kind_opens_path_in_roots(monkeypatch, tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hi")
    started = {}
    monkeypatch.setattr(files_mod, "_resolve_and_check", lambda t: f)
    monkeypatch.setattr(os, "startfile", lambda p: started.setdefault("p", p), raising=False)
    msg = open_on_screen_impl(str(f), "file")
    assert started["p"] == str(f)
    assert "sir" in msg.lower()


def test_file_kind_refuses_outside_roots(monkeypatch):
    started = {}
    monkeypatch.setattr(files_mod, "_resolve_and_check", lambda t: None)
    monkeypatch.setattr(os, "startfile", lambda p: started.setdefault("p", p), raising=False)
    msg = open_on_screen_impl("C:/Windows/System32", "file")
    assert "outside" in msg.lower()
    assert "p" not in started  # never launched
